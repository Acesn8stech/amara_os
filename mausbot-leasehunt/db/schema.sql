-- mausbot-leasehunt shared state schema.
-- Portable across SQLite (local dev) and Postgres/Supabase: TEXT ids (uuid/slug generated in
-- Python), ISO-8601 UTC TEXT timestamps (lexicographically sortable), JSON stored as TEXT,
-- money stored as integer cents. Statements are separated by semicolons at end of line.

CREATE TABLE IF NOT EXISTS tracts (
    tract_id            TEXT PRIMARY KEY,
    state               TEXT NOT NULL,
    county              TEXT NOT NULL,
    legal_description   TEXT NOT NULL,
    section             TEXT,
    township            TEXT,
    range_              TEXT,
    gross_acres         REAL,
    nma                 REAL,
    source_docket       TEXT,
    trigger_type        TEXT,   -- UNIT_SWEET_SPOT | APD_HALO
    evidence_map        TEXT,   -- JSON: tract-level field -> evidence object (merged from accepted receipts)
    status              TEXT NOT NULL DEFAULT 'NEW',
    abstractor_status   TEXT NOT NULL DEFAULT 'NOT_STARTED',
    curative_cost_cents INTEGER NOT NULL DEFAULT 0,
    curative_cost_priced INTEGER NOT NULL DEFAULT 0,
    min_evidence_grade  TEXT,
    net_spread_per_nma  REAL,
    score               INTEGER,
    tier                TEXT,
    cyclicality_kill_switch INTEGER NOT NULL DEFAULT 0,
    lease_info          TEXT,   -- JSON: expiration pre-screen result
    deal_card           TEXT,   -- JSON: validated deal card
    exec_message_ts     TEXT,   -- Slack ts of the #exec-briefings card
    hold_reason         TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_orders (
    work_order_id   TEXT PRIMARY KEY,
    tract_id        TEXT NOT NULL,
    task_type       TEXT NOT NULL,
    assigned_to     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN, CLAIMED, COMPLETE, ACCEPTED, REVISION_REQUESTED, SUPERSEDED, FAILED
    payload         TEXT NOT NULL DEFAULT '{}',
    revision_count  INTEGER NOT NULL DEFAULT 0,
    parent_id       TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- At most one live work order per (tract, task) -- including one awaiting COS acceptance:
-- the database-level duplicate-scrape guard.
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_work_order
    ON work_orders (tract_id, task_type) WHERE status IN ('OPEN', 'CLAIMED', 'COMPLETE');

CREATE INDEX IF NOT EXISTS ix_work_orders_tract ON work_orders (tract_id);

CREATE TABLE IF NOT EXISTS tract_locks (
    tract_id    TEXT NOT NULL,
    scope       TEXT NOT NULL,       -- task_type being performed
    owner       TEXT NOT NULL,       -- agent_id@host
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    PRIMARY KEY (tract_id, scope)
);

CREATE TABLE IF NOT EXISTS runs_sheets (
    id                TEXT PRIMARY KEY,
    tract_id          TEXT NOT NULL,
    version           INTEGER NOT NULL,
    entries           TEXT NOT NULL,      -- JSON array (schemas/runs_sheet.schema.json)
    nma_math          TEXT NOT NULL,      -- JSON object
    abstractor_status TEXT NOT NULL,
    defects           TEXT NOT NULL DEFAULT '[]',
    field_tickets     TEXT NOT NULL DEFAULT '[]',
    min_evidence_grade TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS owners (
    owner_id        TEXT PRIMARY KEY,
    tract_id        TEXT NOT NULL,
    name            TEXT NOT NULL,
    role            TEXT NOT NULL,        -- UNLEASED_MINERAL, EXPIRED_LESSOR, HEIR, NPRI
    status          TEXT NOT NULL,        -- LOCATED, DECEASED, UNLOCATED
    deceased        INTEGER NOT NULL DEFAULT 0,
    probate_ref     TEXT,
    mailing_address TEXT,
    phone           TEXT,
    resolution_path TEXT,                 -- which waterfall step resolved it
    evidence        TEXT,                 -- JSON evidence object
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comps (
    comp_id        TEXT PRIMARY KEY,
    source_type    TEXT NOT NULL,         -- OK_CLO, NM_SLO, BLM, OCC_POOLING
    state          TEXT NOT NULL,
    county         TEXT NOT NULL,
    sale_date      TEXT NOT NULL,
    acres          REAL NOT NULL,
    price_per_acre REAL NOT NULL,
    royalty        REAL,
    grade          TEXT NOT NULL,
    evidence       TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS buyer_demand (
    buyer_id      TEXT PRIMARY KEY,
    buyer_name    TEXT NOT NULL,
    buyer_type    TEXT NOT NULL,          -- OPERATOR, BROKER, CAPITAL_PARTNER
    state         TEXT NOT NULL,
    county        TEXT NOT NULL,
    max_bonus_per_acre REAL,
    jv_split_pct  REAL,
    evidence      TEXT,                   -- JSON: LOI / standing-bid citation
    active        INTEGER NOT NULL DEFAULT 1,
    notes         TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tract_expenses (
    expense_id   TEXT PRIMARY KEY,
    tract_id     TEXT NOT NULL,
    category     TEXT NOT NULL,           -- COPIES, RECORDING_FEE, SKIP_TRACE, MILEAGE, ABSTRACTOR_FEE, POSTAGE, OTHER
    amount_cents INTEGER NOT NULL,
    description  TEXT NOT NULL,
    receipt_ref  TEXT,
    incurred_by  TEXT NOT NULL,
    incurred_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_expenses_tract ON tract_expenses (tract_id);

CREATE TABLE IF NOT EXISTS market_snapshots (
    snapshot_id           TEXT PRIMARY KEY,
    wti_usd               REAL,
    henry_hub_usd         REAL,
    permit_count_current  INTEGER,
    permit_count_baseline INTEGER,
    kill_switch           INTEGER NOT NULL,
    reasons               TEXT NOT NULL DEFAULT '[]',
    source                TEXT NOT NULL,
    created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id        TEXT PRIMARY KEY,
    ts        TEXT NOT NULL,
    actor     TEXT NOT NULL,
    event     TEXT NOT NULL,
    tract_id  TEXT,
    detail    TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS ix_audit_tract ON audit_log (tract_id);
