#!/usr/bin/env python3
"""Gate G1 helper: print N random permit->frac joins so a human can check them by hand.

Each line gives enough to look the well up on the RRC and FracFocus sites: API, permit
number, lease name, operator, county, permit date, frac start, water, proppant.
Usage: sample_join.py --permits permits.csv --frac frac.csv [--county REEVES] [-n 10] [--seed 1]
"""
import argparse
import importlib.util
import random
from pathlib import Path

spec = importlib.util.spec_from_file_location("ff", Path(__file__).resolve().parent / "frac_forecast.py")
ff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ff)


def sample(permit_rows, frac_rows, county=None, n=10, seed=1):
    permits, _ = ff.clean_permits(permit_rows)
    extras = {}
    for r in permit_rows:                       # keep the human-readable extras for display
        key = ff.api10(r.get("api"))
        if key:
            extras.setdefault(key, r)
    if county:
        permits = {k: v for k, v in permits.items() if v["county"] == county.strip().upper()}
    fracs, _ = ff.clean_fracs(frac_rows)
    joined, report = ff.join(permits, fracs)
    pick = random.Random(seed).sample(joined, min(n, len(joined)))
    return [dict(j, permit_number=extras.get(j["api"], {}).get("permit_number", ""),
                 lease_name=extras.get(j["api"], {}).get("lease_name", "")) for j in pick], report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--permits", required=True)
    ap.add_argument("--frac", required=True)
    ap.add_argument("--county")
    ap.add_argument("-n", type=int, default=10)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args(argv)
    rows, report = sample(ff.read_csv(a.permits), ff.read_csv(a.frac), a.county, a.n, a.seed)
    print(f"JOIN REPORT: {report}\n")
    for r in rows:
        print(f"API {r['api']} | permit# {r['permit_number']} | lease {r['lease_name']} | {r['operator']} | "
              f"{r['county']} | permitted {r['permit_date']} | frac {r['frac_start']} (+{r['lag_days']}d) | "
              f"water {r['water_gal']} gal | proppant {r['proppant_lb']} lb")
    return 0 if rows else 2


if __name__ == "__main__":
    raise SystemExit(main())
