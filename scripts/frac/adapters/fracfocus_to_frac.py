#!/usr/bin/env python3
"""FracFocus CSV download -> frac.csv for frac_forecast.py. Streams the zip; never extracts it.

One output row per job: api, frac_start, operator, water_gal, proppant_lb.
  water_gal   = TotalBaseWaterVolume (gallons, per the FracFocus data dictionary)
  proppant_lb = sum of MassIngredient (pounds) over the job's rows whose Purpose
                contains "proppant". Blank, never 0, when the job has no proppant mass.
FracFocus data may not be altered; this only aggregates.

Column names come from the Nov 2023 data dictionary quoted in the download readme. The
file header is CHECKED against them and the run stops with a clear error on a mismatch.

Usage: fracfocus_to_frac.py --zip ~/frac-data/fracfocus/FracFocusCSV.zip --output frac.csv
       fracfocus_to_frac.py --zip ... --list-purposes      (print Purpose values containing 'ropp')
"""
import argparse
import csv
import io
import json
import re
import sys
import zipfile
from collections import defaultdict
from datetime import datetime

csv.field_size_limit(10 * 1024 * 1024)

REQUIRED = ["APINumber", "OperatorName", "JobStartDate", "TotalBaseWaterVolume",
            "Purpose", "MassIngredient", "StateName"]
JOB_KEY_CANDIDATES = ["UploadKey", "DisclosureKey", "DisclosureId"]
DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]
FIELDS = ["api", "frac_start", "operator", "water_gal", "proppant_lb"]


def parse_date(value):
    text = (value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def num(value):
    try:
        x = float(str(value).replace(",", "").strip())
        return x if x == x and abs(x) != float("inf") else None
    except (ValueError, TypeError):
        return None


def is_proppant(purpose):
    return "proppant" in (purpose or "").lower()


def registry_members(zf):
    return sorted(n for n in zf.namelist() if re.search(r"FracFocusRegistry_\d+\.csv$", n))


def open_text(zf, name):
    return io.TextIOWrapper(zf.open(name), encoding="utf-8", errors="replace", newline="")


def check_header(header, job_key=None):
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        raise SystemExit(f"ERROR: FracFocus header is missing expected columns {missing}.\n"
                         f"Header found: {header}\nThe dictionary may have changed; stop and review.")
    key = job_key
    if key:
        if key not in header:
            raise SystemExit(f"ERROR: --job-key {key!r} is not in the header: {header}")
        return key
    for cand in JOB_KEY_CANDIDATES:
        if cand in header:
            return cand
    raise SystemExit("ERROR: no job-key column found (tried "
                     f"{JOB_KEY_CANDIDATES}). Pass --job-key NAME. Header: {header}")


def aggregate(rows, header, job_key, state="TEXAS", county=None, stats=None, jobs=None):
    """Fold ingredient rows into per-job aggregates (mutates jobs/stats)."""
    stats = stats if stats is not None else defaultdict(int)
    jobs = jobs if jobs is not None else {}
    idx = {name: i for i, name in enumerate(header)}
    g = lambda row, name: row[idx[name]] if idx[name] < len(row) else ""
    for row in rows:
        stats["ingredient_rows"] += 1
        if g(row, "StateName").strip().upper() != state:
            stats["skipped_not_texas"] += 1
            continue
        if county and ("CountyName" not in idx or g(row, "CountyName").strip().upper() != county):
            stats["skipped_other_county"] += 1
            continue
        key = g(row, job_key)
        if not key:
            stats["rejected_no_job_key"] += 1
            continue
        job = jobs.get(key)
        if job is None:
            job = jobs[key] = {
                "api": re.sub(r"\D", "", g(row, "APINumber")),
                "start_raw": g(row, "JobStartDate"),
                "operator": g(row, "OperatorName").strip(),
                "water": num(g(row, "TotalBaseWaterVolume")),
                "prop": 0.0, "prop_rows": 0, "prop_with_mass": 0,
            }
        elif num(g(row, "TotalBaseWaterVolume")) != job["water"]:
            stats["job_water_conflicts"] += 1   # same job, different job-level value; first kept
        if is_proppant(g(row, "Purpose")):
            job["prop_rows"] += 1
            mass = num(g(row, "MassIngredient"))
            if mass is not None and mass > 0:
                job["prop"] += mass
                job["prop_with_mass"] += 1
    return jobs, stats


def finalize(jobs, stats):
    out = []
    for job in jobs.values():
        start = parse_date(job["start_raw"])
        if not start:
            stats["rejected_bad_start_date"] += 1
            continue
        if len(job["api"]) < 8:
            stats["rejected_bad_api"] += 1
            continue
        water = job["water"] if job["water"] and job["water"] > 0 else None
        prop = job["prop"] if job["prop_with_mass"] else None   # blank, never 0, when unknown
        if water is None:
            stats["water_unknown"] += 1
        if prop is None:
            stats["proppant_unknown"] += 1
        out.append({"api": job["api"], "frac_start": start, "operator": job["operator"],
                    "water_gal": "" if water is None else round(water, 2),
                    "proppant_lb": "" if prop is None else round(prop, 2)})
    return out


def list_purposes(zf, member_limit=None):
    counts = defaultdict(int)
    for member in registry_members(zf)[:member_limit]:
        with open_text(zf, member) as handle:
            reader = csv.reader(handle)
            header = next(reader)
            i = header.index("Purpose")
            for row in reader:
                if i < len(row) and "ropp" in row[i].lower():
                    counts[row[i]] += 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def convert(zip_path, out_path, county=None, job_key=None):
    stats, jobs = defaultdict(int), {}
    with zipfile.ZipFile(zip_path) as zf:
        members = registry_members(zf)
        if not members:
            raise SystemExit("ERROR: no FracFocusRegistry_*.csv members in the zip.")
        used_key = None
        for member in members:
            with open_text(zf, member) as handle:
                reader = csv.reader(handle)
                header = next(reader)
                used_key = check_header(header, job_key)
                aggregate(reader, header, used_key, county=county.strip().upper() if county else None,
                          stats=stats, jobs=jobs)
            stats["members_read"] += 1
    rows = finalize(jobs, stats)
    with open(out_path, "w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return {"jobs_written": len(rows), "job_key_used": used_key, "county_filter": county,
            "stats": dict(stats)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--zip", required=True)
    ap.add_argument("--output")
    ap.add_argument("--county")
    ap.add_argument("--job-key")
    ap.add_argument("--list-purposes", action="store_true")
    args = ap.parse_args(argv)
    if args.list_purposes:
        with zipfile.ZipFile(args.zip) as zf:
            print(json.dumps(list_purposes(zf), indent=1))
        return 0
    if not args.output:
        ap.error("--output is required")
    print(json.dumps(convert(args.zip, args.output, args.county, args.job_key), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
