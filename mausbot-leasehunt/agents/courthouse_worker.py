"""Machine 2 — CourthouseBot: county clerk index pre-screen for expiring primary terms."""
from __future__ import annotations

import calendar
import json
import os
from datetime import date
from pathlib import Path
from typing import Callable, Protocol

from .base_worker import CHANNELS, BaseWorker, WorkResult, run_agent

LEASE_TYPES = {"OIL_GAS_LEASE", "MEMORANDUM_OF_LEASE"}
# Any later instrument of these types means the lease may be extended/held: pre-screen FAIL.
DISQUALIFIER_TYPES = {"EXTENSION", "RATIFICATION", "AMENDMENT", "SHUT_IN", "UNIT_DESIGNATION",
                      "POOLING_DESIGNATION", "ACTIVE_PRODUCTION", "HBP_AFFIDAVIT", "DIVISION_ORDER"}
RELEASE_TYPES = {"LEASE_RELEASE", "AFFIDAVIT_NON_PRODUCTION"}
AGE_WINDOWS_MONTHS = ((30, 42), (54, 66))   # 3-yr and 5-yr primary terms
EXPIRY_BAND_MONTHS = (-12, 12)              # expiration must fall within this band around today


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def months_between(a: date, b: date) -> int:
    """Whole months from a to b (b later)."""
    m = (b.year - a.year) * 12 + b.month - a.month
    return m - (1 if b.day < a.day else 0)


class ClerkIndex(Protocol):
    def search(self, state: str, county: str, legal_description: str) -> list[dict]: ...


class StaticClerkIndex:
    def __init__(self, records_by_tract: dict[str, list[dict]]):
        self.records = records_by_tract

    def search(self, state, county, legal_description):
        return self.records.get(f"{state}:{county}:{legal_description}", self.records.get(legal_description, []))


class JsonClerkIndex:
    """Reads normalized clerk grantor/grantee results from a JSON file or https URL
    ({"<state>:<county>:<legal>": [instrument, ...]}). County-specific portal adapters emit this."""

    def __init__(self, location: str):
        self.location = location

    def search(self, state, county, legal_description):
        if self.location.startswith("http"):
            import httpx
            r = httpx.get(self.location, params={"state": state, "county": county, "legal": legal_description}, timeout=60)
            r.raise_for_status()
            return r.json()
        return json.loads(Path(self.location).read_text()).get(f"{state}:{county}:{legal_description}", [])


def book_page(r: dict) -> str | None:
    return f"Book {r['book']} Page {r['page']}" if r.get("book") and r.get("page") else None


def rec_evidence(r: dict) -> dict | None:
    ev = {"grade": "A" if r.get("imaged_viewed") else "B"}
    if book_page(r):
        ev["book_page"] = book_page(r)
    if r.get("instrument_no"):
        ev["instrument_no"] = r["instrument_no"]
    if r.get("source_url"):
        ev["source_url"] = r["source_url"]
    return ev if ("book_page" in ev or "source_url" in ev) else None


class CourthouseWorker(BaseWorker):
    agent_id = "courthouse-bot"
    channel = CHANNELS["title_exp"]
    prompt_file = "02_courthouse_bot.md"
    tasks = ("COURTHOUSE_SCREEN",)

    def __init__(self, *args, index: ClerkIndex | None = None, today: Callable[[], date] = date.today, **kw):
        super().__init__(*args, **kw)
        self.index = index or JsonClerkIndex(os.environ.get("CLERK_FEED", "clerk_feed.json"))
        self.today = today

    def execute(self, wo: dict) -> WorkResult:
        p = wo["payload"]
        tid, today = wo["tract_id"], self.today()
        records = self.index.search(p["state"], p["county"], p["legal_description"])
        leases = [r for r in records if r.get("type") in LEASE_TYPES and r.get("effective_date")]
        searched = [{"instrument_id": r.get("instrument_id"), "type": r.get("type"), "book_page": book_page(r)} for r in records]
        candidates, rejected = [], []
        for ls in leases:
            eff = date.fromisoformat(ls["effective_date"])
            age = months_between(eff, today)
            if not any(lo <= age <= hi for lo, hi in AGE_WINDOWS_MONTHS):
                rejected.append({"instrument_id": ls.get("instrument_id"), "reason": f"AGE_{age}_MO_OUTSIDE_WINDOWS"})
                continue
            term = ls.get("primary_term_months")
            if not term:
                rejected.append({"instrument_id": ls.get("instrument_id"), "reason": "PRIMARY_TERM_UNKNOWN"})
                continue
            exp = add_months(eff, int(term))
            lo, hi = EXPIRY_BAND_MONTHS
            if not (add_months(today, lo) <= exp <= add_months(today, hi)):
                rejected.append({"instrument_id": ls.get("instrument_id"), "reason": f"EXPIRATION_{exp}_OUTSIDE_BAND"})
                continue
            bad = [r for r in records if r is not ls and r.get("type") in DISQUALIFIER_TYPES | RELEASE_TYPES
                   and r.get("refers_to") in (None, ls.get("instrument_id"))
                   and (r.get("instrument_date") or "9999") >= ls["effective_date"]]
            if bad:
                rejected.append({"instrument_id": ls.get("instrument_id"), "reason": "DISQUALIFIED",
                                 "disqualifiers": [{"type": b["type"], "book_page": book_page(b),
                                                    "instrument_id": b.get("instrument_id")} for b in bad]})
                continue
            candidates.append((exp, ls, age))
        base = {"searched": searched, "rejected": rejected, "searched_at": today.isoformat()}
        if not candidates:
            self.store.audit(self.agent_id, "SCREEN_FAIL", tid, rejected=len(rejected), records=len(records))
            return WorkResult("COMPLETE", base, [], outcome="FAIL")
        exp, ls, age = min(candidates, key=lambda c: c[0])
        ev = rec_evidence(ls)
        lease = {"instrument_id": ls.get("instrument_id"), "recording": book_page(ls), "effective_date": ls["effective_date"],
                 "primary_term_months": int(ls["primary_term_months"]), "expiration_date": exp.isoformat(),
                 "lessor": ls.get("lessor"), "lessee": ls.get("lessee"), "age_months": age,
                 "window": "30-42" if age <= 42 else "54-66", "status_vs_today": "EXPIRED" if exp < today else "EXPIRING"}
        claims = [self.claim("lease.recording", lease["recording"], ev),
                  self.claim("lease.effective_date", lease["effective_date"], ev),
                  self.claim("lease.primary_term_months", lease["primary_term_months"], ev),
                  self.claim("lease.expiration_date", lease["expiration_date"], ev),
                  self.claim("lease.lessor", lease["lessor"], ev)]
        self.store.update_tract(tid, lease_info=lease) if self.store.get_tract(tid) else None
        self.store.audit(self.agent_id, "SCREEN_PASS", tid, expiration=lease["expiration_date"])
        return WorkResult("COMPLETE", {**base, "lease": lease}, claims, outcome="PASS")


if __name__ == "__main__":
    run_agent(CourthouseWorker)
