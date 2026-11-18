"""End-to-end simulation: one Grady County, OK tract flows through all six bots over the in-memory
Slack transport and the real SQLite store, plus the hard-gate / G4 / kill-switch / lock tests."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from agents.abstractor_worker import AbstractorWorker, StaticRunsSource, compute_title
from agents.base_worker import (ALL_CHANNELS, CHANNELS, InMemoryTransport, RateLimiter, Settings, envelope_errors,
                                evaluate_hard_gates, evaluate_kill_switch, make_work_order, parse_envelope,
                                render_envelope, sanitize_claims, validate_claims)
from agents.cfo_worker import (CFOWorker, add_buyer, add_comp, max_acquisition_per_nma, underwrite)
from agents.cos_orchestrator import DEAL_CARD_VALIDATOR, MausBotCOS, score_tract
from agents.courthouse_worker import CourthouseWorker, StaticClerkIndex, add_months
from agents.docket_worker import DocketWorker, StaticDocketSource, halo_sections
from agents.trace_worker import StaticOwnerSource, TraceWorker, ledger_summary, record_expense
from db.lock_manager import LockManager
from db.store import Store, now_iso

TODAY = date(2026, 11, 18)
PRINCIPAL = "UPRINCIPAL1"
TID = "OK-GRADY-12-5N-6W-NE4"
LEGAL = "NE/4, Sec 12-5N-6W, Grady County"
EIA = "https://www.eia.gov/dnav/pet/pet_pri_spt_s1_d.htm"


def ev(grade="A", **kw):
    return {"grade": grade, **kw}


LEASE_EV = ev(book_page="Book 2891 Page 114", instrument_no="2023-004415")


def entry(seq, etype, d, grantor, grantee, interest=None, grade="A", **kw):
    return {"seq": seq, "instrument_id": f"I{seq}", "entry_type": etype, "instrument_date": d, "grantor": grantor,
            "grantee": grantee, "recording": {"county": "Grady", "book": "100", "page": str(seq)}, "interest": interest,
            "imaged_online": True, "evidence": ev(grade, book_page=f"Book 100 Page {seq}"), **kw}


def runs_sheet(tract_id=TID, **over):
    sheet = {
        "tract_id": tract_id, "state": "OK", "county": "Grady", "gross_acres": 80, "subject_owner": "Jane Doe",
        "imaging_start_year": 1890,
        "entries": [
            entry(1, "SOVEREIGN_PATENT", "1902-03-01", None, "US Patentee", {"type": "MINERAL", "fraction": "1", "basis": "OF_TOTAL"}),
            entry(2, "WARRANTY_DEED", "1955-01-10", "US Patentee", "John Doe"),
            entry(3, "DEATH", "2001-04-02", "John Doe", None),
            entry(4, "PROBATE_ORDER", "2001-09-01", "John Doe", "Jane Doe", grade="B"),
            entry(5, "OIL_GAS_LEASE", "2023-06-15", "Jane Doe", "Acme Energy LLC", primary_term_months=36),
        ]}
    sheet.update(over)
    return sheet


def lease_record():
    return {"instrument_id": "L-2023-0415", "type": "OIL_GAS_LEASE", "instrument_date": "2023-06-15",
            "effective_date": "2023-06-15", "primary_term_months": 36, "lessor": "Jane Doe", "lessee": "Acme Energy LLC",
            "book": "2891", "page": "114", "instrument_no": "2023-004415", "imaged_viewed": True}


def filing(tracts=None, **over):
    f = {"state": "OK", "regulator": "OK_OCC", "kind": "SPACING", "docket_no": "CD2026-004567",
         "source_url": "https://oklahoma.gov/occ/dockets/CD2026-004567", "operator": "Acme Energy LLC", "county": "Grady",
         "section": "12", "township": "5N", "range": "6W", "hearing_date": "2026-12-15", "order_entered": False,
         "tracts": tracts or [{"tract_ref": "NE/4", "gross_acres": 80, "leased": False}], "imaged": True}
    f.update(over)
    return f


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in {"COMPANY_NAME": "Maus Minerals LLC", "COMPANY_SIGNER": "Pat Principal", "COMPANY_ADDRESS": "1 Main St, Dallas, TX",
                 "ESCROW_AGENT_NAME": "Lone Star Title", "ESCROW_AGENT_ADDRESS": "2 Elm St, Chickasha, OK"}.items():
        monkeypatch.setenv(k, v)
    settings = Settings(db_url=f"sqlite:///{tmp_path / 't.db'}", principal_user_ids=(PRINCIPAL,), slack_msgs_per_second=1000)
    store = Store(settings.db_url)
    store.init_schema()
    slack = InMemoryTransport()
    return type("Env", (), {"settings": settings, "store": store, "slack": slack, "tmp": tmp_path})()


def build(env, *, filings=None, clerk=None, sheets=None, owners=None):
    s, st, tr = env.settings, env.store, env.slack
    owners = owners if owners is not None else {"tax_roll": {"JANE DOE": {"name": "Jane Doe", "mailing_address": "PO Box 9, Chickasha, OK 73018",
                                              "fee_cents": 500, "receipt_ref": "RCPT-001",
                                              "evidence": ev("B", source_url="https://grady.county-taxroll.example.gov/parcel/12-5N-6W")}}}
    bots = type("Bots", (), {})()
    bots.cos = MausBotCOS(tr, st, s)
    bots.docket = DocketWorker(tr, st, s, source=StaticDocketSource(filings if filings is not None else [filing()]), today=lambda: TODAY)
    bots.court = CourthouseWorker(tr, st, s, index=StaticClerkIndex(clerk if clerk is not None else {LEGAL: [lease_record()]}), today=lambda: TODAY)
    bots.abstract = AbstractorWorker(tr, st, s, source=StaticRunsSource(sheets if sheets is not None else {TID: runs_sheet()}), today=lambda: TODAY)
    bots.trace = TraceWorker(tr, st, s, source=StaticOwnerSource(owners))
    bots.cfo = CFOWorker(tr, st, s, packet_dir=str(env.tmp / "packets"))
    return bots


def seed_cfo(env, comp_grades=("A", "B", "A")):
    for i, g in enumerate(comp_grades):
        add_comp(env.store, source_type="OK_CLO", state="OK", county="Grady", sale_date=f"2026-0{i + 1}-10", acres=160,
                 price_per_acre=380 + 10 * i, evidence=ev(g, docket_no=f"CLO-2026-0{i + 1}", source_url=f"https://clo.ok.gov/sales/2026-0{i + 1}"))
    add_buyer(env.store, buyer_name="Acme Energy LLC", buyer_type="OPERATOR", state="OK", county="Grady",
              max_bonus_per_acre=420, evidence=ev("B", source_url="https://docs.example.com/loi/acme-2026-11-18.pdf"))


def run_market(env, bots, wti=72.4, gas=3.1, now=118, base=120):
    bots.cos.start_market_check(wti, gas, now, base, EIA)
    env.slack.drain()


def senders(env):
    out = set()
    for m in env.slack.messages:
        e = parse_envelope(m.text)
        if e:
            out.add(e["from_agent"])
    return out


# ============================================================ the headline test
def test_grady_county_tract_flows_through_all_six_bots(env):
    bots = build(env)
    seed_cfo(env)
    record_expense(env.store, TID, "COPIES", 1850, "certified copy of lease Book 2891/114", "RCPT-0042", "abstractor-contractor")
    record_expense(env.store, TID, "MILEAGE", 4200, "courthouse run, 60 mi @ $0.70", None, "trace-bot")
    bots.cos.set_underwrite_inputs(TID, acquisition_per_nma=100.0, orri_pct=0.02)

    run_market(env, bots)
    assert bots.cos.kill_switch_on()[0] is False

    bots.cos.start_scan("OK", "OK_OCC")
    env.slack.drain()

    t = env.store.get_tract(TID)
    assert senders(env) == {"mausbot-cos", "docket-bot", "courthouse-bot", "abstractor-bot", "trace-bot", "cfo-bot"}
    assert t["status"] == "TIER1_ESCALATED", (t["status"], t["hold_reason"])
    assert t["abstractor_status"] == "TITLE_VERIFIED_CLEAN"
    assert t["nma"] == pytest.approx(80.0)
    assert t["lease_info"]["expiration_date"] == "2026-06-15"
    assert t["score"] >= 80 and t["tier"] == "TIER_1"
    assert t["net_spread_per_nma"] >= env.settings.min_spread_threshold
    # ledger: 500 (tax roll lookup, booked by TraceBot) + 1850 + 4200
    assert ledger_summary(env.store, TID)["total_cents"] == 6550
    assert t["deal_card"]["costs"]["total_overhead_usd"] == pytest.approx(65.50)

    # ---- #exec-briefings Block Kit card with the four buttons, card validates against the schema
    card_msgs = env.slack.in_channel(CHANNELS["exec"])
    card = next(m for m in card_msgs if m.blocks)
    actions = next(b for b in card.blocks if b["type"] == "actions")
    assert [e["text"]["text"] for e in actions["elements"]] == [
        "Approve & Generate Packets", "Request Field Abstractor", "Hold", "Reject"]
    assert [e["action_id"] for e in actions["elements"]] == ["deal_approve", "deal_field_abstractor", "deal_hold", "deal_reject"]
    assert not list(DEAL_CARD_VALIDATOR.iter_errors(t["deal_card"]))
    # nothing reached #exec-briefings before the gates cleared (only the single card exists)
    assert len([m for m in card_msgs if m.blocks]) == 1

    # ---- principal approves -> CFOBot assembles packets
    reply = bots.cos.handle_action("deal_approve", TID, PRINCIPAL)
    assert reply.startswith("Approved")
    env.slack.drain()
    t = env.store.get_tract(TID)
    assert t["status"] == "PACKETS_GENERATED"
    files = sorted(p.name for p in (env.tmp / "packets" / TID).iterdir())
    assert files == ["affidavit_non_production.md", "assignment_with_orri.md", "escrow_instructions.md",
                     "operator_pitch_packet.md", "owner_outreach_letters.md", "top_lease_option.md"]
    assignment = (env.tmp / "packets" / TID / "assignment_with_orri.md").read_text()
    assert "2.0%" in assignment and "{{" not in assignment
    assert "Acme Energy LLC" in assignment
    # the approved card lost its buttons
    assert not any(b["type"] == "actions" for b in env.slack.in_channel(CHANNELS["exec"])[0].blocks or [])
    # a second click is idempotent
    assert "already processed" in bots.cos.handle_action("deal_approve", TID, PRINCIPAL)


# ============================================================ gates
GOOD = dict(abstractor_status="TITLE_VERIFIED_CLEAN", curative_cost_priced=False, curative_cost_usd=0,
            comp_grades=["A", "B"], title_grades=["A", "B"], net_spread_per_nma=120, min_spread=50, kill_switch=False)


@pytest.mark.parametrize("change,gate", [
    ({"abstractor_status": "TITLE_DEFECTS_FOUND"}, "title"),
    ({"abstractor_status": "FIELD_TICKET_REQUIRED", "curative_cost_priced": True, "curative_cost_usd": 700}, "title"),
    ({"comp_grades": ["A", "C"]}, "evidence_grade"),
    ({"title_grades": ["A", "D"]}, "evidence_grade"),
    ({"comp_grades": []}, "evidence_grade"),
    ({"net_spread_per_nma": 49.99}, "spread"),
    ({"net_spread_per_nma": None}, "spread"),
    ({"kill_switch": True}, "kill_switch_clear"),
])
def test_each_hard_gate_blocks(change, gate):
    r = evaluate_hard_gates(**{**GOOD, **change})
    assert not r.passed and r.gates[gate] is False


def test_hard_gates_pass_and_curative_priced_path():
    assert evaluate_hard_gates(**GOOD).passed
    priced = evaluate_hard_gates(**{**GOOD, "abstractor_status": "TITLE_DEFECTS_FOUND", "curative_cost_priced": True, "curative_cost_usd": 900})
    assert priced.passed


def test_cfo_will_not_clear_dirty_title_and_cos_does_not_escalate(env):
    sheets = {TID: runs_sheet(entries=runs_sheet()["entries"] + [
        entry(6, "MORTGAGE", "2015-02-02", "Jane Doe", "First Bank")])}   # open mortgage, never released
    bots = build(env, sheets=sheets)
    seed_cfo(env)
    bots.cos.set_underwrite_inputs(TID, acquisition_per_nma=100.0)
    run_market(env, bots)
    bots.cos.start_scan("OK", "OK_OCC")
    env.slack.drain()
    t = env.store.get_tract(TID)
    assert t["abstractor_status"] == "TITLE_DEFECTS_FOUND"
    assert t["curative_cost_priced"] == 1                      # curative priced into the spread
    assert t["status"] in ("TIER1_ESCALATED", "TIER2_QUEUE")   # allowed because priced and spread still clears
    assert "OPEN_MORTGAGE" in t["deal_card"]["title"]["defects"]
    # ...but closing is blocked until TITLE_VERIFIED_CLEAN, both by COS and by CFOBot
    if t["status"] == "TIER1_ESCALATED":
        assert "TITLE_VERIFIED_CLEAN" in bots.cos.handle_action("deal_approve", TID, PRINCIPAL)
    wo = make_work_order("mausbot-cos", "cfo-bot", TID, "ASSEMBLE_CLOSING_PACKET", {})
    env.store.insert("work_orders", work_order_id=wo["id"], tract_id=TID, task_type=wo["task_type"], assigned_to="cfo-bot",
                     status="OPEN", payload={}, created_at=now_iso(), updated_at=now_iso())
    res = bots.cfo.execute(wo)
    assert res.outcome == "CLOSING_BLOCKED_TITLE" and bots.abstract.closing_blocked(TID)
    assert not (env.tmp / "packets" / TID).exists()


def test_field_ticket_title_never_reaches_exec(env):
    sheet = runs_sheet()
    sheet["entries"][1]["imaged_online"] = False       # 1955 deed not imaged online
    bots = build(env, sheets={TID: sheet})
    seed_cfo(env)
    run_market(env, bots)
    bots.cos.start_scan("OK", "OK_OCC")
    env.slack.drain()
    t = env.store.get_tract(TID)
    assert t["abstractor_status"] == "FIELD_TICKET_REQUIRED" and t["status"] == "AWAITING_FIELD_ABSTRACT"
    assert any("COURTHOUSE_FIELD_TICKET" in m.text for m in env.slack.in_channel(CHANNELS["abstract"]))
    assert not any(m.blocks for m in env.slack.in_channel(CHANNELS["exec"]))
    # field abstractor returns the book pull -> pipeline resumes and passes
    fixed = dict(sheet["entries"][1], imaged_online=True)
    sheet["entries"][1] = fixed
    bots.cos.set_underwrite_inputs(TID, acquisition_per_nma=100.0)
    bots.cos.attach_field_records(TID, [])
    env.slack.drain()
    assert env.store.get_tract(TID)["status"] == "TIER1_ESCALATED"


def test_low_spread_blocks_escalation(env):
    bots = build(env)
    seed_cfo(env)
    bots.cos.set_underwrite_inputs(TID, acquisition_per_nma=400.0)     # 420 - 400 = 20/NMA < $50 floor
    run_market(env, bots)
    bots.cos.start_scan("OK", "OK_OCC")
    env.slack.drain()
    t = env.store.get_tract(TID)
    assert t["status"] == "HELD_GATE_FAIL" and "G3 spread" in t["hold_reason"]
    assert not any(m.blocks for m in env.slack.in_channel(CHANNELS["exec"]))


def test_grade_c_comp_blocks_escalation(env):
    bots = build(env)
    seed_cfo(env)
    cid = add_comp(env.store, source_type="BLM", state="OK", county="Grady", sale_date="2026-05-01", acres=80, price_per_acre=300,
                   evidence=ev("C", source_url="https://broker-blog.example.com/sale"))
    bots.cos.set_underwrite_inputs(TID, acquisition_per_nma=100.0, comp_ids=[cid])
    run_market(env, bots)
    bots.cos.start_scan("OK", "OK_OCC")
    env.slack.drain()
    assert env.store.get_tract(TID)["status"] == "HELD_GATE_FAIL"
    assert "G2 evidence" in env.store.get_tract(TID)["hold_reason"]


# ============================================================ kill switch
def test_kill_switch_math():
    s = Settings()
    assert evaluate_kill_switch(70, 3, 100, 100, s) == (False, [])
    assert evaluate_kill_switch(50, 3, 100, 100, s)[0]
    assert evaluate_kill_switch(70, 1.5, 100, 100, s)[0]
    assert evaluate_kill_switch(70, 3, 75, 100, s)[0]            # -25% permits
    assert evaluate_kill_switch(70, 3, 85, 100, s)[0] is False   # -15% is inside the 20% tolerance
    assert evaluate_kill_switch(None, 3, 100, 100, s) == (True, ["MISSING_MARKET_DATA"])   # fail closed


def test_kill_switch_pauses_acquisition_then_resumes(env):
    bots = build(env)
    seed_cfo(env)
    bots.cos.set_underwrite_inputs(TID, acquisition_per_nma=100.0)
    run_market(env, bots, wti=41.0)                              # below floor
    bots.cos.start_scan("OK", "OK_OCC")                          # docket monitoring still runs
    env.slack.drain()
    t = env.store.get_tract(TID)
    assert t["status"] == "PAUSED_KILL_SWITCH"
    assert env.store.select("work_orders", {"tract_id": TID}) == []     # nothing dispatched downstream
    assert any("kill-switch ON" in m.text for m in env.slack.in_channel(CHANNELS["exec"]))
    run_market(env, bots, wti=74.0)                              # recovers
    assert env.store.get_tract(TID)["status"] == "TIER1_ESCALATED"


def test_approval_rechecks_kill_switch(env):
    bots = build(env)
    seed_cfo(env)
    bots.cos.set_underwrite_inputs(TID, acquisition_per_nma=100.0)
    run_market(env, bots)
    bots.cos.start_scan("OK", "OK_OCC")
    env.slack.drain()
    assert env.store.get_tract(TID)["status"] == "TIER1_ESCALATED"
    run_market(env, bots, wti=38.0)                              # market turns before the Principal clicks
    out = bots.cos.handle_action("deal_approve", TID, PRINCIPAL)
    assert "gates failed" in out and "kill-switch" in out
    assert env.store.get_tract(TID)["status"] == "HELD_GATE_FAIL"


# ============================================================ buttons
def test_buttons_authorization_hold_reject_field(env):
    bots = build(env)
    seed_cfo(env)
    bots.cos.set_underwrite_inputs(TID, acquisition_per_nma=100.0)
    run_market(env, bots)
    bots.cos.start_scan("OK", "OK_OCC")
    env.slack.drain()
    assert "not authorized" in bots.cos.handle_action("deal_approve", TID, "UINTRUDER")
    assert env.store.get_tract(TID)["status"] == "TIER1_ESCALATED"
    assert bots.cos.handle_action("deal_hold", TID, PRINCIPAL) == "Held."
    assert env.store.get_tract(TID)["status"] == "HELD_BY_PRINCIPAL"
    assert "Field abstractor requested" in bots.cos.handle_action("deal_field_abstractor", TID, PRINCIPAL)
    env.slack.drain()
    assert env.store.get_tract(TID)["abstractor_status"] == "FIELD_TICKET_REQUIRED"
    assert any("COURTHOUSE_FIELD_TICKET" in m.text for m in env.slack.in_channel(CHANNELS["abstract"]))
    t = env.store.get_tract(TID)
    env.store.update_tract(TID, status="TIER1_ESCALATED")
    assert bots.cos.handle_action("deal_reject", TID, PRINCIPAL) == "Rejected."
    assert env.store.get_tract(TID)["status"] == "REJECTED_BY_PRINCIPAL"


# ============================================================ G4 Evidence-or-Null
def test_g4_validator_and_sanitizer():
    secondary = ("zillow.com",)
    good = {"field": "a", "value": 5, "evidence": ev("A", book_page="Book 1 Page 2")}
    nocite = {"field": "b", "value": "2026-01-01", "evidence": None}
    nogrd = {"field": "c", "value": 1, "evidence": {"source_url": "https://x.gov/r"}}
    badurl = {"field": "d", "value": 1, "evidence": ev("B", source_url="ftp://x")}
    zillow = {"field": "e", "value": 1, "evidence": ev("A", source_url="https://www.zillow.com/x")}
    nullv = {"field": "f", "value": None, "evidence": None}
    bad = validate_claims([good, nocite, nogrd, badurl, zillow, nullv], secondary)
    assert [v.split(":")[0] for v in bad] == ["b", "c", "d", "e"]
    clean, nulled = sanitize_claims([good, nocite, zillow], secondary)
    assert nulled == ["b", "e"] and clean[1]["value"] is None and clean[0]["value"] == 5


def test_cos_rejects_uncited_receipt_then_holds_after_max_revisions(env):
    bots = build(env)
    run_market(env, bots)
    wo = bots.cos.dispatch(TID, "COURTHOUSE_SCREEN", {"state": "OK", "county": "Grady", "legal_description": LEGAL})
    env.store.insert("tracts", tract_id=TID, state="OK", county="Grady", legal_description=LEGAL, status="SCREENING",
                     created_at=now_iso(), updated_at=now_iso())
    env.slack.messages.clear()
    env.slack._queue.clear()
    receipt = {"type": "WORKER_RECEIPT", "id": "rc_x", "work_order_id": wo, "tract_id": TID, "task_type": "COURTHOUSE_SCREEN",
               "from_agent": "courthouse-bot", "to_agent": "mausbot-cos", "status": "COMPLETE", "outcome": "PASS",
               "payload": {"lease": {}}, "claims": [{"field": "lease.expiration_date", "value": "2026-06-15", "evidence": None}],
               "g4_nulled": [], "created_at": now_iso()}
    assert not envelope_errors(receipt)
    bots.cos.handle_receipt(receipt)
    rr = [parse_envelope(m.text) for m in env.slack.messages if parse_envelope(m.text)]
    assert rr and rr[0]["type"] == "REVISION_REQUEST" and rr[0]["attempt"] == 1
    assert env.store.get("work_orders", "work_order_id", wo)["status"] == "REVISION_REQUESTED"
    assert env.store.get_tract(TID)["status"] == "SCREENING"          # did not advance
    # a stale re-post of the superseded work order is ignored by the worker
    env.slack._queue.clear()
    env.slack.post(CHANNELS["dispatch"], render_envelope(make_work_order("mausbot-cos", "courthouse-bot", TID, "COURTHOUSE_SCREEN", {}) | {"id": wo}, "stale"))
    env.slack.drain(3)
    assert any(r["event"] == "STALE_WORK_ORDER_IGNORED" for r in env.store.select("audit_log", {"tract_id": TID}))
    # at the revision cap the tract is held for a human instead of looping forever
    env.store.update("work_orders", "work_order_id", wo, status="CLAIMED", revision_count=env.settings.max_revisions)
    bots.cos.handle_receipt(receipt)
    assert env.store.get_tract(TID)["status"] == "HELD_G4"
    assert env.store.get("work_orders", "work_order_id", wo)["status"] == "FAILED"


def test_worker_nulls_uncited_claims_and_reports_partial(env):
    bots = build(env, clerk={LEGAL: [{**lease_record(), "book": None, "page": None, "instrument_no": None}]})
    run_market(env, bots)
    env.store.insert("tracts", tract_id=TID, state="OK", county="Grady", legal_description=LEGAL, status="SCREENING",
                     created_at=now_iso(), updated_at=now_iso())
    bots.cos.dispatch(TID, "COURTHOUSE_SCREEN", {"state": "OK", "county": "Grady", "legal_description": LEGAL})
    env.slack.drain(8)
    rc = [parse_envelope(m.text) for m in env.slack.in_channel(CHANNELS["title_exp"])]
    rc = [r for r in rc if r and r["type"] == "WORKER_RECEIPT"]
    assert rc and rc[0]["status"] in ("PARTIAL", "COMPLETE")
    assert all(c["value"] is None for c in rc[0]["claims"] if c["evidence"] is None)


# ============================================================ locks, single-tract-first
def test_lock_manager_atomic_and_expiry(env):
    lm = LockManager(env.store, ttl_seconds=60)
    assert lm.acquire("T1", "COURTHOUSE_SCREEN", "m1")
    assert not lm.acquire("T1", "COURTHOUSE_SCREEN", "m2")        # second machine loses
    assert lm.acquire("T1", "ABSTRACT_TITLE", "m2")              # different scope is independent
    assert lm.acquire("T1", "COURTHOUSE_SCREEN", "m1")           # re-entrant for the owner
    assert not lm.release("T1", "COURTHOUSE_SCREEN", "m2")       # non-owner cannot release
    later = datetime_plus(120)
    assert lm.acquire("T1", "COURTHOUSE_SCREEN", "m2", now=later)    # expired lock is taken over
    assert lm.holder("T1", "COURTHOUSE_SCREEN") == "m2"


def datetime_plus(seconds):
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def test_duplicate_work_order_blocked_and_worker_respects_lock(env):
    bots = build(env)
    run_market(env, bots)
    env.store.insert("tracts", tract_id=TID, state="OK", county="Grady", legal_description=LEGAL, status="SCREENING",
                     created_at=now_iso(), updated_at=now_iso())
    first = bots.cos.dispatch(TID, "COURTHOUSE_SCREEN", {"state": "OK", "county": "Grady", "legal_description": LEGAL})
    assert first and bots.cos.dispatch(TID, "COURTHOUSE_SCREEN", {}) is None
    # another machine already holds the lock: this worker must not scrape
    LockManager(env.store).acquire(TID, "COURTHOUSE_SCREEN", "courthouse-bot@other-machine")
    env.slack.drain()
    assert any(r["event"] == "LOCK_CONTESTED" for r in env.store.select("audit_log", {"tract_id": TID}))
    assert not [m for m in env.slack.in_channel(CHANNELS["title_exp"])]


def test_single_tract_first_queues_second_tract(env):
    two = filing(tracts=[{"tract_ref": "NE/4", "gross_acres": 80, "leased": False},
                         {"tract_ref": "SW/4", "gross_acres": 80, "leased": False}])
    bots = build(env, filings=[two], clerk={LEGAL: [lease_record()]})        # SW/4 has no lease of record
    run_market(env, bots)
    bots.cos.start_scan("OK", "OK_OCC")
    env.slack.drain(40)
    events = [(r["tract_id"], r["event"]) for r in env.store.select("audit_log")]
    sw = "OK-GRADY-12-5N-6W-SW4"
    assert (sw, "QUEUED_SINGLE_TRACT_FIRST") in events
    env.slack.drain()
    # once tract 1 left the deep pipeline the queue released SW/4, which then failed its own pre-screen
    assert env.store.get_tract(sw)["status"] == "REJECTED_EXPIRATION_SCREEN"


# ============================================================ domain engines
def test_nma_math_exact_fractions_npri_and_defects():
    ent = [
        entry(1, "SOVEREIGN_PATENT", "1902-03-01", None, "Patentee", {"type": "MINERAL", "fraction": "1", "basis": "OF_TOTAL"}),
        entry(2, "WARRANTY_DEED", "1930-01-01", "Patentee", "Ann", {"type": "MINERAL", "fraction": "1/2", "basis": "OF_GRANTOR_INTEREST"}),
        entry(3, "WARRANTY_DEED", "1931-01-01", "Ann", "Jane Doe", {"type": "MINERAL", "fraction": "1/3", "basis": "OF_GRANTOR_INTEREST"}),
        entry(4, "RESERVATION", "1932-01-01", "Patentee", "Patentee", {"type": "NPRI", "fraction": "1/16", "basis": "OF_TOTAL"}),
    ]
    r = compute_title({"tract_id": "T", "county": "Grady", "gross_acres": 160, "subject_owner": "Jane Doe", "entries": ent, "imaging_start_year": 1890}, 1890, TODAY)
    assert r["holders"] == {"Patentee": "1/2", "Ann": "1/3", "Jane Doe": "1/6"}
    assert r["subject_mineral_fraction"] == "1/6" and r["nma"] == pytest.approx(160 / 6)
    assert r["npri_royalty_fraction"] == "1/16"            # NPRI never leaks into NMA
    assert r["abstractor_status"] == "TITLE_VERIFIED_CLEAN"


def test_curative_defects_detected():
    ent = [
        entry(1, "SOVEREIGN_PATENT", "1902-03-01", None, "Patentee", {"type": "MINERAL", "fraction": "1", "basis": "OF_TOTAL"}),
        entry(2, "WARRANTY_DEED", "1930-01-01", "Patentee", "Ann", homestead=True, spouse_joined=False),
        entry(3, "DEATH", "1990-01-01", "Ann", None),
        entry(4, "MORTGAGE", "1980-01-01", "Ann", "Bank"),
        entry(5, "OIL_GAS_LEASE", "2024-01-01", "Ann", "Op"),                    # no term shown: genuine unreleased lease
        entry(6, "WARRANTY_DEED", "2000-01-01", "Stranger", "Bob"),               # grantor holds nothing
        entry(7, "WARRANTY_DEED", "2001-01-01", "Ann", "Jane Doe", {"type": "MINERAL", "fraction": "3/2", "basis": "OF_TOTAL"}),
    ]
    r = compute_title({"tract_id": "T", "county": "Grady", "gross_acres": 80, "subject_owner": "Jane Doe", "entries": ent}, 1890, TODAY)
    codes = {d["code"] for d in r["defects"]}
    assert {"MISSING_HOMESTEAD_JOINDER", "MISSING_PROBATE", "OPEN_MORTGAGE", "UNRELEASED_LEASE", "CHAIN_GAP", "OVERCONVEYANCE"} <= codes
    assert r["abstractor_status"] == "TITLE_DEFECTS_FOUND" and r["curative_cost_usd"] > 5000


def test_pre_imaging_triggers_field_ticket_and_blocks_clean():
    sheet = runs_sheet(imaging_start_year=1990)
    r = compute_title(sheet, 1990, TODAY)
    assert r["abstractor_status"] == "FIELD_TICKET_REQUIRED"
    assert {t["reason"] for t in r["field_tickets"]} == {"PRE_IMAGING_1990"}


def test_grade_c_title_record_is_not_clean():
    sheet = runs_sheet()
    sheet["entries"][2]["evidence"] = ev("C", source_url="https://genealogy.example.com/x")
    assert compute_title(sheet, 1890, TODAY)["abstractor_status"] == "FIELD_TICKET_REQUIRED"


def test_courthouse_prescreen_windows_and_disqualifiers():
    store = Store("sqlite:///:memory:")
    store.init_schema()
    tr = InMemoryTransport()
    ch = lambda recs: CourthouseWorker(tr, store, Settings(), index=StaticClerkIndex({LEGAL: recs}), today=lambda: TODAY)
    wo = make_work_order("mausbot-cos", "courthouse-bot", TID, "COURTHOUSE_SCREEN", {"state": "OK", "county": "Grady", "legal_description": LEGAL})
    assert ch([lease_record()]).execute(wo).outcome == "PASS"
    for typ in ("EXTENSION", "RATIFICATION", "SHUT_IN", "UNIT_DESIGNATION", "ACTIVE_PRODUCTION"):
        later = {"instrument_id": "X1", "type": typ, "instrument_date": "2025-01-01", "book": "3000", "page": "1"}
        assert ch([lease_record(), later]).execute(wo).outcome == "FAIL", typ
    young = {**lease_record(), "effective_date": "2025-06-15"}                        # 17 months old: outside both windows
    assert ch([young]).execute(wo).outcome == "FAIL"
    five_year = {**lease_record(), "effective_date": "2021-09-01", "primary_term_months": 60}   # 62 months old, expires 2026-09-01
    assert ch([five_year]).execute(wo).outcome == "PASS"
    assert add_months(date(2021, 9, 1), 60) == date(2026, 9, 1)


def test_halo_geometry():
    h = halo_sections(12, "5N", "6W", 2)
    assert len(h) == 24 and {"section": 12, "township": "5N", "range": "6W"} not in h
    corner = halo_sections(6, "5N", "6W", 1)      # NW corner section reaches into the next range/township
    assert {"section": 1, "township": "5N", "range": "7W"} in corner
    assert any(x["township"] == "6N" for x in corner)


def test_docket_sweet_spot_rules():
    store = Store("sqlite:///:memory:")
    store.init_schema()
    d = DocketWorker(InMemoryTransport(), store, Settings(), source=StaticDocketSource([
        filing(),                                                  # qualifies
        filing(docket_no="CD-2", order_entered=True),              # pooling order already entered: too late
        filing(docket_no="CD-3", hearing_date="2026-01-05"),       # hearing in the past
        filing(docket_no="CD-4", tracts=[{"tract_ref": "NW/4", "gross_acres": 80, "leased": True}]),
        filing(docket_no="CD-5", tracts=[{"tract_ref": "SE/4", "gross_acres": 80}]),     # lease status unknown
        filing(docket_no="CD-6", kind="APD", tracts=[]),           # APD -> halo
    ]), today=lambda: TODAY)
    res = d.execute(make_work_order("mausbot-cos", "docket-bot", "scan:OK", "DOCKET_SCAN", {"state": "OK", "regulator": "OK_OCC"}))
    ids = [t["tract_id"] for t in res.payload["tracts"]]
    assert ids.count(TID) == 1
    assert sum(1 for t in res.payload["tracts"] if t["trigger"] == "APD_HALO") == 24
    assert sum(1 for t in res.payload["tracts"] if t["trigger"] == "UNIT_SWEET_SPOT") == 1
    assert res.payload["permit_counts"] == {"OK": 1}


def test_underwriting_math_orri_and_jv():
    u = underwrite(nma=80, acquisition_per_nma=100, exit_per_nma=420, overhead_usd=65.5, curative_usd=0)
    assert u["net_spread_per_nma"] == pytest.approx((320 * 80 - 65.5) / 80, abs=0.01)
    jv = underwrite(nma=80, acquisition_per_nma=100, exit_per_nma=420, overhead_usd=0, curative_usd=0, structure="JV_SPLIT", jv_partner_pct=0.5)
    assert jv["net_spread_per_nma"] == pytest.approx(160)
    cap = max_acquisition_per_nma(nma=80, exit_per_nma=420, overhead_usd=65.5, curative_usd=0, closing_costs_usd=0,
                                  min_spread=50, structure="BACK_TO_BACK_LOI", jv_partner_pct=0)
    assert underwrite(nma=80, acquisition_per_nma=cap, exit_per_nma=420, overhead_usd=65.5, curative_usd=0)["net_spread_per_nma"] == pytest.approx(50, abs=0.01)


def test_orri_out_of_range_rejected(env):
    bots = build(env)
    seed_cfo(env)
    bots.cos.set_underwrite_inputs(TID, acquisition_per_nma=100.0, orri_pct=0.05)
    run_market(env, bots)
    bots.cos.start_scan("OK", "OK_OCC")
    env.slack.drain()
    assert env.store.get_tract(TID)["status"] == "HELD_WORKER_FAILED"       # 5% is outside the 1-3% carve-out


def test_comps_register_rejects_uncited_and_secondary(env):
    with pytest.raises(ValueError):
        add_comp(env.store, source_type="OK_CLO", state="OK", county="Grady", sale_date="2026-01-01", acres=80, price_per_acre=100, evidence={"grade": "A"})
    with pytest.raises(ValueError):
        add_comp(env.store, source_type="OK_CLO", state="OK", county="Grady", sale_date="2026-01-01", acres=80, price_per_acre=100,
                 evidence=ev("A", source_url="https://www.zillow.com/x"), settings=env.settings)
    with pytest.raises(ValueError):
        add_comp(env.store, source_type="CRAIGSLIST", state="OK", county="Grady", sale_date="2026-01-01", acres=80, price_per_acre=100, evidence=ev("A", docket_no="X"))


def test_expense_ledger_rules(env):
    record_expense(env.store, "T9", "RECORDING_FEE", 2600, "recording affidavit", "REC-77", "trace-bot")
    record_expense(env.store, "T9", "POSTAGE", 195, "certified mail", None, "trace-bot")
    s = ledger_summary(env.store, "T9")
    assert s["total_cents"] == 2795 and s["unreceipted_entries"] == 1
    for bad in (("NOPE", 100, "x"), ("COPIES", -5, "x"), ("COPIES", 10.5, "x"), ("COPIES", 10, "  ")):
        with pytest.raises(ValueError):
            record_expense(env.store, "T9", bad[0], bad[1], bad[2])


# ============================================================ plumbing
def test_scoring_engine_bounds():
    kw = dict(target_spread=500, min_grade="A", structure="BACK_TO_BACK_LOI", exit_cited=True, trigger="UNIT_SWEET_SPOT")
    top, _ = score_tract(net_spread=900, title_status="TITLE_VERIFIED_CLEAN", curative_priced=False, **kw)
    assert top == 100
    low, parts = score_tract(net_spread=50, title_status="TITLE_DEFECTS_FOUND", curative_priced=True, **{**kw, "min_grade": "B"})
    assert low < 80 and parts["title"] == 12


def test_rate_limiter_paces_posts():
    t = [0.0]
    slept = []
    rl = RateLimiter(rate_per_sec=1, burst=2, clock=lambda: t[0], sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)))
    for _ in range(4):
        rl.acquire()
    assert round(sum(slept), 2) == 2.0          # 2 burst tokens free, then 1/sec


def test_envelope_roundtrip_and_schema():
    wo = make_work_order("mausbot-cos", "docket-bot", "scan:OK", "DOCKET_SCAN", {"state": "OK"})
    assert not envelope_errors(wo)
    assert parse_envelope(render_envelope(wo, "hello")) == wo
    assert envelope_errors({**wo, "to_agent": "nobody"})
    assert envelope_errors({**wo, "task_type": "SCRAPE_EVERYTHING"})


def test_deal_card_schema_rejects_value_without_evidence():
    good = {"value": 80.0, "evidence": ev("A", book_page="Book 1 Page 2")}
    from agents.cos_orchestrator import _ev
    assert _ev(80.0, None) == {"value": None, "evidence": None}      # Evidence-or-Null
    assert _ev(80.0, good["evidence"])["value"] == 80.0
    defs = {"$schema": "http://json-schema.org/draft-07/schema#", "$ref": "#/definitions/ev_number",
            "definitions": json.loads((Path(__file__).parent.parent / "schemas" / "deal_card.schema.json").read_text())["definitions"]}
    dv = Draft7Validator(defs)
    assert not list(dv.iter_errors(good))
    assert list(dv.iter_errors({"value": 80.0, "evidence": None}))
    assert list(dv.iter_errors({"value": 80.0, "evidence": {"grade": "A"}}))
    assert not list(dv.iter_errors({"value": None, "evidence": None}))
