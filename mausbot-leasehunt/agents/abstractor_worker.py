"""Machine 3 — AbstractorBot: runs sheets, exact-fraction NMA math, curative defects, field tickets."""
from __future__ import annotations

import json
import os
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Protocol

from jsonschema import Draft7Validator

from db.store import new_id, now_iso

from .courthouse_worker import add_months
from .base_worker import (CHANNELS, GRADE_RANK, BaseWorker, WorkResult, min_grade, render_envelope, run_agent, ROOT)

RUNS_SHEET_VALIDATOR = Draft7Validator(json.loads((ROOT / "schemas" / "runs_sheet.schema.json").read_text()))

# Planning estimates (USD) used to price curative work into the spread. Override per deployment
# with CURATIVE_COST_TABLE='{"MISSING_PROBATE": 4200, ...}'. These are estimates, never cited as facts.
CURATIVE_COSTS = {"MISSING_PROBATE": 3500, "OPEN_MORTGAGE": 750, "UNRELEASED_LEASE": 150,
                  "MISSING_HOMESTEAD_JOINDER": 1200, "CHAIN_GAP": 2500, "CHAIN_NOT_FROM_SOVEREIGN": 1500,
                  "OVERCONVEYANCE": 2000}
FIELD_TICKET_FEE_USD = 350
CONVEYANCES = {"WARRANTY_DEED", "MINERAL_DEED"}
NPRI_TYPES = {"ROYALTY_DEED", "RESERVATION"}
PROBATE_TYPES = {"PROBATE_ORDER", "AFFIDAVIT_HEIRSHIP"}


def _norm(n: str | None) -> str:
    return (n or "").strip().upper()


def _frac(s: str) -> Fraction:
    return Fraction(s)


def curative_table() -> dict[str, float]:
    t = dict(CURATIVE_COSTS)
    t.update(json.loads(os.environ.get("CURATIVE_COST_TABLE", "{}")))
    return t


def entry_grade(e: dict) -> str:
    ev = e.get("evidence")
    if not ev or ev.get("grade") not in GRADE_RANK:
        return "D"
    return ev["grade"]


def compute_title(sheet: dict, imaging_start_year: int, today: date | None = None) -> dict:
    """Pure function: Runs Sheet -> holders, NMA, defects, field tickets, status. Exact fractions."""
    today = today or date.today()
    gross = float(sheet["gross_acres"])
    closing_requirements: list[dict] = []
    subject = _norm(sheet["subject_owner"])
    entries = sorted(sheet["entries"], key=lambda e: (e["instrument_date"], e["seq"]))
    mineral: dict[str, Fraction] = {}
    npri: dict[str, Fraction] = {}
    leases: dict[str, dict] = {}
    mortgages: dict[str, dict] = {}
    deceased: dict[str, str] = {}        # name -> death date
    probated: set[str] = set()
    defects: list[dict] = []
    tickets: list[dict] = []
    county = sheet["county"]

    def defect(code: str, e: dict | None, detail: str) -> None:
        defects.append({"code": code, "instrument_id": e["instrument_id"] if e else None, "detail": detail})

    if not entries or entries[0]["entry_type"] != "SOVEREIGN_PATENT":
        defect("CHAIN_NOT_FROM_SOVEREIGN", entries[0] if entries else None,
               "first instrument of record is not a sovereign patent/grant")

    for e in entries:
        t, grantor, grantee = e["entry_type"], _norm(e.get("grantor")), _norm(e.get("grantee"))
        interest = e.get("interest") or {"type": "MINERAL", "fraction": "1", "basis": "OF_GRANTOR_INTEREST"}
        f = _frac(interest["fraction"])
        if t == "SOVEREIGN_PATENT":
            if mineral:
                defect("CHAIN_GAP", e, "second sovereign patent for the same tract")
            mineral[grantee] = mineral.get(grantee, Fraction(0)) + f
        elif t in CONVEYANCES or t in PROBATE_TYPES:
            held = mineral.get(grantor, Fraction(0))
            amount = f * held if interest["basis"] == "OF_GRANTOR_INTEREST" else f
            if t in PROBATE_TYPES:
                probated.add(grantor)
            if held == 0:
                defect("CHAIN_GAP", e, f"{e.get('grantor')} holds no mineral interest of record to convey")
                continue
            if amount > held:
                defect("OVERCONVEYANCE", e, f"conveys {amount} but grantor holds {held}")
                amount = held
            mineral[grantor] = held - amount
            mineral[grantee] = mineral.get(grantee, Fraction(0)) + amount
            if t in CONVEYANCES and e.get("homestead") and not e.get("spouse_joined"):
                defect("MISSING_HOMESTEAD_JOINDER", e, f"{e.get('grantor')} conveyed homestead without spouse joinder")
        elif t in NPRI_TYPES:
            if t == "ROYALTY_DEED" and mineral.get(grantor, Fraction(0)) == 0 and npri.get(grantor, Fraction(0)) > 0:
                amount = f * npri[grantor] if interest["basis"] == "OF_GRANTOR_INTEREST" else f
                npri[grantor] -= min(amount, npri[grantor])
                npri[grantee] = npri.get(grantee, Fraction(0)) + amount
            else:   # carved out of the mineral estate (royalty deed) or reserved in a deed (RESERVATION)
                npri[grantee] = npri.get(grantee, Fraction(0)) + f
        elif t == "DEATH":
            deceased[_norm(e.get("grantor") or e.get("grantee"))] = e["instrument_date"]
        elif t == "OIL_GAS_LEASE":
            leases[e["instrument_id"]] = {"entry": e, "released": False}
        elif t in ("LEASE_RELEASE", "AFFIDAVIT_NON_PRODUCTION"):
            if e.get("releases") in leases:
                leases[e["releases"]]["released"] = True
        elif t == "MORTGAGE":
            mortgages[e["instrument_id"]] = {"entry": e, "released": False}
        elif t == "MORTGAGE_RELEASE":
            if e.get("releases") in mortgages:
                mortgages[e["releases"]]["released"] = True

        # ---- field-ticket triggers: cannot certify from a screen
        year = int(e["instrument_date"][:4])
        rec = e.get("recording") or {}
        reason = None
        if not rec.get("book") and not rec.get("instrument_no"):
            reason = "NO_RECORDING_REFERENCE"
        elif e.get("imaged_online") is False:
            reason = "NOT_IMAGED_ONLINE"
        elif year < imaging_start_year:
            reason = f"PRE_IMAGING_{imaging_start_year}"
        elif entry_grade(e) in ("C", "D"):
            reason = "EVIDENCE_GRADE_BELOW_B"
        if reason:
            tickets.append({"county": county, "instrument_id": e["instrument_id"], "book": rec.get("book"),
                            "page": rec.get("page"), "instrument_no": rec.get("instrument_no"),
                            "instrument_date": e["instrument_date"], "reason": reason,
                            "pull": "certified copy of recorded instrument + abstract of marginal references"})

    for name, dt in deceased.items():
        if name not in probated:
            defect("MISSING_PROBATE", None, f"{name.title()} died {dt}; no probate order/affidavit of heirship of record")
    for iid, m in mortgages.items():
        if not m["released"]:
            defect("OPEN_MORTGAGE", m["entry"], f"mortgage {iid} has no recorded release")
    for iid, l in leases.items():
        if l["released"]:
            continue
        e, term = l["entry"], l["entry"].get("primary_term_months")
        if term and add_months(date.fromisoformat(e["instrument_date"]), int(term)) <= today:
            # primary term over and nothing of record extends it: the affidavit of non-production is a
            # closing requirement (we record it), not a defect in the chain
            closing_requirements.append({"code": "RECORD_AFFIDAVIT_NON_PRODUCTION", "instrument_id": iid})
        else:
            defect("UNRELEASED_LEASE", e, f"lease {iid} has no release of record and its primary term is not shown to be over")

    held = mineral.get(subject, Fraction(0))
    nma = float(gross * held)
    table = curative_table()
    curative = sum(table.get(d["code"], 0) for d in defects) + FIELD_TICKET_FEE_USD * len(tickets)
    grade = min_grade(entry_grade(e) for e in entries)
    if tickets:
        status = "FIELD_TICKET_REQUIRED"
    elif defects:
        status = "TITLE_DEFECTS_FOUND"
    else:
        status = "TITLE_VERIFIED_CLEAN"
    vesting = [e for e in entries if _norm(e.get("grantee")) == subject and (e.get("interest") or {"type": "MINERAL"})["type"] == "MINERAL"]
    return {
        "gross_acres": gross, "subject_owner": sheet["subject_owner"],
        "holders": {k.title(): str(v) for k, v in mineral.items() if v},
        "subject_mineral_fraction": str(held), "nma": nma,
        "npri_holders": {k.title(): str(v) for k, v in npri.items() if v},
        "npri_royalty_fraction": str(sum(npri.values(), Fraction(0))),
        "note": "NPRI is a share of production, not mineral acreage; excluded from NMA.",
        "defects": defects, "field_tickets": tickets, "closing_requirements": closing_requirements, "curative_cost_usd": float(curative),
        "abstractor_status": status, "min_evidence_grade": grade,
        "vesting_entry": vesting[-1] if vesting else (entries[-1] if entries else None),
    }


class RunsSource(Protocol):
    def runs_sheet(self, tract: dict, lease_info: dict | None) -> dict: ...


class StaticRunsSource:
    def __init__(self, sheets: dict[str, dict]):
        self.sheets = sheets

    def runs_sheet(self, tract, lease_info):
        return self.sheets[tract["tract_id"]]


class JsonRunsSource:
    """Reads {tract_id: runs_sheet} from a JSON file / https URL produced by county adapters."""

    def __init__(self, location: str):
        self.location = location

    def runs_sheet(self, tract, lease_info):
        if self.location.startswith("http"):
            import httpx
            r = httpx.get(self.location, params={"tract_id": tract["tract_id"]}, timeout=60)
            r.raise_for_status()
            return r.json()
        return json.loads(Path(self.location).read_text())[tract["tract_id"]]


class AbstractorWorker(BaseWorker):
    agent_id = "abstractor-bot"
    channel = CHANNELS["abstract"]
    prompt_file = "03_abstractor_bot.md"
    tasks = ("ABSTRACT_TITLE", "FIELD_TICKET_REQUEST")

    def __init__(self, *args, source: RunsSource | None = None, today=date.today, **kw):
        super().__init__(*args, **kw)
        self.today = today
        self.source = source or JsonRunsSource(os.environ.get("RUNS_FEED", "runs_feed.json"))

    def closing_blocked(self, tract_id: str) -> bool:
        """True while CFOBot must NOT close/assemble (anything but TITLE_VERIFIED_CLEAN)."""
        t = self.store.get_tract(tract_id)
        return not t or t["abstractor_status"] != "TITLE_VERIFIED_CLEAN"

    def execute(self, wo: dict) -> WorkResult:
        tid = wo["tract_id"]
        tract = self.store.get_tract(tid)
        if wo["task_type"] == "FIELD_TICKET_REQUEST":
            return self._field_ticket_request(wo, tract)
        sheet = self.source.runs_sheet(tract, tract.get("lease_info"))
        sheet = dict(sheet, entries=list(sheet["entries"]) + list(wo["payload"].get("supplemental_entries", [])))
        errs = [e.message for e in RUNS_SHEET_VALIDATOR.iter_errors(sheet)]
        if errs:
            return WorkResult("FAILED", {"error": "runs sheet failed schema validation", "details": errs[:10]})
        imaging = int(sheet.get("imaging_start_year") or self.settings.default_imaging_start_year)
        res = compute_title(sheet, imaging, self.today())
        prev = self.store.scalar("SELECT COALESCE(MAX(version),0) FROM runs_sheets WHERE tract_id=:t", t=tid)
        self.store.insert("runs_sheets", id=new_id("rs_"), tract_id=tid, version=int(prev) + 1,
                          entries=sheet["entries"], nma_math={k: res[k] for k in
                          ("gross_acres", "holders", "subject_mineral_fraction", "nma", "npri_holders",
                           "npri_royalty_fraction", "note")},
                          abstractor_status=res["abstractor_status"], defects=res["defects"],
                          field_tickets=res["field_tickets"], min_evidence_grade=res["min_evidence_grade"],
                          created_at=now_iso())
        self.store.update_tract(tid, abstractor_status=res["abstractor_status"], nma=res["nma"],
                                gross_acres=res["gross_acres"], min_evidence_grade=res["min_evidence_grade"],
                                curative_cost_cents=round(res["curative_cost_usd"] * 100),
                                status="TITLE_ABSTRACTED" if res["abstractor_status"] != "FIELD_TICKET_REQUIRED"
                                else "AWAITING_FIELD_ABSTRACT")
        if res["field_tickets"]:
            self._post_tickets(tid, res["field_tickets"])
        ev = (res["vesting_entry"] or {}).get("evidence")
        claims = [self.claim("nma", res["nma"], ev), self.claim("subject_mineral_fraction", res["subject_mineral_fraction"], ev)]
        payload = {k: res[k] for k in ("holders", "subject_mineral_fraction", "nma", "gross_acres", "npri_holders",
                                       "npri_royalty_fraction", "defects", "field_tickets", "closing_requirements", "curative_cost_usd",
                                       "abstractor_status", "min_evidence_grade")}
        return WorkResult("COMPLETE", payload, claims, outcome=res["abstractor_status"])

    def _field_ticket_request(self, wo: dict, tract: dict) -> WorkResult:
        rs = self.store.select("runs_sheets", {"tract_id": tract["tract_id"]}, order_by="version DESC", limit=1)
        if not rs:
            return WorkResult("BLOCKED", {"error": "no runs sheet to build tickets from"})
        tickets = list(rs[0]["field_tickets"]) or [{
            "county": tract["county"], "instrument_id": e["instrument_id"], "book": (e.get("recording") or {}).get("book"),
            "page": (e.get("recording") or {}).get("page"), "instrument_date": e["instrument_date"],
            "reason": "PRINCIPAL_REQUESTED_PHYSICAL_VERIFICATION", "pull": "certified copy of recorded instrument"}
            for e in rs[0]["entries"]]
        self._post_tickets(tract["tract_id"], tickets)
        self.store.update_tract(tract["tract_id"], status="AWAITING_FIELD_ABSTRACT", abstractor_status="FIELD_TICKET_REQUIRED")
        return WorkResult("COMPLETE", {"field_tickets": tickets, "abstractor_status": "FIELD_TICKET_REQUIRED",
                                       "defects": rs[0]["defects"], "curative_cost_usd": len(tickets) * FIELD_TICKET_FEE_USD},
                          [], outcome="FIELD_TICKET_REQUIRED")

    def _post_tickets(self, tract_id: str, tickets: list[dict]) -> None:
        body = {"alert": "COURTHOUSE_FIELD_TICKET", "tract_id": tract_id, "tickets": tickets,
                "next": "contract title abstractor pulls physical books; send results as supplemental_entries"}
        self.limiter.acquire()
        self.transport.post(self.channel, f":clipboard: *COURTHOUSE_FIELD_TICKET* {tract_id} — {len(tickets)} instrument(s) to pull\n"
                            f"```\n{json.dumps(body, indent=1, default=str)}\n```")


if __name__ == "__main__":
    run_agent(AbstractorWorker)
