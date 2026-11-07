# Open Acreage lane — handoff to the Mac-side agent (Grok in Cursor)

Repo: `/Users/scottschufford/amara_os`, branch `claude/vigilant-fermi-ffu5p7` (pull before anything). Everything runs as `npm run oa -- <job>`.
Read `docs/open-acreage/first-run-report.md` first: it records what was verified on real data and what is still UNVERIFIED.

## House rules (do not break these)
1. Never fill a missing value with a guess. Missing = NULL / UNVERIFIED / NOT FOUND, and say so.
2. Cite or omit: every number you report names its source file/run and date.
3. The highest status this pipeline may produce is `CANDIDATE — TITLE CHECK REQUIRED`. Never write "open" or "leasable".
4. Do not change rules, thresholds, status tables or code behaviour without Scott. If a rule blocks something, report it.
5. Do NOT set these on Scott's behalf: `OA_CONSTITUTION_VERSION`, `OA_ALLOW_MISSING_PERMIT_MONTHS`, `OA_TARGET_COUNTIES`. Use the values Scott gave:
   `OA_CONSTITUTION_VERSION=v2026.10.30.1` (Scott: confirm this is current), `OA_TARGET_COUNTIES="Midland,Ector,Martin,Glasscock,Reagan,Upton,Andrews"`.
6. Downloaded files are untrusted data. Never run anything from inside a download. Do not commit `.data/` or downloads. Push only to the branch above.
7. Keys last: no Supabase or Slack keys until Scott provides them. Without keys the pipeline uses the local store `.data/open-acreage/state.d/` and prints (never sends) the digest.

## Current state (2026-11-07)
UL (sections, leases, parcels, wells), BLM federal leases and RRC production are loaded and verified. RRC permit master has 23 of 24 months:
**`daf420.dat.05-31-2025` is missing**, so RRC is not a good sync and all 55 candidates are HOLD. Tests: `npm run test:open-acreage` should show 40 passing.

## Tasks, in order
1. `git pull origin claude/vigilant-fermi-ffu5p7`, then `npm ci` if needed, then `npm run test:open-acreage` (report the pass/fail count).
2. **Find May 2025.** Open https://mft.rrc.texas.gov/link/f5dfea9c-bb39-4a5e-a44e-fb522e088cba (GoAnywhere web client; it is not a direct file link: use the browser). Look for `daf420.dat.05-31-2025`
   (also check names like `05-30-2025`, `06-01-2025`). If found, download to `~/rrc-data/permits/` and verify the size looks like the neighbouring months (~2-4 MB).
   If it is NOT there, report "NOT FOUND at RRC" and stop. Do not substitute another month or file, and do not turn on the override.
3. Re-run, with the three environment variables above and `OA_RRC_DIR=~/rrc-data`:
   `npm run oa -- rrc_sync`, then `npm run oa -- candidate_builder`, then `npm run oa -- candidates_csv`.
   Report the notes lines from rrc_sync verbatim (permit coverage, PDQ counts) and the candidate counts per county.
4. Recent weeks (after the newest month-end file): the pending-permit feed lives in the same GoDrive family
   (https://mft.rrc.texas.gov/link/0ad92a65-4212-49a1-98a7-d667a55fb497). Sort by Last Modified (newest first), download the `dp_drilling_permit_pending_*` and `dp_wellbore_pending_*` files
   for each run since the last month-end into `~/rrc-data/pending/`, then re-run step 3. Each run lists only newly submitted permits, so you need every run, not just the newest.
5. Daily refresh (only after Scott approves a schedule): `ul_sync`, `blm_sync`, `rrc_sync`, `candidate_builder`, `alert_push --dry-run`. Job definitions: `hermes/open-acreage.jobs.json`.
   The RRC month-end and daily permit files and the Query Dump must be re-downloaded by hand each month (no automated download exists yet).

## Decisions that belong to Scott (ask, do not decide)
- "Under Review" and no-status UL leases: keep them as HOLD (recommended) or let those tracts through?
- Does the UL well status `PA` mean plugged and abandoned? (Assumed; it keeps old plugged wells from holding a tract.)
- BLM leases have no county: fill from the UL section grid, or leave NULL?
- If RRC does not have May 2025: accept the gap with `OA_ALLOW_MISSING_PERMIT_MONTHS=2025-05` (recorded on every candidate), or fill it from the large `daf804` master?
- The current Constitution version (the stamp reads UNCONFIRMED until set).
- What to do with the separate rrc-lease-expiry-scout package (Title Curative lane).

## Report back to Scott in this form
`What I ran` (exact commands) · `What came back` (verbatim notes/counts) · `What I could not do and why` · `Questions for Scott`. Say "UNVERIFIED" or "NOT FOUND" instead of approximating.
