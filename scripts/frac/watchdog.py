#!/usr/bin/env python3
"""Watchdog for the frac-demand lane. Deterministic: reads the output of run_suite.py and decides
OK / WARN / ALERT. It NEVER changes models, data or code; it only writes status.json and ALERT.md so a
person (or a reviewing Claude) can act.

  python3 scripts/frac/watchdog.py --results ~/frac-data/work/results --out ~/frac-data/work/watch \\
      --inputs ~/frac-data/work/permits.csv ~/frac-data/work/frac_v2.csv
  python3 scripts/frac/watchdog.py --results-ref origin/claude/frac-results --out ./watch   (read published results)

Exit code: 0 OK, 1 WARN, 2 ALERT. Kill switches: FD_KILL_SWITCH or FD_KILL_WATCH set to 1 -> exit 0 untouched.
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("run_suite", HERE / "run_suite.py")
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)

REQUIRED_FILES = ["SUMMARY.md", "water_no_delay.json", "water_with_delay.json", "sand_no_delay.json", "sand_with_delay.json"]
MIN_SHARE_BETTER = 0.6        # v2 must beat the trailing average in at least this share of counties that ran
REGRESS_REL, REGRESS_ABS = 0.25, 0.05   # a county's 13-week error got worse by 25% AND by 0.05 absolute
REGRESS_ALERT_COUNTIES = 3
BIAS_WARN = 0.20              # |median 13-week bias| above this in over half the counties
STALE_DAYS = 45
LOW_MATCH = 0.70
RUNS = {"water_no_delay": "water", "water_with_delay": "water with filing delay",
        "sand_no_delay": "sand", "sand_with_delay": "sand with filing delay"}


def load_run(folder):
    folder = Path(folder)
    missing = [f for f in REQUIRED_FILES if not (folder / f).is_file()]
    if missing:
        return {"missing": missing}
    data = {name: json.loads((folder / f"{name}.json").read_text()) for name in RUNS}
    return {"data": data, "summary": (folder / "SUMMARY.md").read_text()}


def latest_two(parent):
    """The newest two timestamped run folders under `parent` (publish layout), or parent itself if it is a run."""
    parent = Path(parent)
    if (parent / "SUMMARY.md").is_file():
        return parent, None
    subs = sorted(p for p in parent.iterdir() if p.is_dir() and (p / "SUMMARY.md").is_file())
    if not subs:
        return None, None
    return subs[-1], (subs[-2] if len(subs) > 1 else None)


def extract_from_git(ref, repo_root):
    """Copy the two newest results/<stamp> folders from a git ref into a temp dir (no checkout, no raw data)."""
    def git(*a):
        p = subprocess.run(["git", *a], cwd=repo_root, capture_output=True, text=True)
        if p.returncode:
            raise SystemExit(f"git {' '.join(a)} failed: {p.stderr.strip()}")
        return p.stdout
    names = git("ls-tree", "-r", "--name-only", ref, "results/").split()
    stamps = sorted({n.split("/")[1] for n in names if n.count("/") >= 2})
    if not stamps:
        raise SystemExit(f"No results/<stamp> folders found on {ref}.")
    tmp = Path(tempfile.mkdtemp())
    for stamp in stamps[-2:]:
        for n in names:
            if n.startswith(f"results/{stamp}/"):
                dst = tmp / stamp / Path(n).name
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(git("show", f"{ref}:{n}"))
    return tmp


def _v2(result):
    return {c: r["v2"] for c, r in result.items() if "v2" in r}


def check(run, previous, inputs=(), stale_days=STALE_DAYS, now=None):
    """Return a list of {level, check, message, action} dicts."""
    now = now or time.time()
    out = []
    add = lambda level, name, msg, action="": out.append({"level": level, "check": name, "message": msg, "action": action})

    if "missing" in run:
        add("ALERT", "complete_run", f"The last run is incomplete; missing {run['missing']}.",
            "A person should look at why run_suite.py stopped (check the unit-test line in SUMMARY.md).")
        return out
    m = re.search(r"unit tests: (\w+)", run["summary"])
    if not m or m.group(1) == "FAIL":
        add("ALERT", "unit_tests", "The unit tests did not pass on the last run.", "Do not trust the numbers. A person should investigate.")
    elif m.group(1) == "skipped":
        add("WARN", "unit_tests", "The last run skipped the unit tests.", "Re-run without --skip-tests.")

    for path in inputs:
        p = Path(path)
        if not p.is_file():
            add("ALERT", "inputs_exist", f"Input file is missing: {p}.", "Rebuild the inputs (a person must do the FracFocus download).")
            continue
        age = (now - p.stat().st_mtime) / 86400
        if age > 2 * stale_days:
            add("ALERT", "inputs_fresh", f"{p.name} is {age:.0f} days old (limit {stale_days}).",
                "A person must refresh the data: download the FracFocus CSV (terms must be accepted by a person) and rebuild.")
        elif age > stale_days:
            add("WARN", "inputs_fresh", f"{p.name} is {age:.0f} days old (limit {stale_days}).",
                "Plan a data refresh: FracFocus terms must be accepted by a person.")

    water = run["data"]["water_no_delay"]
    v = rs.verdict(water)
    ran = v["counties"]
    if ran:
        def share(s):
            a, b = s.split(" of ")
            return int(a) / int(b) if int(b) else 1.0
        tot, turn = share(v["better_on_13wk_total"]), share(v["better_at_turning_points"])
        if tot < MIN_SHARE_BETTER or turn < MIN_SHARE_BETTER:
            add("ALERT", "beats_baseline",
                f"The forecast beat the trailing average on 13-week totals in {v['better_on_13wk_total']} counties and at turning points in "
                f"{v['better_at_turning_points']} (needs at least {MIN_SHARE_BETTER:.0%} of each).",
                "A person must review before anyone relies on it. Do NOT retune the model to make this pass.")
    else:
        add("ALERT", "beats_baseline", "No county produced a result.", "A person should check the inputs and the join.")

    for name, label in RUNS.items():
        res = run["data"][name]
        biased = [c for c, r in _v2(res).items() if abs(r.get("median_13wk_total_bias") or 0) > BIAS_WARN]
        if len(biased) * 2 > max(1, len(_v2(res))):
            add("WARN", "bias", f"The {label} forecast is off by more than {BIAS_WARN:.0%} on average in {len(biased)} counties.",
                "Review the bias before relying on this forecast.")
    low = [c for c, r in water.items() if "v2" in r and (r.get("match_rate") or 0) < LOW_MATCH]
    if low:
        add("INFO", "low_match", f"Counties where fewer than {LOW_MATCH:.0%} of permits matched a frac (unreliable): {low}.", "Do not trust the forecast there.")

    if previous and "data" in previous:
        worse = []
        for c, r in _v2(water).items():
            old = _v2(previous["data"]["water_no_delay"]).get(c)
            a, b = r.get("median_abs_13wk_total_error"), (old or {}).get("median_abs_13wk_total_error")
            if a is not None and b is not None and b > 0 and (a - b) / b > REGRESS_REL and (a - b) > REGRESS_ABS:
                worse.append(f"{c} ({b:.3f} -> {a:.3f})")
        if len(worse) >= REGRESS_ALERT_COUNTIES:
            add("ALERT", "regression", f"13-week error got clearly worse since the previous run in {len(worse)} counties: {worse}.",
                "A person should check whether the data changed. Do NOT retune the model.")
        elif worse:
            add("WARN", "regression", f"13-week error got clearly worse since the previous run in: {worse}.", "Watch the next run.")
    return out


def overall(checks):
    levels = {c["level"] for c in checks}
    return "ALERT" if "ALERT" in levels else "WARN" if "WARN" in levels else "OK"


def write_outputs(out_dir, status, checks, run_name, policy):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"status": status, "run": run_name, "generated_at": datetime.now().isoformat(timespec="seconds"),
               "checks": checks, "policy": policy}
    (out_dir / "status.json").write_text(json.dumps(payload, indent=1))
    alert = out_dir / "ALERT.md"
    if status == "OK":
        alert.unlink(missing_ok=True)
        return
    lines = [f"# Frac forecast watchdog: {status}", "", f"Run checked: {run_name}", ""]
    for c in checks:
        if c["level"] in ("ALERT", "WARN", "INFO"):
            lines += [f"- **{c['level']}** ({c['check']}): {c['message']}"] + ([f"  - What to do: {c['action']}"] if c["action"] else [])
    lines += ["", "Bots must not accept website terms, delete data, push anywhere but claude/frac-results, or change the model to clear this.",
              "See integration/god-mode/autonomy-policy.json."]
    alert.write_text("\n".join(lines))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--results", help="run folder, or a folder of timestamped run folders")
    src.add_argument("--results-ref", help="git ref holding published results, e.g. origin/claude/frac-results")
    ap.add_argument("--out", required=True)
    ap.add_argument("--inputs", nargs="*", default=[], help="input CSVs whose age should be checked")
    ap.add_argument("--stale-days", type=int, default=STALE_DAYS)
    ap.add_argument("--repo", default=str(HERE.parents[1]), help="git repo used with --results-ref")
    args = ap.parse_args(argv)

    if any(os.environ.get(k) for k in ("FD_KILL_SWITCH", "FD_KILL_WATCH")):
        print("Kill switch set: watchdog did nothing.")
        return 0
    results = extract_from_git(args.results_ref, Path(args.repo).expanduser()) if args.results_ref else Path(args.results).expanduser()
    latest, prev = latest_two(results)
    if latest is None:
        raise SystemExit(f"No run folder with SUMMARY.md found under {results}.")
    policy = json.loads((HERE.parents[1] / "integration/god-mode/autonomy-policy.json").read_text()) \
        if (HERE.parents[1] / "integration/god-mode/autonomy-policy.json").is_file() else {}
    checks = check(load_run(latest), load_run(prev) if prev else None, args.inputs, args.stale_days)
    status = overall(checks)
    write_outputs(Path(args.out).expanduser(), status, checks, latest.name, policy)
    print(json.dumps({"status": status, "run": latest.name, "problems": [c["message"] for c in checks if c["level"] != "INFO"]}, indent=1))
    return {"OK": 0, "WARN": 1, "ALERT": 2}[status]


if __name__ == "__main__":
    raise SystemExit(main())
