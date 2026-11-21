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
from datetime import date, datetime

csv.field_size_limit(10 * 1024 * 1024)

REQUIRED = ["APINumber", "OperatorName", "JobStartDate", "TotalBaseWaterVolume",
            "Purpose", "MassIngredient", "StateName"]
JOB_KEY_CANDIDATES = ["UploadKey", "DisclosureKey", "DisclosureId"]
DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"]
FIELDS = ["api", "frac_start", "operator", "water_gal", "proppant_lb", "proppant_basis"]
SAND_WORDS = ("silica", "quartz", "sand", "ceramic", "bauxite", "silicon dioxide", "sio2")
BASIS_BY_NAME = "RECORD: sand-named ingredient mass >= 100,000 lb (purpose not labeled proppant)"
MAX_DERIVED_LB_PER_GAL = 10.0   # derived proppant above this per gallon of base water is implausible
BIG_SAND_LB = 100_000           # a single sand-like ingredient this heavy is proppant-scale, not a trace
WATER_LB_PER_GAL = 8.34       # fresh water; produced/brine is denser, so derived figures run LOW
YEAR_MIN = 2000               # FracFocus began 2011; earlier job dates are entry errors
BASIS_RECORD = "RECORD: sum of MassIngredient (lb)"
BASIS_DERIVED = "DERIVED: base water mass x proppant %HF job / water %HF job"
WATER_NAMES = {"water", "fresh water", "produced water", "recycled water", "brackish water",
               "reuse water", "flowback water", "sea water", "salt water", "brine", "source water"}


def is_water_name(name):
    n = (name or "").strip().lower()
    return n in WATER_NAMES or n.startswith("water")


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


# Purpose strings are free text and sometimes a comma-separated list of everything a product
# does ("Additive, Breaker, ..., Proppant"). Judge each comma-separated token on its own:
# "propp" catches Proppant, Propping Agent (a common service-company wording) and typos like
# "Proppan"; the words below mark ADDITIVES ABOUT proppant (binders, carriers...), not proppant.
NOT_PROPPANT_WORDS = ("suspend", "transport", "carry", "carrier", "binder", "activator",
                      "shield", "polymer", "perforation", "friction", "fluid loss")


def is_proppant(purpose):
    for token in (purpose or "").lower().split(","):
        if ("propp" in token or "propant" in token) and not any(w in token for w in NOT_PROPPANT_WORDS):
            return True
    return False


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
    g = lambda row, name: row[idx[name]] if name in idx and idx[name] < len(row) else ""
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
                "prop_pct": 0.0, "water_pct": 0.0, "sand_name_total": 0.0,
            }
        elif num(g(row, "TotalBaseWaterVolume")) != job["water"]:
            stats["job_water_conflicts"] += 1   # same job, different job-level value; first kept
        pct = num(g(row, "PercentHFJob"))
        pct = pct if pct is not None and pct > 0 else 0.0
        ing = g(row, "IngredientName")
        if is_water_name(ing):
            job["water_pct"] += pct
        ing_mass = num(g(row, "MassIngredient"))
        if ing_mass is not None and ing_mass >= BIG_SAND_LB and any(w in ing.lower() for w in SAND_WORDS):
            job["sand_name_total"] += ing_mass   # proppant-scale sand identified by what it IS, not its label
        if is_proppant(g(row, "Purpose")):
            job["prop_pct"] += pct
            job["prop_rows"] += 1
            if "," in g(row, "Purpose"):
                stats["proppant_rows_from_multi_value_purpose"] += 1   # sand mixed with other additives
            mass = num(g(row, "MassIngredient"))
            if mass is not None and mass > 0:
                job["prop"] += mass
                job["prop_with_mass"] += 1
    return jobs, stats


def derive_proppant(water_gal, prop_pct, water_pct):
    """Estimate proppant pounds from percentages of the job's total fluid mass.
    Needs base water volume and both percentages; otherwise None. Runs low if the water is
    denser than fresh water, so it is only a fallback and always labeled DERIVED."""
    if water_gal and water_gal > 0 and prop_pct > 0 and water_pct > 0:
        return water_gal * WATER_LB_PER_GAL * prop_pct / water_pct
    return None


def finalize(jobs, stats, derive=False, max_year=None, max_lb_per_gal=MAX_DERIVED_LB_PER_GAL,
             sand_by_name=False):
    out = []
    for job in jobs.values():
        start = parse_date(job["start_raw"])
        if not start:
            stats["rejected_bad_start_date"] += 1
            continue
        if int(start[:4]) < YEAR_MIN or (max_year and int(start[:4]) > max_year):
            stats["rejected_implausible_start_date"] += 1   # e.g. 1955 or 3012: entry errors
            continue
        if len(job["api"]) < 8:
            stats["rejected_bad_api"] += 1
            continue
        water = job["water"] if job["water"] and job["water"] > 0 else None
        prop = job["prop"] if job["prop_with_mass"] else None   # blank, never 0, when unknown
        basis = BASIS_RECORD if prop is not None else ""
        if prop is None and sand_by_name and job["sand_name_total"] > 0:
            named = job["sand_name_total"]
            if water and named / water > max_lb_per_gal:
                stats["rejected_implausible_sand_named_proppant"] += 1
            else:
                prop, basis = named, BASIS_BY_NAME
                stats["proppant_from_sand_named_ingredient"] += 1
        if prop is None and derive:
            prop = derive_proppant(water, job["prop_pct"], job["water_pct"])
            if prop is not None and prop / water > max_lb_per_gal:
                prop = None                                  # physically implausible: leave unknown
                stats["rejected_implausible_derived_proppant"] += 1
            if prop is not None:
                basis = BASIS_DERIVED
                stats["proppant_derived_from_percent"] += 1
        if water is None:
            stats["water_unknown"] += 1
        if prop is None:
            stats["proppant_unknown"] += 1
        out.append({"api": job["api"], "frac_start": start, "operator": job["operator"],
                    "water_gal": "" if water is None else round(water, 2),
                    "proppant_lb": "" if prop is None else round(prop, 2),
                    "proppant_basis": basis})
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



def _pcts(values):
    values = sorted(values)
    if not values:
        return {"n": 0}
    q = lambda p: round(values[min(len(values) - 1, int(p * len(values)))], 3)
    return {"n": len(values), "p05": q(0.05), "p25": q(0.25), "median": q(0.5), "p75": q(0.75), "p95": q(0.95)}


def diagnose(zip_path, county=None, job_key=None, top=25, brief=False):
    """Why are proppant figures unknown? Per-year counts for Texas jobs, read-only."""
    jobs = {}
    purposes = defaultdict(int)
    unmatched_big_sand = defaultdict(int)
    want = county.strip().upper() if county else None
    with zipfile.ZipFile(zip_path) as zf:
        for member in registry_members(zf):
            with open_text(zf, member) as handle:
                reader = csv.reader(handle)
                header = next(reader)
                key_name = check_header(header, job_key)
                idx = {n: i for i, n in enumerate(header)}
                g = lambda row, n: row[idx[n]] if n in idx and idx[n] < len(row) else ""
                for row in reader:
                    if g(row, "StateName").strip().upper() != "TEXAS":
                        continue
                    if want and g(row, "CountyName").strip().upper() != want:
                        continue
                    key = g(row, key_name)
                    if not key:
                        continue
                    job = jobs.get(key)
                    if job is None:
                        start = parse_date(g(row, "JobStartDate"))
                        job = jobs[key] = {"year": start[:4] if start else "unknown",
                                           "rows": 0, "matched": False, "matched_mass": False,
                                           "any_mass": False, "sand_name": False, "sand_mass": False,
                                           "water": (num(g(row, "TotalBaseWaterVolume")) or 0) > 0,
                                           "water_gal": num(g(row, "TotalBaseWaterVolume")) or 0.0,
                                           "prop_pct": 0.0, "water_pct": 0.0, "prop_mass": 0.0,
                                           "big_unmatched": False, "sand_name_total": 0.0}
                    job["rows"] += 1
                    pct = num(g(row, "PercentHFJob"))
                    pct = pct if pct is not None and pct > 0 else 0.0
                    if is_water_name(g(row, "IngredientName")):
                        job["water_pct"] += pct
                    mass = num(g(row, "MassIngredient"))
                    has_mass = mass is not None and mass > 0
                    job["any_mass"] |= has_mass
                    purpose = g(row, "Purpose")
                    if "," not in purpose and "propp" in purpose.lower():
                        purposes[purpose.strip()] += 1
                    if is_proppant(purpose):
                        job["matched"] = True
                        job["matched_mass"] |= has_mass
                        job["prop_pct"] += pct
                        if has_mass:
                            job["prop_mass"] += mass
                    name = g(row, "IngredientName").lower()
                    if any(w in name for w in SAND_WORDS):
                        job["sand_name"] = True
                        job["sand_mass"] |= has_mass
                        if has_mass and mass >= BIG_SAND_LB:
                            job["sand_name_total"] += mass
                        if has_mass and mass >= BIG_SAND_LB and not is_proppant(purpose):
                            job["big_unmatched"] = True            # proppant-scale sand under another label
                            unmatched_big_sand[purpose.strip()[:70] or "(blank)"] += 1
    by_year = defaultdict(lambda: defaultdict(int))
    ratios, lbgal_recorded, lbgal_derived, ratios_name = [], [], [], []
    for job in jobs.values():
        est = derive_proppant(job["water_gal"], job["prop_pct"], job["water_pct"])
        if est is not None and job["prop_mass"] > 0:
            ratios.append(est / job["prop_mass"])          # estimate / recorded, jobs having both
        if job["sand_name_total"] > 0 and job["prop_mass"] > 0:
            ratios_name.append(job["sand_name_total"] / job["prop_mass"])   # name rule / purpose-labeled mass
        if job["water_gal"] > 0 and job["prop_mass"] > 0:
            lbgal_recorded.append(job["prop_mass"] / job["water_gal"])
        elif est is not None and job["water_gal"] > 0:
            lbgal_derived.append(est / job["water_gal"])
        y = by_year[job["year"]]
        y["matched_no_mass_but_derivable_from_percent"] += int(job["matched"] and not job["matched_mass"] and est is not None)
        has_rec = job["matched_mass"]
        by_name = (not has_rec) and job["sand_name_total"] > 0
        by_pct = (not has_rec) and (not by_name) and est is not None
        y["cover_recorded_by_purpose"] += int(has_rec)
        y["cover_added_by_sand_name_rule"] += int(by_name)
        y["cover_added_by_percent_only"] += int(by_pct)
        y["jobs"] += 1
        y["have_water"] += job["water"]
        y["any_ingredient_mass"] += job["any_mass"]
        y["proppant_purpose_match"] += job["matched"]
        y["proppant_purpose_match_with_mass"] += job["matched_mass"]
        y["sand_like_ingredient_name"] += job["sand_name"]
        y["sand_like_ingredient_with_mass"] += job["sand_mass"]
        y["rows_per_job_zero_or_one"] += job["rows"] <= 1
        y["large_sand_under_other_label_and_no_proppant_mass"] += int(job["big_unmatched"] and not job["matched_mass"])
    ratios.sort()
    q = lambda p: round(ratios[min(len(ratios) - 1, int(p * len(ratios)))], 3) if ratios else None
    this_year = date.today().year

    def window(lo):
        agg = defaultdict(int)
        for yr, vals in by_year.items():
            if yr.isdigit() and lo <= int(yr) <= this_year:
                for k, v in vals.items():
                    agg[k] += v
        total = agg["jobs"] or 1
        out = dict(agg)
        out["share_with_water"] = round(agg["have_water"] / total, 3)
        out["share_proppant_pounds_recorded"] = round(agg["proppant_purpose_match_with_mass"] / total, 3)
        out["share_all_three_methods"] = round(
            (agg["cover_recorded_by_purpose"] + agg["cover_added_by_sand_name_rule"]
             + agg["cover_added_by_percent_only"]) / total, 3)
        out["share_recorded_plus_percent_derivable"] = round(
            (agg["proppant_purpose_match_with_mass"] + agg["matched_no_mass_but_derivable_from_percent"]) / total, 3)
        return out

    if brief:
        return {"jobs_total": len(jobs),
                "percent_method_calibration": {"n": len(ratios), "estimate_over_recorded": {
                    "p10": q(0.1), "median": q(0.5), "p90": q(0.9)}},
                "jobs_2013_to_now": window(2013),
                "jobs_last_3_years": window(this_year - 2),
                "lb_proppant_per_gal_base_water": {"recorded_jobs": _pcts(lbgal_recorded),
                                                   "derived_only_jobs": _pcts(lbgal_derived)},
                "sand_name_rule_over_purpose_labeled_mass": _pcts(ratios_name),
                "where_large_sand_hides_when_not_labeled_proppant": dict(
                    sorted(unmatched_big_sand.items(), key=lambda kv: -kv[1])[:15]),
                "how_to_read": "If derived_only_jobs looks like recorded_jobs, the percent method is behaving. "
                               "Large sand under another label means the matcher should learn that label."}
    return {"jobs_total": len(jobs),
            "lb_proppant_per_gal_base_water": {"recorded_jobs": _pcts(lbgal_recorded),
                                               "derived_only_jobs": _pcts(lbgal_derived)},
            "sand_name_rule_over_purpose_labeled_mass": _pcts(ratios_name),
            "where_large_sand_hides_when_not_labeled_proppant": dict(
                sorted(unmatched_big_sand.items(), key=lambda kv: -kv[1])[:15]),
            "percent_method_calibration": {
                "jobs_with_both_recorded_mass_and_percent_estimate": len(ratios),
                "estimate_divided_by_recorded_mass": {"p10": q(0.10), "p25": q(0.25), "median": q(0.5),
                                                      "p75": q(0.75), "p90": q(0.90)},
                "read_as": "median near 1.0 and a tight p25-p75 means the percent method is trustworthy; "
                           "below 1 means it runs low (denser water or missing water row)."},
            "by_year": {k: dict(v) for k, v in sorted(by_year.items())},
            "top_single_value_purposes_containing_propp": dict(sorted(purposes.items(), key=lambda kv: -kv[1])[:top]),
            "how_to_read": "proppant_purpose_match_with_mass is what the adapter will use. If "
                           "sand_like_ingredient_with_mass is much larger, proppant is recorded "
                           "under purposes the matcher does not recognize. If any_ingredient_mass "
                           "is low, the source has no ingredient masses for that job."}


def convert(zip_path, out_path, county=None, job_key=None, derive=False,
            max_lb_per_gal=MAX_DERIVED_LB_PER_GAL, sand_by_name=False):
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
                if derive and not {"PercentHFJob", "IngredientName"} <= set(header):
                    raise SystemExit("ERROR: --derive-proppant-from-percent needs the PercentHFJob and "
                                     f"IngredientName columns; header found: {header}")
                aggregate(reader, header, used_key, county=county.strip().upper() if county else None,
                          stats=stats, jobs=jobs)
            stats["members_read"] += 1
    rows = finalize(jobs, stats, derive=derive, max_year=date.today().year + 1,
                    max_lb_per_gal=max_lb_per_gal, sand_by_name=sand_by_name)
    with open(out_path, "w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return {"jobs_written": len(rows), "job_key_used": used_key, "county_filter": county,
            "derive_proppant_from_percent": derive, "sand_by_name": sand_by_name,
            "stats": dict(stats)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--zip", required=True)
    ap.add_argument("--output")
    ap.add_argument("--county")
    ap.add_argument("--job-key")
    ap.add_argument("--list-purposes", action="store_true")
    ap.add_argument("--derive-proppant-from-percent", action="store_true",
                    help="where MassIngredient is blank, ESTIMATE proppant from percent of job fluid "
                         "(labeled DERIVED in proppant_basis). Check --diagnose calibration first.")
    ap.add_argument("--sand-by-name", action="store_true",
                    help="where no proppant-labeled mass exists, use sand-like ingredients (silica, quartz, "
                         "ceramic...) of >= 100,000 lb regardless of label (basis column says so). "
                         "Check the --diagnose --brief ratio first.")
    ap.add_argument("--max-derived-lb-per-gal", type=float, default=MAX_DERIVED_LB_PER_GAL,
                    help="discard DERIVED proppant above this many lb per gallon of base water (default 10)")
    ap.add_argument("--brief", action="store_true", help="with --diagnose: print only the key findings")
    ap.add_argument("--diagnose", action="store_true",
                    help="per-year breakdown of why proppant figures are unknown (read-only)")
    args = ap.parse_args(argv)
    if args.diagnose:
        print(json.dumps(diagnose(args.zip, args.county, args.job_key, brief=args.brief), indent=1))
        return 0
    if args.list_purposes:
        with zipfile.ZipFile(args.zip) as zf:
            print(json.dumps(list_purposes(zf), indent=1))
        return 0
    if not args.output:
        ap.error("--output is required")
    print(json.dumps(convert(args.zip, args.output, args.county, args.job_key,
                             args.derive_proppant_from_percent, args.max_derived_lb_per_gal,
                             args.sand_by_name), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
