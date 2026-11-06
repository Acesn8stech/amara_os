# Open Acreage Sync — First Run Report

Constitution: **v2026.11.06.1** · Run date: 2026-11-06 · Environment: Claude Code cloud build sandbox (not the Mac Mini)

**Lane status: NOT COMPLETE.** Code and tests are done; no live source data has been pulled. The sandbox egress policy
returned HTTP 403 (CONNECT denied) for every Phase 1 host, and the pipeline reported that instead of substituting anything.
Per the brief ("do not mark complete until the first run report is delivered"), a real first run on the Mac Mini is still required.

| Source | Records pulled | Valid | Rejected | source_updated_at | State |
|---|---|---|---|---|---|
| UL — UL_Section, OG_Lease, AvailableParcels, WellSHL, Wellbore | 0 | 0 | 0 | NULL (unknown) | **UNREACHABLE** (403 from sandbox proxy: services3.arcgis.com) |
| BLM — Natl MLRS O&G Leases | 0 | 0 | 0 | NULL (unknown) | **UNREACHABLE** (403: gis.blm.gov) |
| GLO | 0 | 0 | 0 | n/a | **NOT FOUND** (connector stubbed) |
| RRC — permits, production oil/gas | 0 | 0 | 0 | n/a | **NOT FOUND** (no files staged) + layouts **UNVERIFIED** |

Candidates produced: 0. Target county list is empty (Scott sets it), and every required source is stale, so any candidate would be HOLD / `STALE_SOURCE`.

## UNVERIFIED / NOT FOUND items

1. **UL field names — UNVERIFIED.** The layer metadata could not be read, so the mapper looks fields up from alias lists. Anything unmatched is stored NULL with `status_normalized = UNKNOWN` and listed as `UNMAPPED canonical fields` in the run notes. After the first live run, check those notes and extend `A` in `connectors.ts`.
2. **UL record id stability — UNVERIFIED.** `source_record_id` is the layer's OBJECTID. If UL republishes monthly with renumbered OBJECTIDs, the diff engine will report spurious NEW/REMOVED. Check against a stable key once the fields are visible.
3. **UL terms of use — NOT FOUND** on the GIS page. Do not redistribute publicly.
4. **UL `editingInfo.lastEditDate` — UNVERIFIED.** If a layer does not state it, `source_updated_at` is NULL and that layer is fully re-pulled every run.
5. **BLM layer, fields, and state filter — UNVERIFIED.** The connector discovers them at run time: layers named /lease/, a state field, and the NM/TX values actually present. If any step fails it reports UNVERIFIED and pulls nothing, and does not guess.
6. **GLO endpoint — NOT FOUND.** Search confirms the page [GIS Maps and Data](https://www.glo.texas.gov/land/gis-maps-and-data) lists Oil & Gas Leases (active/inactive) and Lease Sale Nominated Tracts. No REST service or direct download URL could be confirmed, so the connector is a stub. The OTLS land grid is intentionally not ingested. `GLO_INACTIVE_LEASE` and `GLO_NOMINATED_TRACT` cannot fire until a source exists.
7. **RRC file names, links, and record layouts — UNVERIFIED.** Per the [RRC download page](https://www.rrc.texas.gov/resource-center/research/data-sets-available-for-download/) as seen in search: Drilling Permit Master is ASCII, Statewide Production Oil/Gas is EBCDIC, monthly. The manuals with field offsets were not reachable. The decoder (EBCDIC cp037, zoned, packed, ASCII) is built and tested on synthetic records. The shipped layouts in `rrc-layouts.ts` are empty and marked unverified, so the decoder refuses to run until Scott or Cursor fills them from the manuals. The download step also needs the real file links.
8. **Supabase adapter — UNTESTED against a live project** (no keys). Check on first run that PostGIS accepts the GeoJSON geometry the adapter sends.
9. **Production-to-well join — assumption to confirm.** Production rows link to wells by `well_or_lease_id` = well `api_no` or `source_record_id`. RRC production is keyed by lease, so this join may need a lease-to-well crosswalk.

## Verification performed
- `npm run test:open-acreage` — 16 / 16 pass (the 5 required tests plus candidate rules, gate, diff, pagination, 403 no-retry, kill switch, EBCDIC decode, voice).
- `tsc --noEmit` and `eslint` clean on the new code. `next build` was not run.
