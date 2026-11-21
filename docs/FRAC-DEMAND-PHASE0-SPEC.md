# Frac Demand Forecast — Phase 0 Spec (DRAFT, not built)

Goal: predict **where and when** frac crews will need sand and water, weeks before the
trucks are ordered, so hauling capacity can be sold ahead of demand.

Status: core pipeline built and tested on SYNTHETIC data only (scripts/frac/). No real Reeves data has been downloaded or backtested yet. Every figure the system
produces must carry a label: `RECORD` (taken from a source), `DERIVED` (computed from
records) or `ESTIMATE` (model output). No guessing; unknown stays `UNKNOWN`.

## 1. Pipeline

```
RRC drilling permits  ->  spud  ->  completion (W-2 / G-1)  ->  FracFocus disclosure
 (planned lateral)                   (completion date)           (actual water, proppant)
```

Forecast = new permit + operator-specific lag + operator-specific intensity.

- **Lag model:** permit approval date -> frac date, by operator, county and quarter
  (median and spread, not a single number).
- **Intensity model:** pounds of proppant and gallons of water per lateral foot, by
  operator and basin, from FracFocus joined to completion records.
- **Demand estimate per permit:** `lateral_ft x intensity`, placed on a week window
  `[permit + lag_p25, permit + lag_p75]`.
- Roll up by week, county and operator.

## 2. Sources (confirmed to exist; verify field names on first pull)

| Source | Offers | Notes |
|---|---|---|
| RRC drilling permit bulk files (Permit Master, Master and Trailer, Horizontal Drilling Permits) | permit dates, lat/long, operator, planned lateral | already located on the RRC data-sets page |
| RRC completion data (Forms W-2, G-1) | completion dates, well identity | free bulk download, same page |
| FracFocus full SQL download | per-job water volume, proppant, job dates | public; water-source detail is voluntary |
| RRC survey/well shapefiles | spatial join to survey/block/section | already in use (label DERIVED - spatial join) |

Rules: downloaded files only, no query pages scraped, no purchases. Record every download
in `inputs/MANIFEST.md` with SHA-256, as in the land builds.

## 3. Known weak points (design for them, don't hide them)

1. Many permits are never drilled; some wells are drilled and left uncompleted.
2. The lag moves with oil price and operator budgets. Use a rolling window.
3. FracFocus is filed after the job, so the newest wells have no actuals yet.
4. Joining a permit to its FracFocus record needs an API-number match. Report the match
   rate; list every miss as `NOT FOUND`.
5. Large firms already sell forecasts. The edge is local detail and hauling-specific timing.

## 4. Gates (nothing goes live until these pass)

- **G1 Join gate:** permit -> completion -> FracFocus match rate reported; 10-row re-pull
  by hand against the source pages matches.
- **G2 Backtest gate:** for each past quarter, forecast using only permits known at the
  time; compare to actual frac dates and volumes. Report median and p90 error in weeks
  and in percent of volume.
- **G3 Baseline gate:** must beat a naive baseline ("same as the last 4 weeks").
  If it does not, stop and report.
- **G4 Labeling gate:** every output row labeled RECORD / DERIVED / ESTIMATE.

## 5. Phase 0 deliverables (read-only, no UI)

1. Download and hash the sources above for one pilot area (suggest Reeves, to reuse the
   existing survey join).
2. Build the join and report match rates (G1).
3. Fit lag and intensity by operator for the pilot area; show spread, not just medians.
4. Backtest (G2, G3) and report honestly, including if the answer is "not good enough".
5. Stop and report. Phase 1 (weekly forecast command) only after approval.

## 6. Privacy and scope

No owner names or mailing addresses are needed here. Do not ingest them. Keep this project
separate from the land-lease work. Approvals follow the same rule: approved scope only,
stop and report at each gate.

## 7. Corrections and findings from the first build (2026-11-21)

1. **Proppant is not a simple FracFocus column.** The download page describes a header
   table (well, location, treatment date, total base fluid volume), an additives table and
   an ingredients table with mass percentages. Proppant mass must be DERIVED from the
   ingredient rows. Verify the real column names against the data dictionary in the
   download before writing the adapter; do not assume them. Until then `proppant_lb` may be
   blank and the forecast reports those permits as `proppant_unknown_permits`, never as zero.
2. **FracFocus terms of use.** Downloading requires accepting its terms, which include
   "data may not be altered". Scott must accept them personally; no agent should click
   through. Aggregating or combining with other datasets is allowed.
3. **API number formats differ.** RRC permits often carry 8 digits (county + well), FracFocus
   carries 14. The join key is the 10-digit well API (`42` + county + well). Sidetracks and
   multiple jobs share it, so the join uses the first job on or after the permit and reports
   `multiple_jobs_first_used`.
4. **Reuse, don't rebuild.** The God Mode branch already parses RRC Permit Master files
   (cp037, fixed-width). A permits adapter should call that parser and write `permits.csv`.
5. **Sandbox limit.** The agent sandbox cannot reach rrc.texas.gov or fracfocus.org; downloads
   and the real backtest must run on Scott's Mac. `scripts/frac/test_frac_forecast.py` proves
   the code, not the accuracy.
6. **Expect weak weekly accuracy.** In a clean synthetic world where the lag and intensity
   models are exactly right, weekly volume error (WAPE) was 0.73 against 0.86 for the flat
   baseline. That is a ceiling for real data, not a floor. Judge usefulness on 4-week and
   13-week totals as well as weekly, and report both honestly at gate G2.
7. **Censoring.** Recent permits that simply have not been fracked yet are not "never
   completed". Completion rate uses a matured cohort (older than 540 days); lag forecasts for
   waiting permits condition on how long they have already waited.

## 8. Adapters built (2026-11-21, second pass)

- **Plan simplified to two sources.** Core join = RRC Horizontal Drilling Permits (DAF318) +
  FracFocus. FracFocus carries the actual frac start date, which is better for timing than the
  W-2/G-1 completion date; Permit Master and the completion files are optional cross-checks.
- **DAF318 layout is documented** (manual: rrc.texas.gov/media/yi0p3f3j/horizontaldrillingpermits.pdf).
  Real records are 354 bytes: the manual's 360 includes 6 trailing FILLER bytes the file omits.
  The adapter accepts 354 or 360 and stops if a sample of the real file does not parse.
- **No lateral length exists in DAF318** (only PERMIT-TOTAL-DEPTH), so `lateral_ft` is blank,
  labeled UNKNOWN, and the core forecasts from per-WELL volumes (`per_well_fallback_permits`
  shows how much of each week used it). Per-foot intensity is only used if a lateral is known.
  Expect a coarser forecast than the per-foot design; a lateral-length source (e.g. FracFocus
  TVD vs measured depth, or completion data) is a future improvement.
- **Operator names come from the RRC permit** in the join, so FracFocus operator spelling
  differences do not split groups.
- **Verification status:** adapters pass 21 tests and 11/11 mutation checks, but their fixtures
  are built from the manual and dictionary, NOT from the real files. The runtime sanity gate,
  the header check and the 10-row hand check (`scripts/frac/sample_join.py`) are what confirm
  them on real data. Run guide: `scripts/frac/RUN-ON-MAC.md`.

## 9. First real FracFocus run (2026-11-21): what the data showed

- 124,168 Texas jobs. Water volume is present for nearly every job from 2013 on. 2011-2012 are mostly
  header-only (FracFocus 1.0 format): about 18,000 jobs with no ingredients and often no water.
- Proppant recorded as pounds (MassIngredient on a proppant row) exists for only ~51% of the ~105,700 jobs
  from 2013 on, though ~86% have a proppant row. Pound coverage FELL from ~79% (2014) to ~39% (2025-26).
  A forecast built from jobs with pounds therefore leans on older jobs. Treat recent sand estimates as lower
  confidence, and report coverage next to every proppant number.
- The name matcher was missing "Propping Agent" (service-company wording); fixed. The remaining gap is
  missing mass, not missing names.
- Optional fallback: estimate pounds from PercentHFJob and the base water mass (labeled DERIVED, runs low if
  the water is denser than fresh). Off by default until the calibration in `--diagnose` shows it tracks the
  recorded pounds.
- Entry-error dates (1955, 3012) exist; the adapter rejects years outside 2000..next year and the backtest
  refuses ranges over 400 windows so one bad date cannot hang it.

## 10. Second FracFocus pass (2026-11-21)

- Percent method calibrated on 53,390 real jobs: estimate / recorded pounds median 0.997 (p10 0.977, p90 1.000).
  Jobs it fills in average 1.13 lb per gallon of base water vs 1.04 for recorded jobs, similar spread.
- Water volume: 99.6% of jobs from 2013 on, 100% of the last 3 years. Water is the reliable forecast;
  sand is the weaker one. Coverage must be reported beside every sand number.
- For 35% of recent jobs, proppant-scale sand (>= 100,000 lb) is filed under labels such as "Ingredient
  Container Purpose", "Sand", "See Trade Name(s) List" or "Silica Substrate". The optional `--sand-by-name`
  rule counts sand by ingredient name above a 100,000 lb floor; its double-counting risk is measured in
  `--diagnose --brief` before use.
- Order of trust for proppant pounds: recorded on a proppant-labeled row > sand-named ingredient > percent
  estimate. Every row says which in `proppant_basis`.

## 11. First real Reeves water backtest (2026-11-21): gate G3 FAILED, and why

- 128 monthly windows from 2016: the original model beat the "last 28 days repeated" baseline in 23 (18%);
  median weekly error 0.64 of volume vs 0.52 for the baseline. Median 13-week total bias -38%
  (quartiles -54% to -13%): the model runs LOW.
- Cause split (`--explain`): 88% of actual water came from wells the model could have predicted; only 12% was
  unreachable (5.3% no comparable delay history, 5.2% permits older than the 540-day cutoff, 1.6% permits issued
  after the forecast date). Actual per-well volume was 1.23x the predicted per-well figure.
- So the miss is mostly VOLUME PER WELL (a median of skewed, growing well sizes), not inventory. Fixes added as
  options and tested one at a time (`backtest --ablate`): trimmed mean, 24-month well-size window, and timing from
  the full remaining-delay distribution instead of the middle half. Fixes were chosen from the cause split and
  reported with a 2016-2022 / 2023-onward split, but they were designed AFTER seeing results through 2026, so
  the later period is not a clean holdout. Treat the result as encouraging at best until tested on new data.
- Sand: `--sand-by-name` reproduces recorded pounds (median ratio 1.00 over 48,796 jobs) and lifts proppant
  coverage from 51% to 82% of modern jobs (83% in the last 3 years).

## 12. Reeves ablation result (2026-11-21)

| model | 13-wk total bias | median abs 13-wk total error | weekly error vs baseline (0.518) |
|---|---|---|---|
| original | -38.5% | 43.5% | 0.644 |
| A trimmed mean | -36.2% | 43.6% | 0.642 |
| B + 24-month well sizes | -28.2% | 39.6% | 0.643 |
| C + full-distribution timing (v2) | -6.7% | 23.3% | 0.516 |

- The timing change did most of the work: the middle-half placement of each permit's expected frac distorted
  when volume arrived. v2 ties the trailing-average baseline weekly (56 of 128 windows; 0.488 vs 0.484 through
  2022, 0.540 vs 0.613 from 2023). A tie is not a win and the usefulness thesis (see demand before it shows) is
  NOT yet demonstrated on real data.
- Pending: the baseline's 13-week total error, and turning-point windows, now measured by `backtest`.
  Out-of-sample test: `--compare-counties` on counties never looked at during design.
- Design caveat: v2 was designed after seeing Reeves results through 2026; Reeves is not a clean holdout.

## 13. Out-of-sample county test (2026-11-21), water, no filing delay

Ten Permian counties, v1 vs v2 (v2 designed on Reeves only, so the other nine are out of sample).

- 13-week total, median absolute error, v2 vs trailing-average baseline: Reeves .233/.256, Loving .203/.254,
  Midland .187/.242, Martin .182/.267, Howard .208/.349, Upton .171/.310, Culberson .241/.456, Winkler .562/.716,
  Ward .403/.394 (tie), Pecos .737/.458 (worse). v2 better in 8 of 10; typical error ~22% lower.
- Turning-point windows (next 13 weeks differ from the last 13 by >25%): v2 better in 9 of 10 counties, e.g.
  Martin .201 vs .470, Howard .252 vs .524, Upton .217 vs .573. Only Pecos worse (.702 vs .675).
- v2 improved on v1 in every county; median 13-week bias moved from -25% to -76% (v1) to within a few percent
  in seven counties.
- Weekly accuracy is still only a tie with the baseline. Use as a 13-week planning tool, not week by week.
- Small / poorly matched counties fail: Pecos (36% of permits matched a frac), Winkler (60%), Ward (68%), where
  weekly error is at or above 100% of volume. Do not trust the forecast where the permit-frac join is poor.
- Limits: assumes every frac is known the day it starts. FracFocus filings arrive later; `--disclosure-lag-days`
  tests this (assumed lag, not measured). Water only until `--metric proppant` results are reviewed.

## 14. Filing delay and sand results (2026-11-21), measured BEFORE the lag-aware fix

Seven counties (Reeves, Loving, Midland, Martin, Howard, Upton, Culberson), v2 vs trailing-average baseline.

- Assumed 45-day filing delay, water, 13-week total median abs error: Reeves .231/.262, Loving .234/.323,
  Midland .201/.231, Martin .180/.309, Howard .320/.349, Upton .172/.371, Culberson .252/.442. v2 better in 7 of 7,
  and better at turning points in 7 of 7. But the model over-forecast by +4% to +22% because wells that had already
  fracked (but were not yet filed) still looked pending. Weekly accuracy slipped to a tie or slightly worse.
- Sand (recorded pounds), no delay: 13-week error v2/baseline: Reeves .251/.248 (tie), Loving .195/.306,
  Midland .148/.335, Martin .210/.333, Howard .225/.335, Upton .181/.332, Culberson .299/.508. Better in 6 of 7
  (one tie); better at turning points in 7 of 7; better weekly in 5 of 7. Median bias between -9% and +3%.
- Fix added afterwards: the forecast now conditions each permit on "not fracked as of the last date we can know"
  and skips the days already past (`hazard_weeks(..., shift)`, `opts["lag_aware"]`). `--disclosure-lag-days N`
  reports it against the naive version as `v2_not_lag_aware`. NOT yet re-measured on real data.
- The real FracFocus filing delay is unmeasured; 45 days is an assumption.

## 15. Tooling

- `scripts/frac/run_suite.py`: one command runs the tests and all backtests and writes `SUMMARY.md`; `--publish`
  pushes only summary files to `claude/frac-results`.
- `scripts/frac/install_into_god_mode.py` + `integration/god-mode/`: installs this into AMARA God Mode (npm
  scripts `fd`, `fd:forecast`, `test:frac`, and a Hermes lane `hermes/frac-demand.jobs.json`) without touching the
  globe or UI.
