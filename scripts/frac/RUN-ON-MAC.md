# Running the frac forecast on real data (Scott's Mac)

All commands run from the repo root, e.g. `cd ~/amara_os` (or the worktree). Python 3.10+, no installs.
Outputs go to `~/frac-data/work/`, outside the repo and outside Dropbox backups.

## 0. One-time
    mkdir -p ~/frac-data/work

## 1. Check the RRC file matches the manual (prints the first record with a ruler)
    python3 scripts/frac/adapters/daf318_to_permits.py --input ~/frac-data/rrc/daf318 --probe
Look at the parsed fields: county_name should be a county, operator_name a company, the date
parts digits. If the run stops with "does not match the layout", STOP and tell Claude.

## 2. Build permits.csv (all Texas horizontal permits, or one county)
    python3 scripts/frac/adapters/daf318_to_permits.py --input ~/frac-data/rrc/daf318 --output ~/frac-data/work/permits.csv
lateral_ft is blank for every row: the RRC publishes no planned lateral length.

## 3. See which FracFocus "Purpose" values count as proppant (read-only, streams the zip)
    python3 scripts/frac/adapters/fracfocus_to_frac.py --zip ~/frac-data/fracfocus/FracFocusCSV.zip --list-purposes
Check that every value listed really means proppant (sand, ceramic, etc.).

## 3b. Why are so many proppant figures unknown? (read-only, a few minutes)
    python3 scripts/frac/adapters/fracfocus_to_frac.py --zip ~/frac-data/fracfocus/FracFocusCSV.zip --diagnose
Per year: how many Texas jobs, how many have an ingredient mass at all, how many have a proppant-purpose
row with a mass, and how many have a sand-like ingredient name with a mass. A big gap between the last two
means proppant is recorded under a purpose the matcher does not recognize. Paste the result to Claude.

## 3c. Re-run the diagnostic (it now also tests the percent method against jobs that have both numbers)
    python3 scripts/frac/adapters/fracfocus_to_frac.py --zip ~/frac-data/fracfocus/FracFocusCSV.zip --diagnose
Look at "percent_method_calibration": the median of "estimate_divided_by_recorded_mass" near 1.0 with a
tight p25-p75 means the percent estimate is trustworthy. Do NOT turn it on until Claude has read this.

## 3d. Short diagnostic (use this one; the full one prints a wall of text)
    python3 scripts/frac/adapters/fracfocus_to_frac.py --zip ~/frac-data/fracfocus/FracFocusCSV.zip --diagnose --brief
Shows coverage for 2013-now and the last 3 years, the typical pounds-per-gallon for recorded vs derived jobs,
and where large sand hides when it is not labeled proppant.

## 4. Build frac.csv (streams the zip, never extracts it; Texas only)
    python3 scripts/frac/adapters/fracfocus_to_frac.py --zip ~/frac-data/fracfocus/FracFocusCSV.zip --output ~/frac-data/work/frac.csv
It prints which job-key column it used. If it stops on a header mismatch, tell Claude.
Jobs dated before 2000 or after next year are rejected (entry errors such as 1955 or 3012).
Optional, in this order of trust (each is labeled in the proppant_basis column):
  1. recorded pounds on a proppant-labeled row (always used first),
  2. `--sand-by-name`: sand-like ingredients (silica, quartz, ceramic...) of 100,000 lb or more, whatever the label.
     Check `--diagnose --brief` -> "sand_name_rule_over_purpose_labeled_mass": a median near 1.0 means it matches;
     near 2.0 means the same sand is listed twice and the rule double counts.
  3. `--derive-proppant-from-percent`: estimate from percent of job fluid (calibrated: median 0.997 vs recorded).
Optional, ONLY after the calibration check passes: add `--derive-proppant-from-percent` to estimate
proppant where pounds are blank (labeled DERIVED in the proppant_basis column).

## 5. Gate G1: check 10 joined wells by hand
    python3 scripts/frac/sample_join.py --permits ~/frac-data/work/permits.csv --frac ~/frac-data/work/frac.csv --county REEVES
Look 3 or more of them up (RRC drilling permit query by API, FracFocus "Find a Well" by API) and
confirm date, operator and volumes match.

## 6. Backtest (gates G2/G3) and a live forecast
    python3 scripts/frac/frac_forecast.py backtest --permits ~/frac-data/work/permits.csv --frac ~/frac-data/work/frac.csv --county REEVES --from-date 2016-01-01
    python3 scripts/frac/frac_forecast.py forecast --permits ~/frac-data/work/permits.csv --frac ~/frac-data/work/frac.csv --as-of 2026-11-21 --county REEVES
Add `--summary-only` to skip the long per-window list, and `--explain` to split the actual volume by why the model
could or could not predict it (permit issued after the forecast date, permit older than the cutoff, per-well
volume bias, ...). Use both together: `... backtest ... --from-date 2016-01-01 --summary-only --explain`.
`--ablate` runs the original model and three fixes added one at a time (A trimmed mean per well, B plus well sizes
from the last 24 months, C plus full-distribution timing), each split into 2016-2022 and 2023-onward, so you can see
which fix helps and whether it holds up on the later period. `--model v2` runs the full set of fixes.
`--compare-counties REEVES,PECOS,LOVING,...` runs the original and v2 models on each county separately. Because v2
was designed while looking at Reeves only, the OTHER counties are the honest out-of-sample test. Each county reports
weekly error, 13-week total error against the trailing-average baseline, and "turning point" windows (next 13
weeks differ from the last 13 by more than 25%), which is where a permit pipeline should beat a lagging average.
`--disclosure-lag-days N` hides each frac from the model (and the baseline) for N days after it starts, like a real
filing delay (the model then thinks just-fracked wells are still pending, so it over-forecasts). The default 0
assumes every frac is known instantly, which flatters the model. Use e.g. 45 as a what-if (assumed, not measured).
`--metric proppant` forecasts and scores RECORDED proppant pounds (use the rebuilt `frac_v2.csv`); the prediction is
scaled by the share of recent fracs that reported a figure so it compares like with like. `--explain` is water only.
Do not tune anything to make a gate pass. Report the numbers as printed, including "not good enough".

## Tests
    python3 -m unittest scripts/frac/test_frac_forecast.py scripts/frac/adapters/test_adapters.py

## One command for everything (use this)

    python3 scripts/frac/run_suite.py --permits ~/frac-data/work/permits.csv --frac ~/frac-data/work/frac_v2.csv --out ~/frac-data/work/results --publish

Runs the unit tests, then water and sand backtests for ten Permian counties, with and without an assumed 45-day
filing delay, prints 5 wells per county to hand-check, and writes `SUMMARY.md` plus one JSON per run into the output
folder. `--publish` pushes only those summary files to the branch `claude/frac-results` (never CSVs or zips), so the
results can be read without pasting anything. Takes several minutes.

Inside AMARA God Mode: `python3 scripts/frac/install_into_god_mode.py --target <god-mode> --apply` (dry run without
`--apply`), then `npm run fd -- ...` with the same arguments. See `integration/god-mode/INSTALL.md`.

## Live (always on, no schedule)

    # terminal 1: the live service. Stays running and re-evaluates when permits.csv or frac_v2.csv changes.
    python3 scripts/frac/live.py --permits ~/frac-data/work/permits.csv --frac ~/frac-data/work/frac_v2.csv --out ~/frac-data/work/live --port 8765 --publish
    # terminal 2: the desk board (Ctrl-C to quit)
    python3 scripts/frac/desk.py --dir ~/frac-data/work/live

Stop the service: Ctrl-C in its terminal, or `touch ~/frac-data/work/live/STOP`. It writes live_status.json and
live_events.jsonl (one JSON line per event, also printed, so a Claude session can stream it with the Monitor tool).
It never downloads data, never accepts website terms, and never changes the model. If it stops after repeated
failures it writes ALERT.md: read it before restarting.
