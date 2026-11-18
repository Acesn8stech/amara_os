"""Machine 4 — TraceBot: owner resolution waterfall + per-tract hard-cost ledger."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol

from db.store import Store, new_id, now_iso

from .base_worker import CHANNELS, BaseWorker, WorkResult, run_agent

EXPENSE_CATEGORIES = ("COPIES", "RECORDING_FEE", "SKIP_TRACE", "MILEAGE", "ABSTRACTOR_FEE", "POSTAGE", "OTHER")


# ------------------------------------------------------------------ cost ledger (module-level: COS uses it too)
def record_expense(store: Store, tract_id: str, category: str, amount_cents: int, description: str,
                   receipt_ref: str | None = None, incurred_by: str = "trace-bot") -> str:
    if category not in EXPENSE_CATEGORIES:
        raise ValueError(f"category must be one of {EXPENSE_CATEGORIES}")
    if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents < 0:
        raise ValueError("amount_cents must be a non-negative integer (cents)")
    if not description.strip():
        raise ValueError("every expense needs a description")
    eid = new_id("ex_")
    store.insert("tract_expenses", expense_id=eid, tract_id=tract_id, category=category, amount_cents=amount_cents,
                 description=description.strip(), receipt_ref=receipt_ref, incurred_by=incurred_by, incurred_at=now_iso())
    store.audit(incurred_by, "EXPENSE", tract_id, category=category, amount_cents=amount_cents, receipt_ref=receipt_ref)
    return eid


def ledger_summary(store: Store, tract_id: str) -> dict:
    rows = store.select("tract_expenses", {"tract_id": tract_id})
    by_cat: dict[str, int] = {}
    for r in rows:
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + r["amount_cents"]
    return {"tract_id": tract_id, "total_cents": sum(by_cat.values()), "total_usd": sum(by_cat.values()) / 100,
            "by_category_cents": by_cat, "entries": len(rows),
            "unreceipted_entries": sum(1 for r in rows if not r["receipt_ref"])}


# ------------------------------------------------------------------ owner sources
class OwnerSource(Protocol):
    def tax_roll(self, state: str, county: str, name: str) -> dict | None: ...
    def probate(self, state: str, county: str, name: str) -> dict | None: ...
    def corp_registry(self, state: str, name: str) -> dict | None: ...
    def skip_trace(self, provider: str, name: str, hint: dict) -> dict | None: ...


class StaticOwnerSource:
    """Dict-backed source: {"tax_roll": {NAME: rec}, "probate": {...}, "corp": {...}, "skip": {provider: {...}}}."""

    def __init__(self, data: dict):
        self.d = data

    def tax_roll(self, state, county, name):
        return self.d.get("tax_roll", {}).get(name.upper())

    def probate(self, state, county, name):
        return self.d.get("probate", {}).get(name.upper())

    def corp_registry(self, state, name):
        return self.d.get("corp", {}).get(name.upper())

    def skip_trace(self, provider, name, hint):
        return self.d.get("skip", {}).get(provider, {}).get(name.upper())


class JsonOwnerSource(StaticOwnerSource):
    """Normalized dump from county tax-roll / probate / SOS / vendor adapters (JSON file)."""

    def __init__(self, path: str):
        super().__init__(json.loads(Path(path).read_text()))


class TraceWorker(BaseWorker):
    agent_id = "trace-bot"
    channel = CHANNELS["owner"]
    prompt_file = "04_trace_bot.md"
    tasks = ("TRACE_OWNERS",)

    def __init__(self, *args, source: OwnerSource | None = None, **kw):
        super().__init__(*args, **kw)
        self.source = source or JsonOwnerSource(os.environ.get("OWNER_FEED", "owner_feed.json"))

    def _book_fee(self, tract_id: str, rec: dict, category: str, label: str) -> None:
        if rec.get("fee_cents"):
            record_expense(self.store, tract_id, category, int(rec["fee_cents"]), f"{label}: {rec.get('name')}",
                           rec.get("receipt_ref"), self.agent_id)

    def resolve(self, tract_id: str, state: str, county: str, name: str) -> tuple[dict | None, str | None]:
        """Waterfall; returns (record, resolution_path)."""
        rec = self.source.tax_roll(state, county, name)
        if rec:
            self._book_fee(tract_id, rec, "COPIES", "tax roll lookup")
            if rec.get("deceased"):
                pr = self.source.probate(state, county, name)
                if pr:
                    self._book_fee(tract_id, pr, "COPIES", "probate docket copies")
                    return {**rec, **pr, "deceased": True}, "TAX_ROLL+PROBATE"
            return rec, "TAX_ROLL"
        for label, fn in (("PROBATE", lambda: self.source.probate(state, county, name)),
                          ("CORP_REGISTRY", lambda: self.source.corp_registry(state, name))):
            rec = fn()
            if rec:
                self._book_fee(tract_id, rec, "COPIES", label.lower())
                return rec, label
        for provider in self.settings.skip_trace_providers:
            rec = self.source.skip_trace(provider, name, {"state": state, "county": county})
            if rec:
                self._book_fee(tract_id, rec, "SKIP_TRACE", f"skip trace via {provider}")
                return rec, f"SKIP_TRACE:{provider}"
        return None, None

    def execute(self, wo: dict) -> WorkResult:
        tid, p = wo["tract_id"], wo["payload"]
        owners_out, claims, corroborate = [], [], []
        for o in p["owners"]:
            rec, path = self.resolve(tid, p["state"], p["county"], o["name"])
            ev = (rec or {}).get("evidence")
            if not rec:
                status = "UNLOCATED"
            elif rec.get("deceased"):
                status = "DECEASED"
            else:
                status = "LOCATED"
            addr = (rec or {}).get("mailing_address")
            if path and path.startswith("SKIP_TRACE"):
                corroborate.append(o["name"])
            self.store.insert("owners", owner_id=new_id("ow_"), tract_id=tid, name=o["name"], role=o.get("role", "UNLEASED_MINERAL"),
                              status=status, deceased=int(status == "DECEASED"), probate_ref=(rec or {}).get("probate_ref"),
                              mailing_address=addr, phone=(rec or {}).get("phone"), resolution_path=path,
                              evidence=ev, created_at=now_iso())
            owners_out.append({"name": o["name"], "status": status, "resolution_path": path, "mailing_address": addr,
                               "probate_ref": (rec or {}).get("probate_ref"), "heirs": (rec or {}).get("heirs", []),
                               "evidence": ev})
            claims += [self.claim(f"owner.{o['name']}.name", o["name"] if rec else None, ev),
                       self.claim(f"owner.{o['name']}.mailing_address", addr, ev)]
            if status == "DECEASED":
                claims.append(self.claim(f"owner.{o['name']}.probate_ref", (rec or {}).get("probate_ref"), ev))
        located = [o for o in owners_out if o["status"] != "UNLOCATED"]
        outcome = ("OWNERS_RESOLVED" if len(located) == len(owners_out) else
                   "OWNERS_PARTIAL" if located else "OWNERS_UNLOCATED")
        led = ledger_summary(self.store, tid)
        self.store.update_tract(tid, status=self.store.get_tract(tid)["status"])  # touch updated_at
        return WorkResult("COMPLETE", {"owners": owners_out, "needs_corroboration": corroborate, "ledger": led}, claims, outcome)


if __name__ == "__main__":
    run_agent(TraceWorker)
