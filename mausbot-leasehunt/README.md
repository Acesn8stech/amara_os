# mausbot-leasehunt

Multi-agent Slack orchestration for an oil & gas mineral lease acquisition and double-closing
("finders and paper") operation. Six seats, each on its own machine, coordinate through Slack
channels and one shared state database.

| Seat | Slack | Channel | Runner |
|---|---|---|---|
| `mausbot-cos` | @MausBot — Chief of Staff | `#dispatch-ops`, `#exec-briefings` | `python -m agents.cos_orchestrator` |
| `docket-bot` | @DocketBot | `#docket-feed` | `python -m agents.docket_worker` |
| `courthouse-bot` | @CourthouseBot | `#title-expiration` | `python -m agents.courthouse_worker` |
| `abstractor-bot` | @AbstractorBot | `#abstract-title` | `python -m agents.abstractor_worker` |
| `trace-bot` | @TraceBot | `#owner-intel` | `python -m agents.trace_worker` |
| `cfo-bot` | @CFOBot | `#underwriting-cfo` | `python -m agents.cfo_worker` |

```
            ┌────────────── #dispatch-ops: WORK_ORDER / REVISION_REQUEST ──────────────┐
 MausBot ───┤                                                                          ├──► worker
   ▲  │     └──────── worker's channel: WORKER_RECEIPT (G4-checked twice) ◄────────────┘
   │  └─► #exec-briefings: Tier-1 Deal Card + [Approve & Generate Packets][Request Field Abstractor][Hold][Reject]
   └── shared DB (tracts, work_orders, locks, runs_sheets, owners, comps, expenses, audit_log)
```

Pipeline: `MARKET_CHECK` → `DOCKET_SCAN` → `COURTHOUSE_SCREEN` → (`ABSTRACT_TITLE` ‖ `TRACE_OWNERS`) →
`UNDERWRITE` → gates + score → Deal Card → Principal button → `ASSEMBLE_CLOSING_PACKET`.

## What the code enforces (not just the prompts)

| Rule | Where |
|---|---|
| **G4 Evidence-or-Null** — every non-null claim needs a Book/Page, Docket #, or http(s) record URL; secondary domains can't be grade A/B | `base_worker.validate_claims/sanitize_claims`. Workers null uncited values; MausBot re-validates every receipt and issues a `REVISION_REQUEST` (max 2, then `HELD_G4`). |
| **Hard gates**: title clean *or* curative priced · all comps & title records grade A/B · `net_spread_per_nma ≥ $50` · kill-switch off | `base_worker.evaluate_hard_gates`, called by `cfo_worker` (before it reports eligibility), `cos_orchestrator` (before escalating) and again at button-click time. |
| **Cyclicality kill-switch** — WTI / Henry Hub below floors or permits down >N% vs baseline. Missing data fails *closed*. Recomputed from raw numbers, never from a stored flag | `evaluate_kill_switch`; acquisition tasks refuse to dispatch while ON, docket monitoring continues. |
| **Single-tract-first** — only one tract in the deep pipeline until one has reached `PACKETS_GENERATED` | `MausBotCOS.advance/pump_queue`. |
| **No closing before `TITLE_VERIFIED_CLEAN`** (stricter than escalation: priced curative can reach the card, never a closing) | `cfo_worker._packet`, `MausBotCOS.handle_action`, `AbstractorWorker.closing_blocked`. |
| **No duplicate scraping across machines** — unique live work order per (tract, task) + atomic `tract_locks` with expiry | `db/schema.sql` partial unique index, `db/lock_manager.py`. |
| Principal-only buttons / write commands | `PRINCIPAL_USER_IDS`; unauthorized clicks are audited. |
| Scoring 0–100 (spread 35 · title 25 · evidence 15 · buyer 15 · docket timing 10); Tier-1 ≥ 80 | `cos_orchestrator.score_tract` |

`net_spread_per_nma = ((exit − offer) × NMA − ledger overhead − curative − closing) ÷ NMA`, after the
JV partner's share when `JV_SPLIT`. The 1–3 % ORRI is reserved in the assignment and is upside only —
it is **not** counted toward the spread gate.

## Setup

### 1. Slack apps (one per seat)
Socket Mode spreads events across all connections of **one** app, so six machines need six apps.
```bash
python scripts/render_manifests.py          # -> build/manifests/<seat>.yaml (from slack-app-manifest.yaml)
```
For each file: *api.slack.com/apps → Create New App → From a manifest*, install it, then copy the
**Bot token** (`xoxb-…`) and create an **App-level token** with `connections:write` (`xapp-…`).
Only the MausBot app has slash commands (`/leasehunt`, `/field-ticket`) and interactivity.

### 2. Channels
```bash
SLACK_BOT_TOKEN=xoxb-… python -m scripts.bootstrap_channels
```
Creates `#dispatch-ops #docket-feed #title-expiration #abstract-title #owner-intel #underwriting-cfo`
and a private `#exec-briefings`. Invite all six bots to the first six; invite MausBot and the Principal
to `#exec-briefings`. (Workers only read/post their own channel + `#dispatch-ops`.)

### 3. Shared database
* **Local dev (one machine):** `DATABASE_URL=sqlite:///leasehunt.db`
* **Multi-machine:** a Supabase/Postgres URL (`postgresql+psycopg://…`) in every machine's `.env`.

`db/schema.sql` is portable SQL (TEXT ids, ISO timestamps, JSON-as-TEXT, integer cents). Initialize once:
```bash
python -m scripts.init_db
```
Lock acquisition uses `INSERT … ON CONFLICT … WHERE expired OR mine RETURNING` — one atomic statement on
SQLite ≥ 3.35 and Postgres.

### 4. Each machine
```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # set SLACK_BOT_TOKEN / SLACK_APP_TOKEN for THIS seat, DATABASE_URL, PRINCIPAL_USER_IDS
python -m agents.<seat>_worker        # or: python -m agents.cos_orchestrator
```
Start MausBot last. System prompts live in `prompts/` and are used for human `@mention` conversations
(LLM via `ANTHROPIC_API_KEY`); the work itself is done by the deterministic engines in `agents/`.

### 5. First run
```
/leasehunt market 72.40 3.10 118 120      # WTI, Henry Hub, permits now, permit baseline (seed the baseline!)
/leasehunt scan OK OK_OCC
/leasehunt loi <tract_id> 420 "Acme Energy" https://…/loi.pdf     # same-day back-to-back LOI (or load buyer_demand)
/leasehunt expense <tract_id> COPIES 18.50 certified lease copy receipt=RCPT-42
/leasehunt status | tract <id> | ledger <id> | resume
```
A Tier-1 tract appears in `#exec-briefings` with the four buttons. Packets are written to
`PACKET_DIR/<tract_id>/` (set `COMPANY_*` / `ESCROW_AGENT_*` on the CFOBot machine).

## Data adapters (what is and is not included)
The screening/title/underwriting engines are complete and tested. **Live portal scraping is not
shipped**: TX RRC, OK OCC, CO ECMC, ND DMR and each county clerk differ and change often. Each worker
reads normalized records through a small interface (`DocketSource`, `ClerkIndex`, `RunsSource`,
`OwnerSource`) with a JSON-file/URL implementation (`DOCKET_FEED`, `CLERK_FEED`, `RUNS_FEED`,
`OWNER_FEED`). Write one adapter per portal that emits these shapes (see the fixtures in
`tests/test_pipeline_e2e.py`: `filing()`, `lease_record()`, `runs_sheet()`). Terms of use and
robots rules for each portal are yours to check. Texas has no PLSS grid, so APD halos there are
reported `ABSTRACT_GIS_REQUIRED` rather than guessed. `fetch_eia_spot` (CFOBot) targets the EIA v2 API
and has not been run against the live service.

## Tests
```bash
pytest tests/test_pipeline_e2e.py
```
Simulates a Grady County, OK tract through all six bots on an in-memory Slack transport and a real
SQLite store: market check → docket → expiration screen → runs sheet/NMA + owner trace → underwriting →
gates → Block Kit card → Approve → packets. Also covers each gate failing, kill-switch pause/resume and
re-check at approval, G4 nulling/revision/hold, locks and duplicate dispatch, single-tract-first, field
tickets, defect detection, NMA fractions, PLSS halos, and the ledger. `BoltTransport`, the slash-command /
action wiring in `run_cos`, and the LLM `@mention` path need live Slack/Anthropic credentials and are not
covered by tests.

## Caveats
* Templates in `templates/` are model forms, not legal advice; a licensed attorney in each state must
  adapt them (top-lease, homestead, ORRI anti-washout and abstractor licensing rules vary).
* AbstractorBot produces a run sheet, not a title opinion. Curative cost figures are planning
  estimates (`CURATIVE_COST_TABLE` to override), never cited as facts.
* An expired lease with no extension of record is treated as a *closing requirement* (record the
  affidavit of non-production), not a chain defect; a lease with no term shown or a live term is a defect.
