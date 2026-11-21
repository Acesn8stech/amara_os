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

## 4. Build frac.csv (streams the zip, never extracts it; Texas only)
    python3 scripts/frac/adapters/fracfocus_to_frac.py --zip ~/frac-data/fracfocus/FracFocusCSV.zip --output ~/frac-data/work/frac.csv
It prints which job-key column it used. If it stops on a header mismatch, tell Claude.

## 5. Gate G1: check 10 joined wells by hand
    python3 scripts/frac/sample_join.py --permits ~/frac-data/work/permits.csv --frac ~/frac-data/work/frac.csv --county REEVES
Look 3 or more of them up (RRC drilling permit query by API, FracFocus "Find a Well" by API) and
confirm date, operator and volumes match.

## 6. Backtest (gates G2/G3) and a live forecast
    python3 scripts/frac/frac_forecast.py backtest --permits ~/frac-data/work/permits.csv --frac ~/frac-data/work/frac.csv --county REEVES
    python3 scripts/frac/frac_forecast.py forecast --permits ~/frac-data/work/permits.csv --frac ~/frac-data/work/frac.csv --as-of 2026-11-21 --county REEVES
Do not tune anything to make a gate pass. Report the numbers as printed, including "not good enough".

## Tests
    python3 -m unittest scripts/frac/test_frac_forecast.py scripts/frac/adapters/test_adapters.py
