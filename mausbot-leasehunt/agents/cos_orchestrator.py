"""Seat 0 — MausBot, Chief of Staff. Dispatch loop, G4 enforcement, kill-switch, single-tract-first,
0-100 scoring engine, hard gates, and the #exec-briefings Block Kit escalator + approval buttons."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import date, datetime, timezone

from jsonschema import Draft7Validator
from sqlalchemy.exc import IntegrityError

from db.lock_manager import LockManager
from db.store import Store, new_id, now_iso

from .base_worker import (CHANNELS, ROOT, BoltTransport, GateResult, LLMAssistant, RateLimiter, Settings, Transport,
                          envelope_errors, evaluate_hard_gates, evaluate_kill_switch, load_dotenv, make_work_order,
                          parse_envelope, render_envelope, validate_claims)
from .trace_worker import EXPENSE_CATEGORIES, ledger_summary, record_expense

DEAL_CARD_VALIDATOR = Draft7Validator(json.loads((ROOT / "schemas" / "deal_card.schema.json").read_text()))

DEEP_ACTIVE = {"SCREENING", "TITLE_EXPIRATION_PASSED", "TITLE_ABSTRACTED", "AWAITING_FIELD_ABSTRACT",
               "UNDERWRITTEN", "TIER1_ESCALATED", "APPROVED"}
TERMINAL = {"REJECTED_EXPIRATION_SCREEN", "REJECTED_BY_PRINCIPAL", "HELD_GATE_FAIL", "HELD_G4", "HELD_WORKER_FAILED",
            "HELD_OWNER_UNLOCATED", "HELD_BY_PRINCIPAL", "TIER2_QUEUE", "PACKETS_GENERATED", "CLOSING_BLOCKED"}
ASSIGNEE = {"DOCKET_SCAN": "docket-bot", "COURTHOUSE_SCREEN": "courthouse-bot", "ABSTRACT_TITLE": "abstractor-bot",
            "FIELD_TICKET_REQUEST": "abstractor-bot", "TRACE_OWNERS": "trace-bot", "UNDERWRITE": "cfo-bot",
            "MARKET_CHECK": "cfo-bot", "ASSEMBLE_CLOSING_PACKET": "cfo-bot"}
# tasks that are acquisition work and therefore stop while the cyclicality kill-switch is ON
ACQUISITION_TASKS = {"COURTHOUSE_SCREEN", "ABSTRACT_TITLE", "TRACE_OWNERS", "UNDERWRITE", "ASSEMBLE_CLOSING_PACKET"}
APPROVE_FROM = {"TIER1_ESCALATED", "HELD_BY_PRINCIPAL"}


# ------------------------------------------------------------------ scoring engine (0-100)
def score_tract(*, net_spread: float, target_spread: float, title_status: str, curative_priced: bool,
                min_grade: str, structure: str, exit_cited: bool, trigger: str | None) -> tuple[int, dict]:
    """Spread 35 · Title 25 · Evidence 15 · Buyer demand 15 · Docket timing 10."""
    b = {
        "spread": 35 * min(1.0, max(0.0, net_spread) / target_spread),
        "title": 25 if title_status == "TITLE_VERIFIED_CLEAN" else (12 if curative_priced else 0),
        "evidence": {"A": 15, "B": 10}.get(min_grade, 0),
        "buyer": (15 if structure == "BACK_TO_BACK_LOI" else 10) if exit_cited else 0,
        "docket": {"UNIT_SWEET_SPOT": 10, "APD_HALO": 6}.get(trigger or "", 0),
    }
    b = {k: round(v, 1) for k, v in b.items()}
    return max(0, min(100, round(sum(b.values())))), b


def _ev(value, evidence):
    """Evidence-or-Null: a value without a citation is emitted as null."""
    return {"value": value, "evidence": evidence} if value is not None and evidence else {"value": None, "evidence": None}


# ------------------------------------------------------------------ Block Kit
def deal_card_blocks(card: dict) -> list[dict]:
    tid, pr, ti = card["tract_id"], card["pricing"], card["title"]
    loc, nma = card["location"], card["nma"]["value"]
    g = card["gates"]
    gate_line = "  ".join(f"{':white_check_mark:' if v else ':x:'} {k}" for k, v in g.items())
    lease = card["lease"]
    cites = [f"Lease {lease['recording']['value']} ({lease['recording']['evidence']['grade']})"
             if lease["recording"]["value"] else "Lease: null"]
    cites += [f"{c['comp_id']} ${c['price_per_acre']['value']:.0f}/ac ({c['grade']})" for c in card["comps"][:4]]
    btn = lambda text, aid, style=None, confirm=None: {
        "type": "button", "text": {"type": "plain_text", "text": text}, "action_id": aid, "value": tid,
        **({"style": style} if style else {}), **({"confirm": confirm} if confirm else {})}
    confirm = lambda title, text: {"title": {"type": "plain_text", "text": title}, "text": {"type": "mrkdwn", "text": text},
                                   "confirm": {"type": "plain_text", "text": "Yes"}, "deny": {"type": "plain_text", "text": "Cancel"}}
    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"Tier-1 Deal · {tid} · score {card['score']}"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Location*\n{loc['county']} Co., {loc['state']}\n{loc['legal_description']}"},
            {"type": "mrkdwn", "text": f"*NMA*\n{nma if nma is not None else 'null'} of {card['gross_acres']['value']} gross ac"},
            {"type": "mrkdwn", "text": f"*Net spread / NMA*\n${pr['net_spread_per_nma']:,.2f} (floor ${pr['min_spread_threshold']:.0f}, target ${pr.get('target_spread_per_nma', 500):.0f}+)"},
            {"type": "mrkdwn", "text": f"*Structure*\n{pr['structure']} · ORRI {pr['orri_pct'] * 100:.1f}%"},
            {"type": "mrkdwn", "text": f"*Buy → Sell / NMA*\n${pr['acquisition_per_nma']:,.2f} → ${pr['exit_per_nma']['value']:,.2f}"},
            {"type": "mrkdwn", "text": f"*Title*\n{ti['abstractor_status']}" + (f"\ncurative priced: ${ti['curative_cost_total']:,.0f}" if ti["curative_cost_priced"] else "")},
        ]},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Hard gates*  {gate_line}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "*Evidence:* " + " · ".join(cites)}]},
        {"type": "actions", "block_id": f"deal_actions_{tid}", "elements": [
            btn("Approve & Generate Packets", "deal_approve", "primary",
                confirm("Generate packets?", "Re-runs every hard gate, then has CFOBot assemble the closing packet.")),
            btn("Request Field Abstractor", "deal_field_abstractor"),
            btn("Hold", "deal_hold"),
            btn("Reject", "deal_reject", "danger", confirm("Reject deal?", "This closes the tract."))]},
    ]


class MausBotCOS:
    agent_id = "mausbot-cos"
    channel = CHANNELS["dispatch"]
    prompt_file = "00_mausbot_cos.md"

    def __init__(self, transport: Transport, store: Store, settings: Settings | None = None,
                 locks: LockManager | None = None, limiter: RateLimiter | None = None):
        self.transport, self.store = transport, store
        self.settings = settings or Settings.from_env()
        self.locks = locks or LockManager(store, self.settings.lock_ttl_seconds)
        self.limiter = limiter or RateLimiter(self.settings.slack_msgs_per_second)
        transport.subscribe(self.on_message)

    # ================================================================== helpers
    def _say(self, channel: str, text: str, blocks: list | None = None) -> str:
        self.limiter.acquire()
        return self.transport.post(channel, text, blocks)

    def _send(self, env: dict, summary: str) -> None:
        errs = envelope_errors(env)
        if errs:
            raise ValueError(f"invalid envelope: {errs}")
        self._say(self.channel, render_envelope(env, summary))

    def market(self) -> dict | None:
        return self.store.latest_market()

    def kill_switch_on(self) -> tuple[bool, list[str]]:
        """Recomputed from raw numbers every time. No snapshot == fail closed."""
        m = self.market()
        if not m:
            return True, ["NO_MARKET_SNAPSHOT"]
        return evaluate_kill_switch(m["wti_usd"], m["henry_hub_usd"], m["permit_count_current"],
                                    m["permit_count_baseline"], self.settings)

    def _done(self, tract_id: str, task: str) -> bool:
        return bool(self.store.scalar("SELECT COUNT(*) FROM work_orders WHERE tract_id=:t AND task_type=:k AND status='ACCEPTED'",
                                      t=tract_id, k=task))

    def _live(self, tract_id: str, task: str) -> bool:
        return bool(self.store.scalar("SELECT COUNT(*) FROM work_orders WHERE tract_id=:t AND task_type=:k AND status IN ('OPEN','CLAIMED','COMPLETE')",
                                      t=tract_id, k=task))

    def _merge_evidence(self, tract_id: str, claims: list[dict], prefix_strip: str | None = None) -> None:
        t = self.store.get_tract(tract_id)
        em = dict(t.get("evidence_map") or {})
        for c in claims:
            if c.get("value") is not None and c.get("evidence"):
                f = c["field"]
                if prefix_strip and f.startswith(prefix_strip):
                    f = f[len(prefix_strip):]
                em[f] = c["evidence"]
        self.store.update_tract(tract_id, evidence_map=em)

    # ================================================================== dispatch
    def dispatch(self, tract_id: str, task: str, payload: dict | None = None) -> str | None:
        """Create + post a work order. Returns its id, or None if blocked/duplicate."""
        if task in ACQUISITION_TASKS:
            on, why = self.kill_switch_on()
            if on:
                self.store.update_tract(tract_id, status="PAUSED_KILL_SWITCH", hold_reason="; ".join(why))
                self.store.audit(self.agent_id, "PAUSED_KILL_SWITCH", tract_id, task=task, reasons=why)
                return None
        wo = make_work_order(self.agent_id, ASSIGNEE[task], tract_id, task, payload or {})
        try:
            self.store.insert("work_orders", work_order_id=wo["id"], tract_id=tract_id, task_type=task,
                              assigned_to=wo["to_agent"], status="OPEN", payload=wo["payload"],
                              created_at=now_iso(), updated_at=now_iso())
        except IntegrityError:   # unique (tract, task) live work order: duplicate-scrape guard
            self.store.audit(self.agent_id, "DUPLICATE_DISPATCH_BLOCKED", tract_id, task=task)
            return None
        self.store.audit(self.agent_id, "DISPATCH", tract_id, task=task, to=wo["to_agent"], work_order=wo["id"])
        self._send(wo, f"@{wo['to_agent']} {task} for `{tract_id}`")
        return wo["id"]

    def start_scan(self, state: str, regulator: str, since: str | None = None) -> str | None:
        return self.dispatch(f"scan:{state}:{regulator}:{date.today().isoformat()}", "DOCKET_SCAN",
                             {"state": state, "regulator": regulator, "since": since})

    def start_market_check(self, wti: float | None = None, henry_hub: float | None = None,
                           permits_now: int | None = None, permits_base: int | None = None,
                           source_url: str | None = None) -> str | None:
        if permits_now is None:   # fall back to the latest DocketBot permit count
            rows = self.store.select("audit_log", {"event": "PERMIT_COUNT"}, order_by="ts DESC", limit=1)
            permits_now = rows[0]["detail"].get("count") if rows else None
        if permits_base is None:
            m = self.market()
            permits_base = m["permit_count_baseline"] if m else None
        return self.dispatch(f"market:{now_iso()}", "MARKET_CHECK",
                             {"wti_usd": wti, "henry_hub_usd": henry_hub, "permit_count_current": permits_now,
                              "permit_count_baseline": permits_base, "source_url": source_url})

    # ================================================================== inbound
    def on_message(self, channel: str, text: str, event: dict) -> None:
        env = parse_envelope(text)
        if not env or env.get("to_agent") != self.agent_id or env.get("type") != "WORKER_RECEIPT":
            return
        if envelope_errors(env):
            self.store.audit(self.agent_id, "INVALID_RECEIPT", env.get("tract_id"))
            return
        self.handle_receipt(env)

    def _revise(self, wo: dict, receipt: dict, reasons: list[str]) -> None:
        attempt = wo["revision_count"] + 1
        tid = receipt["tract_id"]
        if attempt > self.settings.max_revisions:
            self.store.update("work_orders", "work_order_id", wo["work_order_id"], status="FAILED", updated_at=now_iso())
            if not tid.startswith(("scan:", "market:")):
                self.store.update_tract(tid, status="HELD_G4", hold_reason="; ".join(reasons)[:500])
            self._say(self.channel, f":no_entry: `{tid}` {wo['task_type']} failed G4 after {attempt - 1} revisions — held for a human.\n" +
                      "\n".join(f"• {r}" for r in reasons))
            self.store.audit(self.agent_id, "G4_ESCALATED", tid, reasons=reasons)
            return
        self.store.update("work_orders", "work_order_id", wo["work_order_id"], status="REVISION_REQUESTED", updated_at=now_iso())
        req = {"type": "REVISION_REQUEST", "id": new_id("rv_"), "work_order_id": wo["work_order_id"], "tract_id": tid,
               "task_type": wo["task_type"], "from_agent": self.agent_id, "to_agent": receipt["from_agent"],
               "reasons": reasons, "attempt": attempt, "created_at": now_iso()}
        self.store.audit(self.agent_id, "REVISION_REQUEST", tid, reasons=reasons, attempt=attempt)
        self._send(req, f"@{receipt['from_agent']} revision {attempt} for `{tid}` {wo['task_type']} (G4 Evidence-or-Null)")

    def handle_receipt(self, r: dict) -> None:
        wo = self.store.get("work_orders", "work_order_id", r["work_order_id"])
        if not wo or wo["status"] == "ACCEPTED":
            return
        tid, task = r["tract_id"], r["task_type"]
        if r["status"] in ("FAILED", "BLOCKED"):
            self.store.update("work_orders", "work_order_id", wo["work_order_id"], status="FAILED", updated_at=now_iso())
            if not tid.startswith(("scan:", "market:")):
                self.store.update_tract(tid, status="HELD_WORKER_FAILED", hold_reason=json.dumps(r["payload"])[:500])
            self._say(self.channel, f":warning: `{tid}` {task} {r['status']}: {json.dumps(r['payload'])[:400]}")
            return
        # ---- G4: independent re-validation (never trust the worker's own check)
        reasons = validate_claims(r["claims"], self.settings.secondary_domains)
        reasons += [f"{f}: nulled by worker for missing citation" for f in r.get("g4_nulled", [])]
        if r["status"] == "PARTIAL" or reasons:
            self._revise(wo, r, reasons or ["PARTIAL receipt without stated reason"])
            return
        self.store.update("work_orders", "work_order_id", wo["work_order_id"], status="ACCEPTED", updated_at=now_iso())
        handler = {"MARKET_CHECK": self._on_market, "DOCKET_SCAN": self._on_docket, "COURTHOUSE_SCREEN": self._on_courthouse,
                   "ABSTRACT_TITLE": self._on_abstract, "TRACE_OWNERS": self._on_trace, "UNDERWRITE": self._on_underwrite,
                   "ASSEMBLE_CLOSING_PACKET": self._on_packet, "FIELD_TICKET_REQUEST": self._on_field_ticket}.get(task)
        if handler:
            handler(r)

    # ================================================================== receipt handlers
    def _on_market(self, r: dict) -> None:
        on, why = self.kill_switch_on()
        if on:
            self._say(CHANNELS["exec"], ":rotating_light: *Cyclicality kill-switch ON — acquisition paused.*\n" +
                      "\n".join(f"• {x}" for x in why))
        else:
            self._say(self.channel, ":green_circle: Kill-switch clear. Resuming paused tracts.")
            self.resume()

    def _on_docket(self, r: dict) -> None:
        p = r["payload"]
        count = sum(p.get("permit_counts", {}).values())
        self.store.audit(self.agent_id, "PERMIT_COUNT", r["tract_id"], count=count, by_state=p.get("permit_counts"))
        em_by_tract: dict[str, list] = {}
        for c in r["claims"]:
            em_by_tract.setdefault(c["field"].split(".", 1)[0], []).append(c)
        for c in p["tracts"]:
            tid = c["tract_id"]
            if self.store.get_tract(tid):
                continue
            self.store.insert("tracts", tract_id=tid, state=c["state"], county=c["county"],
                              legal_description=c["legal_description"], section=c.get("section"),
                              township=c.get("township"), range_=c.get("range"), gross_acres=c.get("gross_acres"),
                              source_docket=c.get("docket_no"), trigger_type=c.get("trigger"),
                              status="DOCKET_QUALIFIED", created_at=now_iso(), updated_at=now_iso())
            self._merge_evidence(tid, em_by_tract.get(tid, []), prefix_strip=f"{tid}.")
            self.advance(tid)

    def _on_courthouse(self, r: dict) -> None:
        tid = r["tract_id"]
        if r["outcome"] != "PASS":
            self.store.update_tract(tid, status="REJECTED_EXPIRATION_SCREEN", hold_reason="no qualifying expiring lease")
            self._say(self.channel, f":x: `{tid}` failed the expiration pre-screen.")
            self.pump_queue()
            return
        lease = r["payload"]["lease"]
        self.store.update_tract(tid, lease_info=lease, status="TITLE_EXPIRATION_PASSED")
        self._merge_evidence(tid, r["claims"])
        self.advance(tid)

    def _on_abstract(self, r: dict) -> None:
        self._merge_evidence(r["tract_id"], r["claims"])
        self.advance(r["tract_id"])

    def _on_trace(self, r: dict) -> None:
        tid = r["tract_id"]
        if r["outcome"] == "OWNERS_UNLOCATED":
            self.store.update_tract(tid, status="HELD_OWNER_UNLOCATED", hold_reason="no owner located")
            self._say(self.channel, f":mag: `{tid}` held: no owner located.")
            self.pump_queue()
            return
        self.advance(tid)

    def _on_field_ticket(self, r: dict) -> None:
        self._say(CHANNELS["exec"], f":clipboard: Field abstractor tickets issued for `{r['tract_id']}` — see #abstract-title.")

    def _on_packet(self, r: dict) -> None:
        tid = r["tract_id"]
        if r["outcome"] == "PACKETS_GENERATED":
            self._say(CHANNELS["exec"], f":white_check_mark: `{tid}` closing packet generated: {r['payload']['packet_dir']}")
        else:
            self.store.update_tract(tid, status="CLOSING_BLOCKED", hold_reason=r["payload"].get("reason"))
            self._say(CHANNELS["exec"], f":no_entry: `{tid}` closing blocked: {r['payload'].get('reason')}")
        self.pump_queue()

    # ================================================================== state machine
    def deep_active_count(self, exclude: str | None = None) -> int:
        n = self.store.active_count(sorted(DEEP_ACTIVE))
        if exclude:
            t = self.store.get_tract(exclude)
            if t and t["status"] in DEEP_ACTIVE:
                n -= 1
        return n

    def proven_count(self) -> int:
        return self.store.active_count(["PACKETS_GENERATED"])

    def advance(self, tract_id: str) -> None:
        t = self.store.get_tract(tract_id)
        if not t or t["status"] in TERMINAL or t["status"] in ("REJECTED_BY_PRINCIPAL",):
            return
        on, why = self.kill_switch_on()
        if on:
            self.store.update_tract(tract_id, status="PAUSED_KILL_SWITCH", hold_reason="; ".join(why))
            self.store.audit(self.agent_id, "PAUSED_KILL_SWITCH", tract_id, reasons=why)
            return
        if not self._done(tract_id, "COURTHOUSE_SCREEN"):
            if self._live(tract_id, "COURTHOUSE_SCREEN"):
                return
            if self.settings.single_tract_first and self.proven_count() == 0 and self.deep_active_count(exclude=tract_id) >= 1:
                self.store.update_tract(tract_id, status="QUEUED_SINGLE_TRACT_FIRST",
                                        hold_reason="single-tract-first: another tract is in the deep pipeline")
                self.store.audit(self.agent_id, "QUEUED_SINGLE_TRACT_FIRST", tract_id)
                return
            self.store.update_tract(tract_id, status="SCREENING", hold_reason=None)
            self.dispatch(tract_id, "COURTHOUSE_SCREEN", {"state": t["state"], "county": t["county"],
                                                           "legal_description": t["legal_description"]})
            return
        lease = t.get("lease_info") or {}
        if not self._done(tract_id, "ABSTRACT_TITLE") and not self._live(tract_id, "ABSTRACT_TITLE"):
            self.dispatch(tract_id, "ABSTRACT_TITLE", {"lease": lease})
        if not self._done(tract_id, "TRACE_OWNERS") and not self._live(tract_id, "TRACE_OWNERS"):
            self.dispatch(tract_id, "TRACE_OWNERS", {"state": t["state"], "county": t["county"],
                                                      "owners": [{"name": lease.get("lessor"), "role": "EXPIRED_LESSOR"}]
                                                      if lease.get("lessor") else []})
        if not (self._done(tract_id, "ABSTRACT_TITLE") and self._done(tract_id, "TRACE_OWNERS")):
            return
        t = self.store.get_tract(tract_id)
        if t["abstractor_status"] == "FIELD_TICKET_REQUIRED":
            self.store.update_tract(tract_id, status="AWAITING_FIELD_ABSTRACT")
            return
        if not self._done(tract_id, "UNDERWRITE") and not self._live(tract_id, "UNDERWRITE"):
            self.dispatch(tract_id, "UNDERWRITE", self._underwrite_inputs(tract_id))

    def _underwrite_inputs(self, tract_id: str) -> dict:
        rows = self.store.select("audit_log", {"tract_id": tract_id, "event": "UNDERWRITE_INPUTS"}, order_by="ts DESC", limit=1)
        return rows[0]["detail"] if rows else {}

    def set_underwrite_inputs(self, tract_id: str, **inputs) -> None:
        """Principal-supplied LOI / offer / ORRI inputs (kept in the audit log, re-used on every UNDERWRITE)."""
        self.store.audit("principal", "UNDERWRITE_INPUTS", tract_id, **inputs)

    def attach_field_records(self, tract_id: str, entries: list[dict]) -> None:
        """Field abstractor's supplemental run-sheet entries -> re-run abstract + underwrite."""
        self.store.execute("UPDATE work_orders SET status='SUPERSEDED' WHERE tract_id=:t AND task_type IN ('ABSTRACT_TITLE','UNDERWRITE') AND status='ACCEPTED'", t=tract_id)
        self.store.update_tract(tract_id, status="TITLE_EXPIRATION_PASSED", abstractor_status="NOT_STARTED")
        self.dispatch(tract_id, "ABSTRACT_TITLE", {"lease": (self.store.get_tract(tract_id).get("lease_info") or {}),
                                                    "supplemental_entries": entries})

    def pump_queue(self) -> None:
        for t in self.store.select("tracts", {"status": "QUEUED_SINGLE_TRACT_FIRST"}, order_by="created_at"):
            if self.deep_active_count() == 0 or self.proven_count() > 0:
                self.advance(t["tract_id"])
            if self.deep_active_count() >= 1 and self.proven_count() == 0:
                break

    def resume(self) -> int:
        n = 0
        for t in self.store.select("tracts", {"status": "PAUSED_KILL_SWITCH"}, order_by="created_at"):
            self.store.update_tract(t["tract_id"], status="DOCKET_QUALIFIED", hold_reason=None)
            self.advance(t["tract_id"])
            n += 1
        return n

    # ================================================================== underwriting receipt -> gates -> escalation
    def _on_underwrite(self, r: dict) -> None:
        tid, p = r["tract_id"], r["payload"]
        t = self.store.get_tract(tid)
        if r["outcome"] == "NO_EXIT_PRICE":
            self.store.update_tract(tid, status="HELD_GATE_FAIL", hold_reason="no cited LOI/buyer demand (exit price null)")
            self._say(self.channel, f":hourglass: `{tid}` held: no cited exit price (Evidence-or-Null).")
            self.pump_queue()
            return
        on, _ = self.kill_switch_on()
        curative = t["curative_cost_cents"] / 100
        gate: GateResult = evaluate_hard_gates(
            abstractor_status=t["abstractor_status"], curative_cost_priced=bool(t["curative_cost_priced"]),
            curative_cost_usd=curative, comp_grades=[c["grade"] for c in p["comps"]], title_grades=p["title_grades"],
            net_spread_per_nma=p["pricing"]["net_spread_per_nma"], min_spread=self.settings.min_spread_threshold, kill_switch=on)
        if not gate.passed or r["outcome"] != "ESCALATION_ELIGIBLE":
            self.store.update_tract(tid, status="HELD_GATE_FAIL", hold_reason="; ".join(gate.failures)[:500])
            self.store.audit(self.agent_id, "GATE_FAIL", tid, failures=gate.failures)
            self._say(self.channel, f":stop_sign: `{tid}` blocked from #exec-briefings:\n" + "\n".join(f"• {f}" for f in gate.failures))
            self.pump_queue()
            return
        card = self.build_deal_card(t, p, gate, on)
        errs = [f"{'/'.join(map(str, e.path))}: {e.message}" for e in DEAL_CARD_VALIDATOR.iter_errors(card)]
        if errs:
            self.store.update_tract(tid, status="HELD_G4", hold_reason="; ".join(errs)[:500])
            self._say(self.channel, f":no_entry: `{tid}` deal card failed schema/G4 validation:\n" + "\n".join(f"• {e}" for e in errs[:8]))
            self.pump_queue()
            return
        self.store.update_tract(tid, score=card["score"], tier=card["tier"], deal_card=card)
        if card["tier"] != "TIER_1":
            self.store.update_tract(tid, status="TIER2_QUEUE", hold_reason=f"score {card['score']} < {self.settings.tier1_score}")
            self._say(self.channel, f":bar_chart: `{tid}` scored {card['score']} (Tier-2) — not escalated.")
            self.pump_queue()
            return
        self.escalate(card)

    def build_deal_card(self, t: dict, p: dict, gate: GateResult, kill: bool) -> dict:
        em, lease, pr = t.get("evidence_map") or {}, t.get("lease_info") or {}, p["pricing"]
        owners = self.store.select("owners", {"tract_id": t["tract_id"]})
        rs = self.store.select("runs_sheets", {"tract_id": t["tract_id"]}, order_by="version DESC", limit=1)
        grades = [c["grade"] for c in p["comps"]] + p["title_grades"]
        min_g = min(grades, key=lambda g: "DCBA".index(g)) if grades else "D"
        score, breakdown = score_tract(
            net_spread=pr["net_spread_per_nma"], target_spread=self.settings.target_spread, title_status=t["abstractor_status"],
            curative_priced=bool(t["curative_cost_priced"]), min_grade=min_g, structure=pr["structure"],
            exit_cited=bool(pr.get("exit_evidence")), trigger=t.get("trigger_type"))
        self.store.audit(self.agent_id, "SCORE", t["tract_id"], score=score, breakdown=breakdown)
        return {
            "schema_version": "1.0", "tract_id": t["tract_id"], "score": score,
            "tier": "TIER_1" if score >= self.settings.tier1_score else "TIER_2",
            "location": {"state": t["state"], "county": t["county"], "legal_description": t["legal_description"]},
            "nma": _ev(t["nma"], em.get("nma")), "gross_acres": _ev(t["gross_acres"], em.get("gross_acres")),
            "lease": {"lessor": _ev(lease.get("lessor"), em.get("lease.lessor")),
                      "recording": _ev(lease.get("recording"), em.get("lease.recording")),
                      "effective_date": _ev(lease.get("effective_date"), em.get("lease.effective_date")),
                      "primary_term_months": _ev(lease.get("primary_term_months"), em.get("lease.primary_term_months")),
                      "expiration_date": _ev(lease.get("expiration_date"), em.get("lease.expiration_date"))},
            "owners": [{"name": _ev(o["name"], o.get("evidence")), "status": o["status"],
                        "resolution_path": o.get("resolution_path")} for o in owners],
            "title": {"abstractor_status": t["abstractor_status"], "curative_cost_priced": bool(t["curative_cost_priced"]),
                      "curative_cost_total": t["curative_cost_cents"] / 100,
                      "defects": [d["code"] for d in (rs[0]["defects"] if rs else [])], "min_evidence_grade": min_g},
            "pricing": {"acquisition_per_nma": pr["acquisition_per_nma"], "exit_per_nma": _ev(pr["exit_per_nma"], pr.get("exit_evidence")),
                        "net_spread_per_nma": pr["net_spread_per_nma"], "min_spread_threshold": pr["min_spread_threshold"],
                        "target_spread_per_nma": pr["target_spread_per_nma"], "structure": pr["structure"],
                        "buyer_name": pr.get("buyer_name"),
                        "jv_partner_pct": pr.get("jv_partner_pct") or 0.0, "orri_pct": pr["orri_pct"]},
            "costs": {"total_overhead_usd": p["overhead_usd"]},
            "comps": [{"comp_id": c["comp_id"], "grade": c["grade"], "price_per_acre": _ev(c["price_per_acre"], c["evidence"])}
                      for c in p["comps"]],
            "gates": gate.gates, "cyclicality_kill_switch": bool(kill), "generated_at": now_iso(),
        }

    def escalate(self, card: dict) -> None:
        tid = card["tract_id"]
        ts = self._say(CHANNELS["exec"], f"Tier-1 deal {tid} (score {card['score']})", deal_card_blocks(card))
        self.store.update_tract(tid, status="TIER1_ESCALATED", exec_message_ts=ts)
        self.store.audit(self.agent_id, "ESCALATED", tid, score=card["score"])

    # ================================================================== buttons
    def _gates_now(self, t: dict) -> GateResult:
        card = t["deal_card"]
        on, _ = self.kill_switch_on()
        return evaluate_hard_gates(
            abstractor_status=t["abstractor_status"], curative_cost_priced=bool(t["curative_cost_priced"]),
            curative_cost_usd=t["curative_cost_cents"] / 100, comp_grades=[c["grade"] for c in card["comps"]],
            title_grades=[card["title"]["min_evidence_grade"]], net_spread_per_nma=card["pricing"]["net_spread_per_nma"],
            min_spread=self.settings.min_spread_threshold, kill_switch=on)

    def _close_card(self, t: dict, note: str) -> None:
        card = t["deal_card"]
        if t.get("exec_message_ts"):
            blocks = [b for b in deal_card_blocks(card) if b["type"] != "actions"]
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
            self.transport.update(CHANNELS["exec"], t["exec_message_ts"], f"Tier-1 deal {t['tract_id']}: {note}", blocks)

    def handle_action(self, action_id: str, tract_id: str, user_id: str) -> str:
        if user_id not in self.settings.principal_user_ids:
            self.store.audit(self.agent_id, "UNAUTHORIZED_ACTION", tract_id, user=user_id, action=action_id)
            return "You are not authorized to act on deals (PRINCIPAL_USER_IDS)."
        t = self.store.get_tract(tract_id)
        if not t or not t.get("deal_card"):
            return f"Unknown tract {tract_id}."
        if t["status"] not in APPROVE_FROM:
            return f"`{tract_id}` is {t['status']}; this action was already processed or no longer applies."
        self.store.audit("principal", f"BUTTON_{action_id}", tract_id, user=user_id)
        if action_id == "deal_approve":
            gate = self._gates_now(t)
            if not gate.passed:
                self.store.update_tract(tract_id, status="HELD_GATE_FAIL", hold_reason="; ".join(gate.failures)[:500])
                self._close_card(t, f":stop_sign: approval refused, gates failed on re-check: {'; '.join(gate.failures)}")
                return "Approval refused — gates failed on re-check:\n" + "\n".join(gate.failures)
            if t["abstractor_status"] != "TITLE_VERIFIED_CLEAN":
                self._say(CHANNELS["exec"], f":lock: `{tract_id}` cannot close: title is {t['abstractor_status']}. Use *Request Field Abstractor*.")
                return "Closing requires TITLE_VERIFIED_CLEAN. Request a field abstractor / cure the defects first."
            self.store.update_tract(tract_id, status="APPROVED")
            self.dispatch(tract_id, "ASSEMBLE_CLOSING_PACKET", {"buyer_name": t["deal_card"]["pricing"].get("buyer_name")})
            self._close_card(t, f":white_check_mark: Approved by <@{user_id}> — CFOBot assembling packets")
            return "Approved. CFOBot is assembling the closing packet."
        if action_id == "deal_field_abstractor":
            self.store.update_tract(tract_id, status="AWAITING_FIELD_ABSTRACT")
            self.store.execute("UPDATE work_orders SET status='SUPERSEDED' WHERE tract_id=:t AND task_type='FIELD_TICKET_REQUEST' AND status='ACCEPTED'", t=tract_id)
            self.dispatch(tract_id, "FIELD_TICKET_REQUEST", {})
            self._close_card(t, f":clipboard: Field abstractor requested by <@{user_id}>")
            return "Field abstractor requested; tickets will appear in #abstract-title."
        if action_id == "deal_hold":
            self.store.update_tract(tract_id, status="HELD_BY_PRINCIPAL", hold_reason=f"held by {user_id}")
            self._close_card(t, f":pause_button: Held by <@{user_id}>")
            return "Held."
        if action_id == "deal_reject":
            self.store.update_tract(tract_id, status="REJECTED_BY_PRINCIPAL", hold_reason=f"rejected by {user_id}")
            self._close_card(t, f":x: Rejected by <@{user_id}>")
            self.pump_queue()
            return "Rejected."
        return "Unknown action."

    # ================================================================== slash commands
    def handle_command(self, text: str, user_id: str) -> str:
        parts = (text or "").split()
        cmd, args = (parts[0].lower() if parts else "status"), parts[1:]
        write = cmd in ("scan", "market", "expense", "resume", "loi")
        if write and user_id not in self.settings.principal_user_ids:
            return "Only principals can run write commands."
        try:
            if cmd == "status":
                counts = self.store.select("tracts")
                by: dict[str, int] = {}
                for t in counts:
                    by[t["status"]] = by.get(t["status"], 0) + 1
                on, why = self.kill_switch_on()
                return f"kill-switch: {'ON ' + '; '.join(why) if on else 'clear'}\ntracts: {json.dumps(by)}"
            if cmd == "scan":
                return f"dispatched {self.start_scan(args[0].upper(), args[1].upper())}"
            if cmd == "market":
                wti, hh, now, base = float(args[0]), float(args[1]), int(args[2]), int(args[3])
                return f"dispatched {self.start_market_check(wti, hh, now, base, args[4] if len(args) > 4 else 'https://www.eia.gov/dnav/pet/pet_pri_spt_s1_d.htm')}"
            if cmd == "tract":
                t = self.store.get_tract(args[0])
                return "```" + json.dumps({k: t[k] for k in ("tract_id", "status", "abstractor_status", "nma", "net_spread_per_nma", "score", "tier", "hold_reason")}, indent=1, default=str) + "```" if t else "unknown tract"
            if cmd == "ledger":
                return "```" + json.dumps(ledger_summary(self.store, args[0]), indent=1) + "```"
            if cmd == "expense":   # expense <tract> <CATEGORY> <usd> <description...> [receipt=<ref>]
                tail = " ".join(args[3:])
                receipt = None
                if " receipt=" in " " + tail:
                    tail, _, receipt = tail.rpartition("receipt=")
                record_expense(self.store, args[0], args[1].upper(), round(float(args[2]) * 100), tail.strip(), receipt, user_id)
                return f"booked ${float(args[2]):.2f} {args[1].upper()} to {args[0]}"
            if cmd == "loi":   # loi <tract> <price_per_nma> <buyer name> <source_url>
                self.set_underwrite_inputs(args[0], loi={"price_per_nma": float(args[1]), "buyer_name": " ".join(args[2:-1]),
                                                         "evidence": {"source_url": args[-1], "grade": "B"}})
                return "LOI recorded for next underwrite"
            if cmd == "resume":
                return f"resumed {self.resume()} tract(s)"
        except (IndexError, ValueError) as e:
            return f"usage error: {e}. Commands: status | scan <st> <reg> | market <wti> <gas> <permits> <baseline> [url] | tract <id> | ledger <id> | expense <tract> <CAT> <usd> <desc> [receipt=<ref>] | loi <tract> <price> <buyer> <url> | resume"
        return "unknown command"

    def field_ticket_text(self, tract_id: str) -> str:
        rows = self.store.select("runs_sheets", {"tract_id": tract_id}, order_by="version DESC", limit=1)
        return "```" + json.dumps(rows[0]["field_tickets"], indent=1, default=str) + "```" if rows else "no runs sheet"

    # ================================================================== housekeeping loop
    def tick(self, stale_seconds: int = 600) -> int:
        """Re-post work orders nobody claimed (worker was offline), unstick queues. Idempotent."""
        n = 0
        cutoff = datetime.now(timezone.utc).timestamp() - stale_seconds
        for wo in self.store.select("work_orders", {"status": "OPEN"}):
            ts = datetime.strptime(wo["created_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()
            if ts < cutoff:
                env = {"type": "WORK_ORDER", "id": wo["work_order_id"], "tract_id": wo["tract_id"], "task_type": wo["task_type"],
                       "from_agent": self.agent_id, "to_agent": wo["assigned_to"], "payload": wo["payload"],
                       "revision_of": wo.get("parent_id"), "created_at": wo["created_at"]}
                self._send(env, f"(repost) @{wo['assigned_to']} {wo['task_type']} `{wo['tract_id']}`")
                self.store.update("work_orders", "work_order_id", wo["work_order_id"], updated_at=now_iso())
                n += 1
        self.pump_queue()
        return n


# ================================================================== production runner
def run_cos() -> None:
    load_dotenv()
    import logging
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    settings = Settings.from_env()
    store = Store(settings.db_url)
    store.init_schema()
    transport = BoltTransport(os.environ["SLACK_BOT_TOKEN"], os.environ["SLACK_APP_TOKEN"])
    cos = MausBotCOS(transport, store, settings)
    app, assistant = transport.app, LLMAssistant("00_mausbot_cos.md", settings)

    def _act(action_id: str):
        def handler(ack, body, respond):  # noqa: ANN001
            ack()
            tract_id = body["actions"][0]["value"]
            respond(text=cos.handle_action(action_id, tract_id, body["user"]["id"]), replace_original=False, response_type="ephemeral")
        return handler

    for aid in ("deal_approve", "deal_field_abstractor", "deal_hold", "deal_reject"):
        app.action(aid)(_act(aid))

    @app.command("/leasehunt")
    def _cmd(ack, command, respond):  # noqa: ANN001
        ack()
        respond(cos.handle_command(command.get("text", ""), command["user_id"]))

    @app.command("/field-ticket")
    def _ft(ack, command, respond):  # noqa: ANN001
        ack()
        respond(cos.field_ticket_text(command.get("text", "").strip()))

    @app.event("app_mention")
    def _mention(event, say):  # noqa: ANN001
        ctx = json.dumps(store.select("tracts", limit=20), default=str)[:6000]
        say(text=assistant.ask(event.get("text", ""), ctx), thread_ts=event.get("ts"))

    def loop():
        while True:
            time.sleep(int(os.environ.get("TICK_SECONDS", "120")))
            try:
                cos.tick()
            except Exception:
                logging.exception("tick failed")

    threading.Thread(target=loop, daemon=True).start()
    transport.run()


if __name__ == "__main__":
    run_cos()
