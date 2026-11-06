# Open Acreage Sync lane

Code: `src/lib/open-acreage/` · Jobs: `hermes/open-acreage.jobs.json` · CLI: `npm run oa -- <job>` · Schema: `supabase/migrations/20261106000000_god_mode_og.sql`
Tests: `npm run test:open-acreage` · API: `GET /api/open-acreage/layers`, `POST /api/open-acreage/voice`

Run order on the Mac Mini: apply migration → set `OA_TARGET_COUNTIES` → `npm run oa -- all` (key-less local JSON store unless Supabase env is set) → read run notes (UNMAPPED / UNVERIFIED) → fill RRC layouts.
House rules enforced in code: NULL not guesses; records without source are rejected; candidate status capped at `CANDIDATE — TITLE CHECK REQUIRED`; stale source → HOLD; >20% count drop halts promote.
Phase 2 (not built): NM State Land Office layer, title-check handoff to Grok/DEED agents, EIA price feed.
