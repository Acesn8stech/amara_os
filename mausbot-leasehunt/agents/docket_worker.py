"""Machine 1 — DocketBot: commission dockets, spacing/pooling applications, APDs."""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Callable, Protocol

from db.store import now_iso

from .base_worker import CHANNELS, BaseWorker, WorkResult, run_agent

# Public docket / permit search entry points, used as evidence URL bases and for operators.
REGULATOR_PORTALS = {
    "TX_RRC": "https://www.rrc.texas.gov/hearings/",
    "OK_OCC": "https://oklahoma.gov/occ/divisions/oil-gas/docket.html",
    "CO_ECMC": "https://ecmc.colorado.gov/",
    "ND_DMR": "https://www.dmr.nd.gov/oilgas/",
}
PLSS_STATES = {"OK", "CO", "ND", "NM"}  # Texas uses abstracts/surveys, not PLSS


class DocketSource(Protocol):
    def fetch(self, state: str, regulator: str, since: str | None) -> list[dict]: ...


class StaticDocketSource:
    """In-memory normalized filings (tests, replays)."""

    def __init__(self, filings: list[dict]):
        self.filings = filings

    def fetch(self, state, regulator, since):
        return [f for f in self.filings if f.get("state") == state and f.get("regulator", regulator) == regulator]


class JsonFeedDocketSource:
    """Reads normalized filings (see README 'Docket feed format') from a JSON file path or an
    https URL. Regulator-specific portal scrapers should emit this format; keeping the scraper
    separate from the screening rules lets each portal adapter be swapped without touching them."""

    def __init__(self, location: str):
        self.location = location

    def fetch(self, state, regulator, since):
        if self.location.startswith("http"):
            import httpx
            r = httpx.get(self.location, params={"state": state, "regulator": regulator, "since": since}, timeout=60)
            r.raise_for_status()
            data = r.json()
        else:
            data = json.loads(Path(self.location).read_text())
        return [f for f in data if f.get("state") == state and f.get("regulator") == regulator]


# ------------------------------------------------------------------ PLSS geometry
def _section_rc(n: int) -> tuple[int, int]:
    """Section number -> (row from north 0-5, col from west 0-5); boustrophedon numbering."""
    row = (n - 1) // 6
    off = (n - 1) % 6
    return row, (5 - off if row % 2 == 0 else off)


def _to_xy(sec: int, twp: int, ns: str, rng: int, ew: str) -> tuple[int, int]:
    row, col = _section_rc(sec)
    y = (twp - 1) * 6 + (5 - row) if ns == "N" else -twp * 6 + (5 - row)
    x = (rng - 1) * 6 + col if ew == "E" else -rng * 6 + col
    return x, y


def _from_xy(x: int, y: int) -> tuple[int, int, str, int, str]:
    if x >= 0:
        ew, rng, col = "E", x // 6 + 1, x % 6
    else:
        rng = (-x - 1) // 6 + 1
        ew, col = "W", x + 6 * rng
    if y >= 0:
        ns, twp, yl = "N", y // 6 + 1, y % 6
    else:
        twp = (-y - 1) // 6 + 1
        ns, yl = "S", y + 6 * twp
    row = 5 - yl
    sec = row * 6 + (6 - col if row % 2 == 0 else col + 1)
    return sec, twp, ns, rng, ew


def halo_sections(section: int, township: str, range_: str, radius_miles: int = 2) -> list[dict]:
    """All PLSS sections within `radius_miles` (Chebyshev, 1 section = 1 mile) of the given section,
    excluding the section itself. township like '5N', range like '6W'. Handles township/range crossings."""
    tm, rm = re.fullmatch(r"(\d+)([NS])", township.upper()), re.fullmatch(r"(\d+)([EW])", range_.upper())
    if not tm or not rm:
        raise ValueError(f"bad township/range: {township} {range_}")
    x0, y0 = _to_xy(section, int(tm[1]), tm[2], int(rm[1]), rm[2])
    out = []
    for dx in range(-radius_miles, radius_miles + 1):
        for dy in range(-radius_miles, radius_miles + 1):
            if dx == 0 and dy == 0:
                continue
            s, t, ns, r, ew = _from_xy(x0 + dx, y0 + dy)
            out.append({"section": s, "township": f"{t}{ns}", "range": f"{r}{ew}"})
    return out


def _slug(*parts: str) -> str:
    return "-".join(re.sub(r"[^A-Z0-9]+", "", str(p).upper()) for p in parts if p)


class DocketWorker(BaseWorker):
    agent_id = "docket-bot"
    channel = CHANNELS["docket"]
    prompt_file = "01_docket_bot.md"
    tasks = ("DOCKET_SCAN",)

    def __init__(self, *args, source: DocketSource | None = None,
                 today: Callable[[], date] = date.today, **kw):
        super().__init__(*args, **kw)
        self.source = source or JsonFeedDocketSource(
            __import__("os").environ.get("DOCKET_FEED", "docket_feed.json"))
        self.today = today

    def _evidence(self, f: dict) -> dict | None:
        ev = {"grade": "A" if f.get("imaged") else "B"}
        if f.get("source_url"):
            ev["source_url"] = f["source_url"]
        if f.get("docket_no"):
            ev["docket_no"] = f["docket_no"]
        return ev if len(ev) > 1 else None

    def execute(self, wo: dict) -> WorkResult:
        p = wo["payload"]
        state, regulator = p["state"], p["regulator"]
        filings = self.source.fetch(state, regulator, p.get("since"))
        tracts, claims, permits = [], [], 0
        self.unresolved_halos = []
        for f in filings:
            kind = f.get("kind", "").upper()
            ev = self._evidence(f)
            if kind in ("APD", "PERMIT"):
                permits += 1
                tracts += self._halo_candidates(f, ev, claims)
            elif kind in ("SPACING", "POOLING", "UNIT"):
                tracts += self._unit_candidates(f, ev, claims)
        self.store.audit(self.agent_id, "DOCKET_SCAN", wo["tract_id"], state=state, regulator=regulator,
                         filings=len(filings), candidates=len(tracts))
        return WorkResult("COMPLETE", {"tracts": tracts, "permit_counts": {state: permits},
                                       "portal": REGULATOR_PORTALS.get(regulator),
                                       "unresolved_halos": self.unresolved_halos, "scanned_at": now_iso()},
                          claims, outcome=f"{len(tracts)}_CANDIDATES")

    def _unit_candidates(self, f: dict, ev: dict | None, claims: list) -> list[dict]:
        """Sweet spot: unleased tract in a proposed unit with NO pooling order entered yet."""
        if f.get("order_entered"):
            return []
        hearing = f.get("hearing_date")
        if not hearing or date.fromisoformat(hearing) <= self.today():
            return []
        out = []
        for t in f.get("tracts", []):
            if t.get("leased") is not False:   # unknown or leased -> not a verified sweet spot
                continue
            tid = _slug(f["state"], f["county"], f.get("section"), f.get("township"), f.get("range"), t["tract_ref"])
            c = {"tract_id": tid, "state": f["state"], "county": f["county"], "section": f.get("section"),
                 "township": f.get("township"), "range": f.get("range"),
                 "legal_description": f"{t['tract_ref']}, Sec {f.get('section')}-{f.get('township')}-{f.get('range')}, {f['county']} County",
                 "gross_acres": t.get("gross_acres"), "trigger": "UNIT_SWEET_SPOT", "docket_no": f.get("docket_no"),
                 "hearing_date": hearing, "operator": f.get("operator"), "source_url": f.get("source_url")}
            out.append(c)
            claims += [self.claim(f"{tid}.gross_acres", t.get("gross_acres"), ev),
                       self.claim(f"{tid}.hearing_date", hearing, ev),
                       self.claim(f"{tid}.docket_no", f.get("docket_no"), ev),
                       self.claim(f"{tid}.operator", f.get("operator"), ev)]
        return out

    def _halo_candidates(self, f: dict, ev: dict | None, claims: list) -> list[dict]:
        if f["state"] not in PLSS_STATES:
            # No PLSS grid (e.g. Texas abstracts/surveys): never guess a halo; surface it for GIS follow-up.
            self.unresolved_halos.append({"docket_no": f.get("docket_no"), "halo_method": "ABSTRACT_GIS_REQUIRED"})
            return []
        out = []
        for h in halo_sections(int(f["section"]), f["township"], f["range"], 2):
            tid = _slug(f["state"], f["county"], h["section"], h["township"], h["range"], "ALL")
            out.append({"tract_id": tid, "state": f["state"], "county": f["county"], "section": str(h["section"]),
                        "township": h["township"], "range": h["range"], "gross_acres": 640.0,
                        "legal_description": f"All of Sec {h['section']}-{h['township']}-{h['range']}, {f['county']} County (2-mi halo of {f.get('docket_no')})",
                        "trigger": "APD_HALO", "docket_no": f.get("docket_no"), "source_url": f.get("source_url")})
            claims.append(self.claim(f"{tid}.docket_no", f.get("docket_no"), ev))
        return out


if __name__ == "__main__":
    run_agent(DocketWorker)
