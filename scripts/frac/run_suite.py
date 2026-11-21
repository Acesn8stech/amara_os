#!/usr/bin/env python3
"""Run the whole frac-forecast evaluation in one go and write a plain-English summary.

What it does (stdlib only):
  1. runs the unit tests (unless --skip-tests)
  2. loads permits.csv and frac.csv ONCE
  3. for each county, runs the original and fixed models, for water and for sand, with and without a
     filing delay (compare_counties), and writes one JSON per run
  4. prints 5 random joined wells per county for the hand check (gate G1)
  5. writes SUMMARY.md: a table per run and a one-line verdict, with the caveats
  6. with --publish, pushes ONLY the summary files (.json/.md/.txt) to the branch claude/frac-results,
     never the CSVs or zips, so results can be read without sharing raw data.

Example:
  python3 scripts/frac/run_suite.py --permits ~/frac-data/work/permits.csv --frac ~/frac-data/work/frac_v2.csv \\
      --out ~/frac-data/work/results --publish
"""
import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ff = _load("ff", "frac_forecast.py")
sj = _load("sj", "sample_join.py")

DEFAULT_COUNTIES = "REEVES,LOVING,MIDLAND,MARTIN,HOWARD,UPTON,CULBERSON,PECOS,WARD,WINKLER"
ALLOWED_SUFFIXES = {".json", ".md", ".txt"}
MAX_PUBLISH_BYTES = 5 * 1024 * 1024
CAVEATS = """\
## Read this before trusting any number

- Counties used to DESIGN the fixed model: Reeves only. Every other county is an out-of-sample test.
- The filing delay is ASSUMED (default 45 days), not measured.
- "Turning point" = the next 13 weeks differ from the previous 13 by more than 25%.
- Sand = RECORDED pounds only; the prediction is scaled by the recent share of fracs that report a figure.
- Counties where fewer than ~70% of permits match a frac are unreliable (see the match% column).
- Backtests replay history; they do not prove a live forecast. Weekly accuracy is about a tie with a
  trailing average: use this as a 13-week planning signal.
"""


def run_tests(repo_root):
    cmd = [sys.executable, "-m", "unittest", "scripts/frac/test_frac_forecast.py", "scripts/frac/adapters/test_adapters.py",
           "scripts/frac/test_run_suite.py", "scripts/frac/test_watchdog.py",
           "scripts/frac/test_live.py", "scripts/frac/test_desk.py"]
    p = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)
    tail = [ln for ln in p.stderr.strip().splitlines() if ln][-3:]
    return p.returncode == 0, " | ".join(tail)


def _cmp(county_result, key):
    return county_result.get(key) or {}


def verdict(result):
    """Counts across counties that ran: v2 vs the trailing-average baseline."""
    ran = {c: r for c, r in result.items() if "v2" in r}
    tot = turn = weekly = tot_cnt = turn_cnt = 0
    for r in ran.values():
        v2 = r["v2"]
        if v2.get("median_abs_13wk_total_error") is not None and v2.get("baseline_median_abs_13wk_total_error") is not None:
            tot_cnt += 1
            tot += v2["median_abs_13wk_total_error"] < v2["baseline_median_abs_13wk_total_error"]
        tp = v2.get("turning_point_windows")
        if tp:
            turn_cnt += 1
            turn += tp["model_median_abs_error"] < tp["baseline_median_abs_error"]
        if v2.get("median_model_wape") is not None:
            weekly += v2["median_model_wape"] < v2["median_baseline_wape"]
    return {"counties": len(ran), "better_on_13wk_total": f"{tot} of {tot_cnt}",
            "better_at_turning_points": f"{turn} of {turn_cnt}", "better_weekly": f"{weekly} of {len(ran)}"}


def table(result):
    lines = ["| county | match% | v1 13-wk err | v2 13-wk err | baseline 13-wk err | v2 bias | v2 turning err | baseline turning err |",
             "|---|---|---|---|---|---|---|---|"]
    f = lambda x: "-" if x is None else f"{x:.3f}"
    for c, r in result.items():
        if "v2" not in r:
            lines.append(f"| {c} | - | {r.get('status', '?')} | | | | | |")
            continue
        v1, v2 = r["v1"], r["v2"]
        tp = v2.get("turning_point_windows") or {}
        lines.append(f"| {c} | {r['match_rate'] * 100:.0f} | {f(v1.get('median_abs_13wk_total_error'))} | "
                     f"{f(v2.get('median_abs_13wk_total_error'))} | {f(v2.get('baseline_median_abs_13wk_total_error'))} | "
                     f"{f(v2.get('median_13wk_total_bias'))} | {f(tp.get('model_median_abs_error'))} | "
                     f"{f(tp.get('baseline_median_abs_error'))} |")
    return "\n".join(lines)


def build_summary(runs, meta, tests, samples):
    out = [f"# Frac forecast evaluation ({meta['stamp']})", "",
           f"- unit tests: {tests}", f"- counties: {meta['counties']}", f"- backtests from: {meta['from_date']}",
           f"- assumed filing delay for the delay runs: {meta['lag_days']} days", ""]
    for name, (title, result) in runs.items():
        v = verdict(result)
        out += [f"## {title}", "",
                f"**v2 vs trailing average:** better on 13-week totals in {v['better_on_13wk_total']} counties, "
                f"better at turning points in {v['better_at_turning_points']}, better weekly in {v['better_weekly']}.", "",
                table(result), ""]
    out += ["## Hand check (gate G1): look 3 or more of these up on the RRC and FracFocus sites", ""]
    out += [f"- {s}" for s in samples] or ["- (none)"]
    out += ["", CAVEATS]
    return "\n".join(out)


def publish(out_dir, repo_root, branch="claude/frac-results"):
    """Commit only summary files to an orphan-style results branch and push it. Raw data never goes."""
    out_dir = Path(out_dir)
    files = sorted(p for p in out_dir.iterdir() if p.is_file())
    bad = [p.name for p in files if p.suffix.lower() not in ALLOWED_SUFFIXES or p.stat().st_size > MAX_PUBLISH_BYTES]
    if bad:
        raise SystemExit(f"REFUSING to publish: not summary-sized .json/.md/.txt files: {bad}")
    if not files:
        raise SystemExit("Nothing to publish.")

    def git(*args, cwd=repo_root):
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        if p.returncode:
            raise SystemExit(f"git {' '.join(args)} failed: {p.stderr.strip()}")
        return p.stdout

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    work = Path(tempfile.mkdtemp()) / "results-worktree"
    remote_has = bool(git("ls-remote", "--heads", "origin", branch).strip())
    tmp_branch = f"frac-results-publish-{stamp}"      # private name: a local branch of the user's is never touched
    try:
        if remote_has:
            git("fetch", "origin", branch)
            git("worktree", "add", "--detach", str(work), f"origin/{branch}")
            git("checkout", "-b", tmp_branch, cwd=work)
        else:
            git("worktree", "add", "--detach", str(work), "HEAD")
            git("checkout", "--orphan", tmp_branch, cwd=work)
            git("rm", "-rf", "--quiet", ".", cwd=work)
        base, n = stamp, 1
        while (work / "results" / stamp).exists():
            n += 1
            stamp = f"{base}-{n}"
        dest = work / "results" / stamp
        dest.mkdir(parents=True)
        for p in files:
            (dest / p.name).write_bytes(p.read_bytes())
        git("add", "results", cwd=work)
        git("commit", "-q", "-m", f"frac evaluation results {stamp} (summary numbers only)", cwd=work)
        git("push", "origin", f"{tmp_branch}:refs/heads/{branch}", cwd=work)
        return f"pushed results/{stamp} to origin/{branch}"
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(work)], cwd=repo_root, capture_output=True)
        subprocess.run(["git", "branch", "-D", tmp_branch], cwd=repo_root, capture_output=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--permits", required=True)
    ap.add_argument("--frac", required=True, help="use frac_v2.csv: water is identical, sand coverage is higher")
    ap.add_argument("--out", required=True)
    ap.add_argument("--counties", default=DEFAULT_COUNTIES)
    ap.add_argument("--from-date", default="2016-01-01")
    ap.add_argument("--lag-days", type=int, default=45)
    ap.add_argument("--weeks", type=int, default=13)
    ap.add_argument("--skip-tests", action="store_true")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--branch", default="claude/frac-results")
    args = ap.parse_args(argv)

    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    repo_root = HERE.parents[1]
    from_date = ff.parse_date(args.from_date)
    if not from_date:
        sys.exit("--from-date must be YYYY-MM-DD")
    counties = [c.strip().upper() for c in args.counties.split(",") if c.strip()]

    tests = "skipped"
    if not args.skip_tests:
        ok, tail = run_tests(repo_root)
        tests = ("PASS: " if ok else "FAIL: ") + tail
        if not ok:
            print(f"Unit tests FAILED, stopping: {tail}", file=sys.stderr)
            (out / "SUMMARY.md").write_text(f"# Evaluation stopped\n\nUnit tests failed: {tail}\n")
            return 1

    permit_rows, frac_rows = ff.read_csv(Path(args.permits).expanduser()), ff.read_csv(Path(args.frac).expanduser())
    permits, _ = ff.clean_permits(permit_rows)
    fracs, _ = ff.clean_fracs(frac_rows)

    plan = {"water_no_delay": ("Water, no filing delay", "water", 0),
            "water_with_delay": (f"Water, assumed {args.lag_days}-day filing delay", "water", args.lag_days),
            "sand_no_delay": ("Sand (recorded pounds), no filing delay", "proppant", 0),
            "sand_with_delay": (f"Sand (recorded pounds), assumed {args.lag_days}-day filing delay", "proppant", args.lag_days)}
    runs = {}
    for key, (title, metric, lag) in plan.items():
        print(f"running: {title} ...", file=sys.stderr, flush=True)
        result = ff.compare_counties(permits, fracs, counties, args.weeks, from_date, metric, lag)
        (out / f"{key}.json").write_text(json.dumps(result, indent=1))
        runs[key] = (title, result)

    samples = []
    for c in counties[:3]:
        rows, rep = sj.sample(permit_rows, frac_rows, county=c, n=5, seed=1)
        samples += [f"[{c}] API {r['api']} | permit# {r['permit_number']} | lease {r['lease_name']} | {r['operator']} | "
                    f"permitted {r['permit_date']} | frac {r['frac_start']} | water {r['water_gal']} gal | proppant {r['proppant_lb']} lb"
                    for r in rows]
    meta = {"stamp": datetime.now().strftime("%Y-%m-%d %H:%M"), "counties": ", ".join(counties),
            "from_date": args.from_date, "lag_days": args.lag_days}
    summary = build_summary(runs, meta, tests, samples)
    (out / "SUMMARY.md").write_text(summary)
    print(summary)
    if args.publish:
        print(publish(out, repo_root, args.branch), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
