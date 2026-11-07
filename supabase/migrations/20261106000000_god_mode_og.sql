-- AMARA v12 / God Mode — Open Acreage Sync lane
-- Schema god_mode_og. Celilo rule: missing values are NULL + status, never guesses.
create extension if not exists postgis;
create schema if not exists god_mode_og;

-- Common provenance columns are repeated on every data table (Cite-or-omit).
-- source_name / source_url / source_record_id / retrieved_at are NOT NULL: no source = no record.

create table god_mode_og.sync_run (
  id uuid primary key default gen_random_uuid(),
  source text not null,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  status text not null default 'RUNNING' check (status in ('RUNNING','OK','PARTIAL','FAILED')),
  records_in int not null default 0,
  records_valid int not null default 0,
  records_rejected int not null default 0,
  source_updated_at timestamptz,
  error_log text,
  constitution_version text not null,
  source_state text,            -- OK / UNVERIFIED / NOT FOUND / UNREACHABLE / DISABLED
  notes jsonb not null default '[]'
);

-- Last-seen editingInfo.lastEditDate per layer: drives the daily 'changed?' check.
create table god_mode_og.layer_state (
  layer_url text primary key,
  last_edit_date timestamptz,
  updated_at timestamptz not null default now()
);

-- macro: data tables share this shape
create table god_mode_og.lease (
  id uuid primary key default gen_random_uuid(),
  source_name text not null,
  source_url text not null,
  source_layer text not null,
  source_record_id text not null,
  source_updated_at timestamptz,
  retrieved_at timestamptz not null,
  sync_run_id uuid references god_mode_og.sync_run(id),
  raw jsonb not null,
  geom geometry(MultiPolygon, 4326),
  lessor_type text not null check (lessor_type in ('UNIVERSITY','STATE','FEDERAL')),
  lease_no text,
  lessee text,
  status_raw text,
  status_normalized text not null default 'UNKNOWN'
    check (status_normalized in ('ACTIVE','INACTIVE','EXPIRED','UNKNOWN')),
  effective_date date,
  expiration_date date,
  acres numeric,
  county text,
  state text,
  unique (source_name, source_record_id),
  check (expiration_date is null or effective_date is null or expiration_date >= effective_date)
);
create index lease_geom_gix on god_mode_og.lease using gist (geom);

create table god_mode_og.available_parcel (
  id uuid primary key default gen_random_uuid(),
  source_name text not null, source_url text not null, source_layer text not null,
  source_record_id text not null, source_updated_at timestamptz, retrieved_at timestamptz not null,
  sync_run_id uuid references god_mode_og.sync_run(id), raw jsonb not null,
  geom geometry(MultiPolygon, 4326),
  county text, state text, acres numeric,
  unique (source_name, source_record_id)
);
create index available_parcel_geom_gix on god_mode_og.available_parcel using gist (geom);

create table god_mode_og.section_grid (
  id uuid primary key default gen_random_uuid(),
  source_name text not null, source_url text not null, source_layer text not null,
  source_record_id text not null, source_updated_at timestamptz, retrieved_at timestamptz not null,
  sync_run_id uuid references god_mode_og.sync_run(id), raw jsonb not null,
  geom geometry(MultiPolygon, 4326),
  county text, state text, label text,
  unique (source_name, source_record_id)
);
create index section_grid_geom_gix on god_mode_og.section_grid using gist (geom);

create table god_mode_og.well (
  id uuid primary key default gen_random_uuid(),
  source_name text not null, source_url text not null, source_layer text not null,
  source_record_id text not null, source_updated_at timestamptz, retrieved_at timestamptz not null,
  sync_run_id uuid references god_mode_og.sync_run(id), raw jsonb not null,
  geom geometry(Geometry, 4326),
  api_no text, operator text, status text,
  spud_date date, completion_date date, plug_date date, lease_name text, field text,
  county text, state text,
  unique (source_name, source_record_id)
);
create index well_geom_gix on god_mode_og.well using gist (geom);

create table god_mode_og.permit (
  id uuid primary key default gen_random_uuid(),
  source_name text not null, source_url text not null, source_layer text not null,
  source_record_id text not null, source_updated_at timestamptz, retrieved_at timestamptz not null,
  sync_run_id uuid references god_mode_og.sync_run(id), raw jsonb not null,
  geom geometry(Point, 4326),
  permit_no text, issued_date date, lease_name text, county text,
  permit_status text, universal_doc_no text, operator_name text, operator_no text, submit_date date, county_code text, nearest_town text, district text, flags text[],
  received_date date, amended_date date, extended_date date, spud_date date, permit_expires date, activity_date date, well_no text, datum text, coord_style text, locations jsonb,
  unique (source_name, source_record_id)
);
create index permit_geom_gix on god_mode_og.permit using gist (geom);

create table god_mode_og.production_monthly (
  id uuid primary key default gen_random_uuid(),
  source_name text not null, source_url text not null, source_layer text not null,
  source_record_id text not null, source_updated_at timestamptz, retrieved_at timestamptz not null,
  sync_run_id uuid references god_mode_og.sync_run(id), raw jsonb not null,
  geom geometry(Point, 4326),
  well_or_lease_id text, month date, oil_bbl numeric, gas_mcf numeric,
  cond_bbl numeric, csgd_mcf numeric, lease_key text, api8_list text[],
  unique (source_name, source_record_id)
);

create table god_mode_og.change_event (
  id uuid primary key default gen_random_uuid(),
  entity_type text not null,
  entity_id text not null,
  change_type text not null check (change_type in
    ('NEW','REMOVED','STATUS_CHANGE','EXPIRATION_CHANGE','GEOMETRY_CHANGE')),
  old_value jsonb,
  new_value jsonb,
  detected_at timestamptz not null default now(),
  sync_run_id uuid references god_mode_og.sync_run(id)
);

create table god_mode_og.open_acreage_candidate (
  id uuid primary key default gen_random_uuid(),
  tract_id text not null,
  county text,
  reason_codes text[] not null default '{}',
  evidence_refs jsonb not null default '[]',
  -- Constitution Control 9: pipeline can never write anything above CANDIDATE.
  status text not null check (status in ('CANDIDATE — TITLE CHECK REQUIRED','HOLD','DROP')),
  constitution_version text not null,
  created_at timestamptz not null default now()
);

create table god_mode_og.title_check (
  id uuid primary key default gen_random_uuid(),
  candidate_id uuid not null references god_mode_og.open_acreage_candidate(id),
  result text not null check (result in ('LEASE FOUND','NO LEASE FOUND','UNVERIFIED')),
  instrument_refs jsonb not null default '[]',
  checked_by text,
  checked_at timestamptz
);

-- History + rejected tables; archive-on-update trigger keeps prior versions (raw is never lost).
do $$
declare t text;
begin
  foreach t in array array['lease','available_parcel','section_grid','well','permit','production_monthly'] loop
    -- LIKE ... INCLUDING DEFAULTS copies columns only (no unique constraints), so many versions per record are allowed.
    execute format(
      'create table god_mode_og.%1$I_history (like god_mode_og.%1$I including defaults, archived_at timestamptz not null default now(), history_id bigserial primary key)', t);
    execute format(
      'create table god_mode_og.%1$I_rejected (id bigserial primary key, source_name text, source_url text, source_layer text, source_record_id text, retrieved_at timestamptz, sync_run_id uuid, raw jsonb, reasons text[] not null, rejected_at timestamptz not null default now())', t);
    execute format(
      'create function god_mode_og.%1$I_archive() returns trigger language plpgsql as $f$ begin insert into god_mode_og.%1$I_history select old.*; if tg_op = 'DELETE' then return old; end if; return new; end $f$', t);
    execute format(
      'create trigger %1$I_archive_trg before update or delete on god_mode_og.%1$I for each row execute function god_mode_og.%1$I_archive()', t);
  end loop;
end $$;
