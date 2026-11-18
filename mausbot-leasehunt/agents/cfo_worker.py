"""Machine 5 — CFOBot: comps register, buyer demand map, underwriting engine, escrow packet."""
from __future__ import annotations

import os
import re
import statistics
from datetime import date, timedelta
from pathlib import Path

from db.store import Store, new_id, now_iso

from .abstractor_worker import entry_grade
from .base_worker import (CHANNELS, ROOT, BaseWorker, Settings, WorkResult, evaluate_hard_gates, evaluate_kill_switch,
                          evidence_problems, run_agent)

COMP_SOURCES = ("OK_CLO", "NM_SLO", "BLM", "OCC_POOLING")
ORRI_MIN, ORRI_MAX = 0.01, 0.03
OFFER_FRACTION_OF_EXIT = 0.35     # default opening offer when none supplied (bounded by max_acquisition)


class ClosingBlocked(Exception):
    """Raised when a closing packet is requested before TITLE_VERIFIED_CLEAN (or gates fail)."""


# ------------------------------------------------------------------ comps register & buyer map
def add_comp(store: Store, *, source_type: str, state: str, county: str, sale_date: str, acres: float,
             price_per_acre: float, evidence: dict, royalty: float | None = None, settings: Settings | None = None) -> str:
    if source_type not in COMP_SOURCES:
        raise ValueError(f"source_type must be one of {COMP_SOURCES}")
    if acres <= 0 or price_per_acre < 0:
        raise ValueError("acres must be > 0 and price_per_acre >= 0")
    probs = evidence_problems(evidence, (settings or Settings()).secondary_domains)
    if probs:
        raise ValueError(f"comp rejected by G4: {probs}")
    cid = new_id("cp_")
    store.insert("comps", comp_id=cid, source_type=source_type, state=state, county=county, sale_date=sale_date,
                 acres=acres, price_per_acre=price_per_acre, royalty=royalty, grade=evidence["grade"],
                 evidence=evidence, created_at=now_iso())
    return cid


def add_buyer(store: Store, *, buyer_name: str, buyer_type: str, state: str, county: str,
              max_bonus_per_acre: float | None, evidence: dict | None, jv_split_pct: float | None = None,
              notes: str | None = None) -> str:
    if buyer_type not in ("OPERATOR", "BROKER", "CAPITAL_PARTNER"):
        raise ValueError("buyer_type must be OPERATOR, BROKER or CAPITAL_PARTNER")
    bid = new_id("by_")
    store.insert("buyer_demand", buyer_id=bid, buyer_name=buyer_name, buyer_type=buyer_type, state=state, county=county,
                 max_bonus_per_acre=max_bonus_per_acre, jv_split_pct=jv_split_pct, evidence=evidence, active=1,
                 notes=notes, created_at=now_iso())
    return bid


def best_buyer(store: Store, state: str, county: str) -> dict | None:
    rows = [b for b in store.select("buyer_demand", {"state": state, "county": county, "active": 1})
            if b["max_bonus_per_acre"] is not None and b.get("evidence")]
    return max(rows, key=lambda b: b["max_bonus_per_acre"]) if rows else None


def select_comps(store: Store, state: str, county: str, comp_ids: list[str] | None = None) -> list[dict]:
    if comp_ids:
        return [c for c in (store.get("comps", "comp_id", i) for i in comp_ids) if c]
    return [c for c in store.select("comps", {"state": state, "county": county}) if c["grade"] in ("A", "B")]


# ------------------------------------------------------------------ underwriting engine (pure)
def underwrite(*, nma: float, acquisition_per_nma: float, exit_per_nma: float, overhead_usd: float,
               curative_usd: float, closing_costs_usd: float = 0.0, structure: str = "BACK_TO_BACK_LOI",
               jv_partner_pct: float = 0.0) -> dict:
    """Net spread math. Spread is measured on OUR side after costs and any JV partner share.
    ORRI value is upside and deliberately excluded from the spread."""
    if nma <= 0:
        raise ValueError("NMA must be > 0")
    gross_total = (exit_per_nma - acquisition_per_nma) * nma
    net_total = gross_total - overhead_usd - curative_usd - closing_costs_usd
    partner_share = max(net_total, 0) * jv_partner_pct if structure == "JV_SPLIT" else 0.0
    ours = net_total - partner_share
    return {"gross_spread_per_nma": exit_per_nma - acquisition_per_nma, "gross_total_usd": round(gross_total, 2),
            "overhead_usd": overhead_usd, "curative_usd": curative_usd, "closing_costs_usd": closing_costs_usd,
            "net_total_usd": round(net_total, 2), "jv_partner_share_usd": round(partner_share, 2),
            "our_net_usd": round(ours, 2), "net_spread_per_nma": round(ours / nma, 2)}


def max_acquisition_per_nma(*, nma: float, exit_per_nma: float, overhead_usd: float, curative_usd: float,
                            closing_costs_usd: float, min_spread: float, structure: str, jv_partner_pct: float) -> float:
    """Highest offer that still clears the spread floor."""
    keep = 1 - (jv_partner_pct if structure == "JV_SPLIT" else 0)
    need_net_total = min_spread * nma / keep
    return round(exit_per_nma - (need_net_total + overhead_usd + curative_usd + closing_costs_usd) / nma, 2)


# ------------------------------------------------------------------ market data
def fetch_eia_spot(api_key: str) -> tuple[float | None, float | None, str]:
    """Latest daily WTI (RWTC) and Henry Hub (RNGWHHD) spot from the EIA v2 API."""
    import httpx

    def latest(route: str, series: str) -> float | None:
        r = httpx.get(f"https://api.eia.gov/v2/{route}/data/", timeout=30, params={
            "api_key": api_key, "frequency": "daily", "data[0]": "value", "facets[series][]": series,
            "sort[0][column]": "period", "sort[0][direction]": "desc", "length": 1})
        r.raise_for_status()
        rows = r.json()["response"]["data"]
        return float(rows[0]["value"]) if rows else None

    return (latest("petroleum/pri/spt", "RWTC"), latest("natural-gas/pri/fut", "RNGWHHD"),
            "https://www.eia.gov/dnav/pet/pet_pri_spt_s1_d.htm")


# ------------------------------------------------------------------ templates
def render_template(name: str, ctx: dict) -> str:
    text = (ROOT / "templates" / name).read_text()
    missing = sorted({m for m in re.findall(r"\{\{\s*([a-z0-9_]+)\s*\}\}", text) if m not in ctx})
    if missing:
        raise KeyError(f"{name}: missing merge fields {missing}")
    return re.sub(r"\{\{\s*([a-z0-9_]+)\s*\}\}", lambda m: str(ctx[m.group(1)]), text)


class CFOWorker(BaseWorker):
    agent_id = "cfo-bot"
    channel = CHANNELS["cfo"]
    prompt_file = "05_cfo_bot.md"
    tasks = ("MARKET_CHECK", "UNDERWRITE", "ASSEMBLE_CLOSING_PACKET")

    def __init__(self, *args, packet_dir: str | None = None, **kw):
        super().__init__(*args, **kw)
        self.packet_dir = Path(packet_dir or os.environ.get("PACKET_DIR", "packets"))

    # ---- MARKET_CHECK
    def _market_check(self, wo: dict) -> WorkResult:
        p = wo["payload"]
        wti, hh, src = p.get("wti_usd"), p.get("henry_hub_usd"), p.get("source_url")
        if (wti is None or hh is None) and self.settings.eia_api_key:
            w, h, url = fetch_eia_spot(self.settings.eia_api_key)
            wti, hh, src = wti if wti is not None else w, hh if hh is not None else h, src or url
        now, base = p.get("permit_count_current"), p.get("permit_count_baseline")
        kill, reasons = evaluate_kill_switch(wti, hh, now, base, self.settings)
        ev = {"source_url": src, "grade": p.get("evidence_grade", "B")} if src else None
        self.store.insert("market_snapshots", snapshot_id=new_id("mk_"), wti_usd=wti, henry_hub_usd=hh,
                          permit_count_current=now, permit_count_baseline=base, kill_switch=int(kill),
                          reasons=reasons, source=src or "UNSOURCED", created_at=now_iso())
        claims = [self.claim("wti_usd", wti, ev), self.claim("henry_hub_usd", hh, ev),
                  self.claim("permit_count_current", now, p.get("permit_evidence") or ev),
                  self.claim("permit_count_baseline", base, p.get("permit_evidence") or ev)]
        return WorkResult("COMPLETE", {"kill_switch": kill, "reasons": reasons, "wti_usd": wti, "henry_hub_usd": hh,
                                       "permit_count_current": now, "permit_count_baseline": base},
                          claims, outcome="KILL_SWITCH_ON" if kill else "CLEAR")

    # ---- UNDERWRITE
    def _underwrite(self, wo: dict) -> WorkResult:
        tid, p = wo["tract_id"], wo["payload"]
        t = self.store.get_tract(tid)
        nma = t.get("nma")
        if not nma:
            return WorkResult("BLOCKED", {"reason": "NMA unknown (no runs sheet yet)"}, [], "BLOCKED_NO_NMA")
        # exit pricing: tract LOI beats the standing buyer map
        loi = p.get("loi")
        if loi:
            exit_price, ev, structure = loi["price_per_nma"], loi.get("evidence"), loi.get("structure", "BACK_TO_BACK_LOI")
            buyer, jv_pct = loi.get("buyer_name"), loi.get("jv_partner_pct", 0.0)
        else:
            b = best_buyer(self.store, t["state"], t["county"])
            exit_price, ev = (b["max_bonus_per_acre"], b["evidence"]) if b else (None, None)
            structure = "JV_SPLIT" if b and b["buyer_type"] == "CAPITAL_PARTNER" else "BACK_TO_BACK_LOI"
            buyer, jv_pct = (b["buyer_name"] if b else None), ((b or {}).get("jv_split_pct") or 0.0)
        if exit_price is None or evidence_problems(ev, self.settings.secondary_domains):
            return WorkResult("COMPLETE", {"reason": "no cited LOI / buyer demand: exit price is null"},
                              [self.claim("exit_per_nma", None, None)], "NO_EXIT_PRICE")
        overhead = self.store.expenses_total_cents(tid) / 100
        status = t["abstractor_status"]
        curative = t["curative_cost_cents"] / 100
        priced = status == "TITLE_DEFECTS_FOUND" and curative > 0
        closing = float(p.get("closing_costs_usd", 0.0))
        s = self.settings
        max_acq = max_acquisition_per_nma(nma=nma, exit_per_nma=exit_price, overhead_usd=overhead,
                                          curative_usd=curative, closing_costs_usd=closing,
                                          min_spread=s.min_spread_threshold, structure=structure, jv_partner_pct=jv_pct)
        acq = p.get("acquisition_per_nma")
        basis = "OFFER_SUPPLIED"
        if acq is None:
            acq, basis = round(max(0.0, min(exit_price * OFFER_FRACTION_OF_EXIT, max_acq)), 2), "RECOMMENDED"
        orri = float(p.get("orri_pct", 0.02))
        if not ORRI_MIN <= orri <= ORRI_MAX:
            raise ValueError(f"ORRI carve-out must be {ORRI_MIN:.0%}-{ORRI_MAX:.0%}, got {orri:.2%}")
        uw = underwrite(nma=nma, acquisition_per_nma=acq, exit_per_nma=exit_price, overhead_usd=overhead,
                        curative_usd=curative, closing_costs_usd=closing, structure=structure, jv_partner_pct=jv_pct)
        comps = select_comps(self.store, t["state"], t["county"], p.get("comp_ids"))
        rows = self.store.select("runs_sheets", {"tract_id": tid}, order_by="version DESC", limit=1)
        title_grades = [entry_grade(e) for e in rows[0]["entries"]] if rows else []
        mk = self.store.latest_market()
        kill = bool(mk["kill_switch"]) if mk else True
        if mk:   # recompute from raw numbers; never trust a stored boolean alone
            kill, _ = evaluate_kill_switch(mk["wti_usd"], mk["henry_hub_usd"], mk["permit_count_current"],
                                           mk["permit_count_baseline"], s)
        gates = evaluate_hard_gates(abstractor_status=status, curative_cost_priced=priced, curative_cost_usd=curative,
                                    comp_grades=[c["grade"] for c in comps], title_grades=title_grades,
                                    net_spread_per_nma=uw["net_spread_per_nma"], min_spread=s.min_spread_threshold,
                                    kill_switch=kill)
        median = statistics.median(c["price_per_acre"] for c in comps) if comps else None
        self.store.update_tract(tid, net_spread_per_nma=uw["net_spread_per_nma"], curative_cost_priced=int(priced),
                                cyclicality_kill_switch=int(kill), status="UNDERWRITTEN")
        claims = [self.claim("exit_per_nma", exit_price, ev)] + [
            self.claim(f"comp.{c['comp_id']}.price_per_acre", c["price_per_acre"], c["evidence"]) for c in comps]
        payload = {"pricing": {"acquisition_per_nma": acq, "acquisition_basis": basis, "exit_per_nma": exit_price,
                               "exit_evidence": ev, "structure": structure, "jv_partner_pct": jv_pct, "buyer_name": buyer,
                               "orri_pct": orri, "max_acquisition_per_nma": max_acq, "comp_median_per_acre": median,
                               "min_spread_threshold": s.min_spread_threshold, "target_spread_per_nma": s.target_spread,
                               **uw},
                   "comps": [{"comp_id": c["comp_id"], "price_per_acre": c["price_per_acre"], "grade": c["grade"],
                              "evidence": c["evidence"]} for c in comps],
                   "title_grades": title_grades, "curative_cost_priced": priced, "curative_cost_usd": curative,
                   "overhead_usd": overhead, "gates": gates.gates, "gate_failures": gates.failures,
                   "kill_switch": kill}
        return WorkResult("COMPLETE", payload, claims, "ESCALATION_ELIGIBLE" if gates.passed else "GATES_FAILED")

    # ---- ASSEMBLE_CLOSING_PACKET
    def _packet(self, wo: dict) -> WorkResult:
        tid, p = wo["tract_id"], wo["payload"]
        t = self.store.get_tract(tid)
        if t["abstractor_status"] != "TITLE_VERIFIED_CLEAN":
            self.store.audit(self.agent_id, "CLOSING_BLOCKED", tid, abstractor_status=t["abstractor_status"])
            return WorkResult("BLOCKED", {"reason": f"AbstractorBot status is {t['abstractor_status']}; "
                                                     "closing requires TITLE_VERIFIED_CLEAN"}, [], "CLOSING_BLOCKED_TITLE")
        card = t["deal_card"]
        mk = self.store.latest_market()
        if not mk or evaluate_kill_switch(mk["wti_usd"], mk["henry_hub_usd"], mk["permit_count_current"],
                                          mk["permit_count_baseline"], self.settings)[0]:
            return WorkResult("BLOCKED", {"reason": "cyclicality kill-switch is ON"}, [], "CLOSING_BLOCKED_KILL_SWITCH")
        pr, lease = card["pricing"], t["lease_info"] or {}
        owners = self.store.select("owners", {"tract_id": tid})
        owner = owners[0] if owners else {}
        need = ["COMPANY_NAME", "COMPANY_SIGNER", "COMPANY_ADDRESS", "ESCROW_AGENT_NAME", "ESCROW_AGENT_ADDRESS"]
        absent = [k for k in need if not os.environ.get(k)]
        if absent:
            raise ClosingBlocked(f"set {absent} in the environment before generating packets")
        today = date.today()
        nma = t["nma"]
        ctx = {
            "tract_id": tid, "state": t["state"], "county": t["county"], "legal_description": t["legal_description"],
            "gross_acres": t["gross_acres"], "nma": f"{nma:.4f}", "lessor_name": lease.get("lessor") or owner.get("name"),
            "owner_name": owner.get("name"), "owner_address": owner.get("mailing_address") or "address on file",
            "lease_recording": lease.get("recording"), "lease_effective_date": lease.get("effective_date"),
            "lease_primary_term_months": lease.get("primary_term_months"), "lease_expiration_date": lease.get("expiration_date"),
            "acquisition_per_nma": f"{pr['acquisition_per_nma']:.2f}", "acquisition_total": f"{pr['acquisition_per_nma'] * nma:,.2f}",
            "exit_per_nma": f"{pr['exit_per_nma']['value'] if isinstance(pr['exit_per_nma'], dict) else pr['exit_per_nma']:.2f}",
            "exit_total": f"{(pr['exit_per_nma']['value'] if isinstance(pr['exit_per_nma'], dict) else pr['exit_per_nma']) * nma:,.2f}",
            "orri_pct": f"{pr['orri_pct'] * 100:.1f}%", "buyer_name": p.get("buyer_name") or pr.get("buyer_name") or "Buyer (see LOI)",
            "buyer_contact": p.get("buyer_contact", "VP of Land"),
            "company_name": os.environ["COMPANY_NAME"], "company_signer": os.environ["COMPANY_SIGNER"],
            "company_address": os.environ["COMPANY_ADDRESS"], "escrow_agent_name": os.environ["ESCROW_AGENT_NAME"],
            "escrow_agent_address": os.environ["ESCROW_AGENT_ADDRESS"], "today": today.isoformat(),
            "option_fee": "$10.00", "option_days": p.get("option_days", 30),
            "closing_date": (today + timedelta(days=int(p.get("option_days", 30)))).isoformat(),
            "docket_no": p.get("docket_no", "see docket feed"),
            "comps_table": "",
        }
        v = lambda x: x["value"] if isinstance(x, dict) else x
        ctx["comps_table"] = "\n".join(f"- {c['comp_id']}: ${v(c['price_per_acre']):.2f}/ac (grade {c['grade']})" for c in card["comps"])
        expired = lease.get("status_vs_today") == "EXPIRED"
        files = ["top_lease_option.md", "affidavit_non_production.md", "assignment_with_orri.md",
                 "escrow_instructions.md", "operator_pitch_packet.md", "owner_outreach_letters.md"]
        outdir = self.packet_dir / tid
        outdir.mkdir(parents=True, exist_ok=True)
        written = []
        for f in files:
            (outdir / f).write_text(render_template(f, {**ctx, "outreach_mode": "POST_EXPIRATION" if expired else "PRE_EXPIRATION"}))
            written.append(str(outdir / f))
        self.store.update_tract(tid, status="PACKETS_GENERATED")
        return WorkResult("COMPLETE", {"packet_dir": str(outdir), "files": written}, [], "PACKETS_GENERATED")

    def execute(self, wo: dict) -> WorkResult:
        return {"MARKET_CHECK": self._market_check, "UNDERWRITE": self._underwrite,
                "ASSEMBLE_CLOSING_PACKET": self._packet}[wo["task_type"]](wo)


if __name__ == "__main__":
    run_agent(CFOWorker)
