# Frac Demand Forecast — Phase 0 Spec (DRAFT, not built)

Goal: predict **where and when** frac crews will need sand and water, weeks before the
trucks are ordered, so hauling capacity can be sold ahead of demand.

Status: proposal only. Nothing here has been built or backtested. Every figure the system
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
