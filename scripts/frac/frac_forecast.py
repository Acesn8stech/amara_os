#!/usr/bin/env python3
"""Frac demand forecast core (Phase 0). Python 3.10+, standard library only.

Predicts WHEN and HOW MUCH water and proppant frac crews will need, from drilling
permits that are already issued. Every output row is basis=ESTIMATE; unknown values
stay UNKNOWN/None. See docs/FRAC-DEMAND-PHASE0-SPEC.md.

Inputs are NORMALIZED CSVs (adapters for RRC permit files and FracFocus downloads
produce these; they are not part of this core):

  permits.csv : api, permit_date (YYYY-MM-DD), operator, county, lateral_ft
  frac.csv    : api, frac_start (YYYY-MM-DD), operator, water_gal, proppant_lb
                (blank water/proppant = unknown)

Commands:
  backtest --permits P --frac F [--county NAME] [--horizon-weeks 13]
  forecast --permits P --frac F --as-of YYYY-MM-DD [--county NAME]
"""
import argparse
import bisect
import csv
import json
import re
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta

MIN_GROUP_N = 5          # smallest sample used for an operator/county estimate
MATURE_DAYS = 540        # a permit older than this with no frac counts as "not completed"
MAX_PERMIT_AGE = 540     # permits older than this are not forecast
MAX_BACKTEST_WINDOWS = 400   # refuse absurd ranges (e.g. one bad date in the year 3012)
UNKNOWN = "UNKNOWN"

# Model options. V1 is the original design; the others are tested one at a time with `backtest --ablate`.
#   intensity_stat: "median" | "trimmed_mean"   (a TOTAL is a sum of means, so a median runs low on skewed volumes)
#   intensity_recent_days: None | N             (well sizes drift; only use fracs from the last N days)
#   timing: "iqr" | "hazard"                    (hazard uses the full remaining-delay distribution)
V1 = {"intensity_stat": "median", "intensity_recent_days": None, "timing": "iqr"}
TRIM = 0.05


def opts_with(**kw):
    d = dict(V1)
    d.update(kw)
    return d


def trimmed_mean(values, trim=TRIM):
    v = sorted(values)
    k = int(len(v) * trim)
    v = v[k:len(v) - k] if len(v) - 2 * k > 0 else v
    return sum(v) / len(v)


def _stat(values, opts):
    return statistics.median(values) if opts["intensity_stat"] == "median" else trimmed_mean(values)


# --------------------------------------------------------------------------- parsing
def api10(value):
    """Normalize a Texas API number to its 10-digit well key (42 + county + well).

    RRC permit files commonly carry 8 digits (county+well, no state code); FracFocus
    carries 14 (10 + sidetrack + completion). Anything else is not joinable.
    """
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 8:
        digits = "42" + digits
    if len(digits) >= 10 and digits.startswith("42"):
        return digits[:10]
    return None


def parse_date(value):
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except (ValueError, TypeError):
        return None


def num(value):
    try:
        x = float(str(value).replace(",", "").strip())
        return x if x == x and x not in (float("inf"), float("-inf")) else None
    except (ValueError, TypeError):
        return None


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def clean_permits(rows):
    out, rejected = {}, defaultdict(int)
    for r in rows:
        key, d = api10(r.get("api")), parse_date(r.get("permit_date"))
        if not key:
            rejected["bad_api"] += 1
            continue
        if not d:
            rejected["bad_permit_date"] += 1
            continue
        rec = {
            "api": key, "permit_date": d,
            "operator": (r.get("operator") or UNKNOWN).strip().upper() or UNKNOWN,
            "county": (r.get("county") or UNKNOWN).strip().upper() or UNKNOWN,
            "lateral_ft": num(r.get("lateral_ft")),
        }
        # Amended permits repeat an API: keep the earliest, it starts the clock.
        if key not in out or d < out[key]["permit_date"]:
            out[key] = rec
    return out, dict(rejected)


def clean_fracs(rows):
    by_api, rejected = defaultdict(list), defaultdict(int)
    for r in rows:
        key, d = api10(r.get("api")), parse_date(r.get("frac_start"))
        if not key:
            rejected["bad_api"] += 1
            continue
        if not d:
            rejected["bad_frac_date"] += 1
            continue
        by_api[key].append({"frac_start": d, "water_gal": num(r.get("water_gal")),
                            "proppant_lb": num(r.get("proppant_lb"))})
    for jobs in by_api.values():
        jobs.sort(key=lambda j: j["frac_start"])
    return dict(by_api), dict(rejected)


# --------------------------------------------------------------------------- join
def join(permits, fracs):
    """Permit -> first frac job on or after the permit date. Returns (rows, report)."""
    rows, report = [], defaultdict(int)
    report["permits"] = len(permits)
    for key, p in permits.items():
        jobs = fracs.get(key)
        if not jobs:
            report["no_frac_found"] += 1
            continue
        valid = [j for j in jobs if j["frac_start"] >= p["permit_date"]]
        if not valid:
            report["frac_before_permit"] += 1   # data error; excluded, not "fixed"
            continue
        if len(valid) > 1:
            report["multiple_jobs_first_used"] += 1
        j = valid[0]
        rows.append({**p, **j, "lag_days": (j["frac_start"] - p["permit_date"]).days})
    report["joined"] = len(rows)
    report["match_rate"] = round(len(rows) / len(permits), 4) if permits else None
    return rows, dict(report)


# --------------------------------------------------------------------------- stats
def percentile(sorted_values, q):
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _levels(op, county):
    return [("operator+county", (op, county)), ("operator", (op, None)),
            ("county", (None, county)), ("all", (None, None))]


def _group(rows, value_fn):
    groups = defaultdict(list)
    for r in rows:
        v = value_fn(r)
        if v is None:
            continue
        for _, key in _levels(r["operator"], r["county"]):
            groups[key].append(v)
    return groups


def pick(groups, op, county, min_n=MIN_GROUP_N):
    """Most specific group with enough samples. Returns (values, level) or (None, UNKNOWN)."""
    for level, key in _levels(op, county):
        values = groups.get(key)
        if values and len(values) >= min_n:
            return values, level
    return None, UNKNOWN


def fit(joined, permits, as_of, opts=None):
    """Fit lag, intensity and completion models using ONLY data known at as_of."""
    opts = opts or V1
    seen = [r for r in joined if r["frac_start"] <= as_of]
    lags = {k: sorted(v) for k, v in _group(seen, lambda r: r["lag_days"]).items()}
    # Well sizes drift over time, so intensities may use only recent fracs. Lags may NOT: restricting
    # to recent completions would drop the long waits of recent permits (a censoring bias).
    days = opts["intensity_recent_days"]
    sized = seen if days is None else [r for r in seen if r["frac_start"] > as_of - timedelta(days=days)]
    water = _group(sized, lambda r: r["water_gal"] / r["lateral_ft"]
                   if r["water_gal"] and r["lateral_ft"] else None)
    prop = _group(sized, lambda r: r["proppant_lb"] / r["lateral_ft"]
                  if r["proppant_lb"] and r["lateral_ft"] else None)

    # Completion rate from a MATURED cohort, so recent permits that simply haven't
    # been fracked yet are not counted as "never completed".
    done_by_api = {r["api"]: r for r in seen}
    cohort = [p for p in permits.values()
              if p["permit_date"] <= as_of - timedelta(days=MATURE_DAYS)]
    comp = defaultdict(lambda: [0, 0])
    for p in cohort:
        r = done_by_api.get(p["api"])
        hit = 1 if r and r["lag_days"] <= MATURE_DAYS else 0
        for _, key in _levels(p["operator"], p["county"]):
            comp[key][0] += hit
            comp[key][1] += 1
    # Per-WELL volumes: used when a permit has no lateral length (RRC publishes none).
    water_well = _group(sized, lambda r: r["water_gal"] if r["water_gal"] else None)
    prop_well = _group(sized, lambda r: r["proppant_lb"] if r["proppant_lb"] else None)
    return {"lags": lags, "water": water, "prop": prop,
            "water_well": water_well, "prop_well": prop_well, "completion": dict(comp),
            "seen_fracs": {r["api"] for r in seen}}


def completion_rate(model, op, county):
    for level, key in _levels(op, county):
        hit, total = model["completion"].get(key, (0, 0))
        if total >= 10:
            return hit / total, level
    return None, UNKNOWN


# --------------------------------------------------------------------------- forecast
def hazard_weeks(sorted_lags, age, weeks):
    """Share of the REMAINING-delay distribution falling in each week after as_of, or None if too thin."""
    n_rem = len(sorted_lags) - bisect.bisect_right(sorted_lags, age)
    if n_rem < 3:
        return None
    out = []
    for k in range(weeks):
        lo, hi = max(age + 1, age + 7 * k), age + 7 * (k + 1)       # remaining delay in [7k, 7k+7)
        out.append((bisect.bisect_left(sorted_lags, hi) - bisect.bisect_left(sorted_lags, lo)) / n_rem if hi > lo else 0.0)
    return out


def forecast(permits, model, as_of, weeks=13, opts=None):
    """Weekly water/proppant demand from permits with no frac yet as of `as_of`."""
    opts = opts or V1
    start = as_of
    buckets = [{"week_start": (start + timedelta(weeks=i)).isoformat(),
                "water_gal": 0.0, "proppant_lb": 0.0, "permits": 0.0,
                "water_unknown_permits": 0.0, "proppant_unknown_permits": 0.0,
                "per_well_fallback_permits": 0.0}
               for i in range(weeks)]
    notes = defaultdict(int)
    for p in permits.values():
        age = (as_of - p["permit_date"]).days
        if p["permit_date"] > as_of or age > MAX_PERMIT_AGE or p["api"] in model["seen_fracs"]:
            continue
        lags, lag_level = pick(model["lags"], p["operator"], p["county"])
        if not lags:
            notes["no_lag_model"] += 1
            continue
        # Condition on survival: this permit has already waited `age` days.
        if opts["timing"] == "hazard":
            shares = hazard_weeks(lags, age, weeks)
            if shares is None:
                notes["overdue_no_comparable_history"] += 1
                continue
        else:
            remaining = sorted(l - age for l in lags if l > age)
            if len(remaining) < 3:
                notes["overdue_no_comparable_history"] += 1
                continue
            d0, d1 = percentile(remaining, 0.25), percentile(remaining, 0.75)
            d0, d1 = max(0, int(d0)), max(int(d1), int(d0) + 6)
        comp, _ = completion_rate(model, p["operator"], p["county"])
        if comp is None:
            comp = 1.0
            notes["completion_rate_unknown_assumed_1"] += 1
        w_int, _ = pick(model["water"], p["operator"], p["county"])
        p_int, _ = pick(model["prop"], p["operator"], p["county"])
        w_well, _ = pick(model["water_well"], p["operator"], p["county"])
        p_well, _ = pick(model["prop_well"], p["operator"], p["county"])
        lateral = p["lateral_ft"]
        if not lateral:
            notes["no_lateral_length_used_per_well_volume"] += 1
        if opts["timing"] == "hazard":
            placements = [(wk, comp * s) for wk, s in enumerate(shares) if s > 0]
        else:
            days = d1 - d0 + 1
            by_week = defaultdict(float)
            for day in range(d0, d1 + 1):
                if day // 7 < weeks:
                    by_week[day // 7] += comp / days
            placements = list(by_week.items())
        for wk, share in placements:
            buckets[wk]["permits"] += share
            # Unknown is not zero: permits we cannot price are counted, not hidden.
            if lateral and w_int:
                buckets[wk]["water_gal"] += share * lateral * _stat(w_int, opts)
            elif w_well:
                buckets[wk]["water_gal"] += share * _stat(w_well, opts)
                buckets[wk]["per_well_fallback_permits"] += share
            else:
                buckets[wk]["water_unknown_permits"] += share
            if lateral and p_int:
                buckets[wk]["proppant_lb"] += share * lateral * _stat(p_int, opts)
            elif p_well:
                buckets[wk]["proppant_lb"] += share * _stat(p_well, opts)
            else:
                buckets[wk]["proppant_unknown_permits"] += share
    for b in buckets:
        b["basis"] = "ESTIMATE"
        for k in ("water_gal", "proppant_lb", "permits",
                  "water_unknown_permits", "proppant_unknown_permits",
                  "per_well_fallback_permits"):
            b[k] = round(b[k], 2)
    return buckets, dict(notes)


# --------------------------------------------------------------------------- backtest
def actual_by_week(joined, as_of, weeks):
    water, prop = [0.0] * weeks, [0.0] * weeks
    for r in joined:
        wk = (r["frac_start"] - as_of).days // 7
        if 0 <= wk < weeks and r["frac_start"] > as_of:
            water[wk] += r["water_gal"] or 0.0
            prop[wk] += r["proppant_lb"] or 0.0
    return water, prop


def wape(pred, act):
    total = sum(act)
    return None if total <= 0 else sum(abs(p - a) for p, a in zip(pred, act)) / total


def quick_summary(results):
    sc = [r for r in results if r["model_wape"] is not None and r["baseline_wape"] is not None]
    if not sc:
        return {"windows": 0}
    errs = [(r["pred_total"] - r["actual_total"]) / r["actual_total"] for r in sc if r["actual_total"] > 0]
    wins = sum(1 for r in sc if r["model_wape"] < r["baseline_wape"])
    return {"windows": len(sc),
            "median_model_wape": round(statistics.median(r["model_wape"] for r in sc), 3),
            "median_baseline_wape": round(statistics.median(r["baseline_wape"] for r in sc), 3),
            "model_beat_baseline": f"{wins} of {len(sc)}",
            "median_13wk_total_bias": round(statistics.median(errs), 3) if errs else None,
            "median_abs_13wk_total_error": round(statistics.median(abs(e) for e in errs), 3) if errs else None}


ABLATION_VARIANTS = [
    ("v1_original (median, iqr timing)", opts_with()),
    ("A: trimmed mean per well", opts_with(intensity_stat="trimmed_mean")),
    ("B: A + well sizes from last 24 months", opts_with(intensity_stat="trimmed_mean", intensity_recent_days=730)),
    ("C: B + full-distribution (hazard) timing", opts_with(intensity_stat="trimmed_mean", intensity_recent_days=730, timing="hazard")),
]


def ablate(permits, joined, first, last, weeks=13, split_year=2022):
    out = {}
    for name, opts in ABLATION_VARIANTS:
        results, _ = backtest(permits, joined, first, last, weeks, opts=opts)
        out[name] = {"all_windows": quick_summary(results),
                     f"through_{split_year}": quick_summary([r for r in results if int(r["as_of"][:4]) <= split_year]),
                     f"{split_year + 1}_onward": quick_summary([r for r in results if int(r["as_of"][:4]) > split_year])}
    return out


def classify_actuals(permits, joined, model, as_of, weeks, opts=None):
    """Split the ACTUAL water used in the horizon by why the model could or could not predict it.

    Classes: permit_issued_after_forecast_date (short lag, unknowable then), permit_older_than_cutoff
    (dropped by MAX_PERMIT_AGE), no_lag_model_for_operator, overdue_no_comparable_history, modelled
    (the model had a chance). Also returns actual vs predicted per-well volume for the modelled wells.
    """
    end = as_of + timedelta(weeks=weeks)
    water, counts = defaultdict(float), defaultdict(int)
    num = den = 0.0
    old_key = f"permit_older_than_{MAX_PERMIT_AGE}_day_cutoff"
    for r in joined:
        if not (as_of < r["frac_start"] <= end):
            continue
        w = r["water_gal"] or 0.0
        age = (as_of - r["permit_date"]).days
        if r["permit_date"] > as_of:
            cls = "permit_issued_after_forecast_date"
        elif age > MAX_PERMIT_AGE:
            cls = old_key
        else:
            lags, _ = pick(model["lags"], r["operator"], r["county"])
            if not lags:
                cls = "no_lag_model_for_operator"
            elif len([x for x in lags if x > age]) < 3:
                cls = "overdue_no_comparable_history"
            else:
                cls = "modelled"
        water[cls] += w
        counts[cls] += 1
        if cls == "modelled" and w:
            per_well, _ = pick(model["water_well"], r["operator"], r["county"])
            if per_well:
                num += w
                den += _stat(per_well, opts or V1)
    return dict(water), dict(counts), num, den


def backtest(permits, joined, first_as_of, last_as_of, weeks=13, step_days=30, explain=False, opts=None):
    opts = opts or V1
    results, d = [], first_as_of
    cause_water, cause_counts = defaultdict(float), defaultdict(int)
    ratio_num = ratio_den = 0.0
    while d <= last_as_of:
        model = fit(joined, permits, d, opts)
        buckets, notes = forecast(permits, model, d, weeks, opts)
        act_w, act_p = actual_by_week(joined, d, weeks)
        pred_w = [b["water_gal"] for b in buckets]
        # Naive baseline: the last 28 days' actual daily average, repeated every week.
        recent = sum(r["water_gal"] or 0.0 for r in joined
                     if d - timedelta(days=28) < r["frac_start"] <= d)
        base_w = [recent / 4.0] * weeks
        if explain:
            cw, cc, n_, d_ = classify_actuals(permits, joined, model, d, weeks, opts)
            for k, v in cw.items():
                cause_water[k] += v
            for k, v in cc.items():
                cause_counts[k] += v
            ratio_num += n_
            ratio_den += d_
        results.append({"as_of": d.isoformat(),
                        "model_wape": wape(pred_w, act_w), "baseline_wape": wape(base_w, act_w),
                        "pred_total": sum(pred_w), "actual_total": sum(act_w),
                        "actual_proppant_total": sum(act_p), "notes": notes})
        d += timedelta(days=step_days)
    scored = [r for r in results if r["model_wape"] is not None and r["baseline_wape"] is not None]
    summary = {"windows": len(results), "scored_windows": len(scored)}
    if scored:
        summary["median_model_wape"] = round(statistics.median(r["model_wape"] for r in scored), 4)
        summary["median_baseline_wape"] = round(statistics.median(r["baseline_wape"] for r in scored), 4)
        summary["gate_G3_beats_baseline"] = summary["median_model_wape"] < summary["median_baseline_wape"]
        bias = [(r["pred_total"] - r["actual_total"]) / r["actual_total"]
                for r in scored if r["actual_total"] > 0]
        summary["median_volume_bias"] = round(statistics.median(bias), 4) if bias else None
    else:
        summary["gate_G3_beats_baseline"] = UNKNOWN
        summary["reason"] = "no window had both a forecast and actual volume"
    if scored:
        wins = sum(1 for r in scored if r["model_wape"] < r["baseline_wape"])
        summary["windows_where_model_beat_baseline"] = f"{wins} of {len(scored)}"
        errs = [(r["pred_total"] - r["actual_total"]) / r["actual_total"] for r in scored if r["actual_total"] > 0]
        summary["horizon_total_error_p25_median_p75"] = [round(percentile(sorted(errs), q), 3) for q in (0.25, 0.5, 0.75)]
    if explain:
        total = sum(cause_water.values()) or 1.0
        summary["actual_water_by_cause_share"] = {k: round(v / total, 3) for k, v in
                                                  sorted(cause_water.items(), key=lambda kv: -kv[1])}
        summary["actual_wells_by_cause"] = dict(cause_counts)
        summary["per_well_volume_actual_over_predicted_for_modelled_wells"] = (
            round(ratio_num / ratio_den, 3) if ratio_den else None)
    return results, summary


# --------------------------------------------------------------------------- CLI
def _load(args):
    permits, prej = clean_permits(read_csv(args.permits))
    fracs, frej = clean_fracs(read_csv(args.frac))
    if args.county:
        want = args.county.strip().upper()
        permits = {k: v for k, v in permits.items() if v["county"] == want}
    joined, report = join(permits, fracs)
    report["rejected_permit_rows"], report["rejected_frac_rows"] = prej, frej
    return permits, joined, report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("backtest", "forecast"):
        s = sub.add_parser(name)
        s.add_argument("--permits", required=True)
        s.add_argument("--frac", required=True)
        s.add_argument("--county")
        s.add_argument("--weeks", type=int, default=13)
    sub.choices["forecast"].add_argument("--as-of", required=True)
    sub.choices["backtest"].add_argument("--from-date", help="first as-of date (YYYY-MM-DD); default: 1 year after the first frac")
    sub.choices["backtest"].add_argument("--ablate", action="store_true",
                                         help="compare the original model with fixes added one at a time")
    sub.choices["backtest"].add_argument("--model", choices=["v1", "v2"], default="v1",
                                         help="v2 = trimmed mean + 24-month well sizes + hazard timing")
    sub.choices["backtest"].add_argument("--explain", action="store_true",
                                         help="split actual volume by why the model could/could not predict it")
    sub.choices["backtest"].add_argument("--summary-only", action="store_true",
                                         help="print only the summary and join report (no per-window list)")
    sub.choices["backtest"].add_argument("--to-date", help="last as-of date (YYYY-MM-DD); default: last frac minus the horizon")
    args = ap.parse_args(argv)

    permits, joined, report = _load(args)
    print("JOIN REPORT (gate G1 needs a hand re-check of 10 rows):", json.dumps(report, indent=1), file=sys.stderr)
    if not joined:
        print(json.dumps({"status": "NO_JOINED_ROWS", "join": report}))
        return 2

    if args.cmd == "forecast":
        as_of = parse_date(args.as_of)
        if not as_of:
            sys.exit("--as-of must be YYYY-MM-DD")
        buckets, notes = forecast(permits, fit(joined, permits, as_of), as_of, args.weeks)
        print(json.dumps({"basis": "ESTIMATE", "as_of": as_of.isoformat(), "weeks": buckets,
                          "notes": notes, "join": report}, indent=1))
        return 0

    first = min(r["frac_start"] for r in joined) + timedelta(days=365)
    last = max(r["frac_start"] for r in joined) - timedelta(weeks=args.weeks)
    for flag, value in (("--from-date", args.from_date), ("--to-date", args.to_date)):
        if value and not parse_date(value):
            sys.exit(f"{flag} must be YYYY-MM-DD")
    if args.from_date:
        first = max(first, parse_date(args.from_date))
    if args.to_date:
        last = min(last, parse_date(args.to_date))
    if first <= last and (last - first).days / 30 > MAX_BACKTEST_WINDOWS:
        print(json.dumps({"status": "RANGE_TOO_LARGE", "first": first.isoformat(), "last": last.isoformat(),
                          "hint": "check the data for impossible dates, or pass --from-date/--to-date"}))
        return 2
    if first > last:
        print(json.dumps({"status": "NOT_ENOUGH_HISTORY",
                          "need": "at least ~1 year of fracs plus the forecast horizon"}))
        return 2
    if args.ablate:
        print(json.dumps({"ablation": ablate(permits, joined, first, last, args.weeks), "join": report}, indent=1))
        return 0
    chosen = ABLATION_VARIANTS[3][1] if args.model == "v2" else V1
    results, summary = backtest(permits, joined, first, last, args.weeks, explain=args.explain, opts=chosen)
    payload = {"summary": summary, "join": report}
    if not args.summary_only:
        payload["windows"] = results
    print(json.dumps(payload, indent=1))
    return 0 if summary.get("gate_G3_beats_baseline") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
