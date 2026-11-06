import test from 'node:test';
import assert from 'node:assert/strict';
import { DEFAULT_CONFIG, type OpenAcreageConfig } from '../config.ts';
import { CANDIDATE_STATUS, CONSTITUTION_VERSION } from '../constitution.ts';
import { validateBatch } from '../validate.ts';
import { buildCandidates, assertStatus, staleSources } from '../candidates.ts';
import { diffRecords } from '../diff.ts';
import { decodePacked, decodeFile, decodeZoned } from '../fixedwidth.ts';
import { dateField, normalizeStatus } from '../normalize.ts';
import { pullUL, pullBLM, pullGLO, decodeRRC, UL_LAYERS } from '../connectors.ts';
import { runSource } from '../runner.ts';
import { MemoryStore } from '../store.ts';
import { answer, parseIntent } from '../voice.ts';
import type { BaseRecord, Geometry, LeaseRecord, Section, SyncRun } from '../types.ts';

const NOW = '2026-11-06T12:00:00.000Z';
const sq = (x: number, y: number, s = 0.1): Geometry => ({ type: 'MultiPolygon', coordinates: [[[[x, y], [x + s, y], [x + s, y + s], [x, y + s], [x, y]]]] });
const cfg = (o: Partial<OpenAcreageConfig> = {}): OpenAcreageConfig => ({ ...DEFAULT_CONFIG, targetCounties: ['Reeves'], ...o });

const lease = (id: string, over: Partial<LeaseRecord> = {}): LeaseRecord => ({
  entity_type: 'lease', source_name: 'UL', source_url: 'https://x/OG_Lease', source_layer: 'OG_Lease', source_record_id: id, source_updated_at: '2026-11-01T00:00:00Z',
  retrieved_at: NOW, raw: { id }, geom: sq(-104, 31), lessor_type: 'UNIVERSITY', lease_no: null, lessee: null, status_raw: null, status_normalized: 'UNKNOWN',
  effective_date: null, expiration_date: null, acres: null, county: 'Reeves', state: 'TX', ...over });
const section = (id: string, g = sq(-104, 31)): Section => ({ entity_type: 'section_grid', source_name: 'UL', source_url: 'https://x/sec', source_layer: 'UL_Section', source_record_id: id,
  source_updated_at: null, retrieved_at: NOW, raw: {}, geom: g, county: 'Reeves', label: null });
const freshRuns = (): SyncRun[] => (['UL', 'BLM', 'RRC'] as const).map((s) => ({ run_id: s, source: s, started_at: NOW, finished_at: '2026-11-06T01:00:00.000Z', status: 'OK', records_in: 1, records_valid: 1,
  records_rejected: 0, source_updated_at: null, error_log: null, constitution_version: CONSTITUTION_VERSION, source_state: 'OK', notes: [] }));
const inputs = (over = {}) => ({ sections: [section('S1')], leases: [], permits: [], wells: [], production: [], parcels: [], runs: freshRuns(), ...over });

// ---- required acceptance tests (§8.4)
test('Celilo: missing field stays NULL and status is UNKNOWN (never inferred)', () => {
  assert.equal(dateField({}, ['EXPIRATION_DATE']), null);
  assert.equal(dateField({ EXPIRATION_DATE: 'garbage' }, ['EXPIRATION_DATE']), null);
  assert.equal(normalizeStatus(null), 'UNKNOWN');
  assert.equal(normalizeStatus('something odd'), 'UNKNOWN');
  assert.equal(normalizeStatus('Inactive'), 'INACTIVE'); // "inactive" must not match "active"
});

test('Celilo: connector maps missing fields to NULL, reports them as unmapped, expiration never invented', async () => {
  const layer = (url: string) => (url.includes('?f=json')
    ? { name: 'L', maxRecordCount: 2, fields: [{ name: 'OBJECTID', type: 'esriFieldTypeOID' }, { name: 'WEIRD', type: 'esriFieldTypeString' }], editingInfo: { lastEditDate: 1762000000000 } }
    : { features: [{ properties: { OBJECTID: 7, WEIRD: 'x' }, geometry: { type: 'Polygon', coordinates: [[[-104, 31], [-103.9, 31], [-103.9, 31.1], [-104, 31]]] } }] });
  const f = (async (u: string) => new Response(JSON.stringify(layer(u)))) as unknown as typeof fetch;
  const r = await pullUL({ fetch: f, timeoutMs: 1000, retries: 0, now: NOW, priorEdit: {} }, ['OG_Lease']);
  const rec = r.layers[0].records[0] as LeaseRecord;
  assert.equal(rec.expiration_date, null); assert.equal(rec.status_normalized, 'UNKNOWN'); assert.equal(rec.lessee, null);
  assert.equal(rec.source_record_id, '7');
  assert.ok(r.layers[0].unmapped.includes('expiration_date'));
});

test('record without source_url is rejected (and kept with reason)', () => {
  const bad = lease('A', { source_url: null as unknown as string });
  const g = validateBatch([bad, lease('B')], null, cfg());
  assert.equal(g.valid.length, 1); assert.equal(g.rejected.length, 1);
  assert.match(g.rejected[0].reasons.join(), /source_url/);
});

test('>20% count drop halts promote and marks run FAILED', async () => {
  const store = new MemoryStore();
  const mk = (n: number) => Array.from({ length: n }, (_, i) => lease(`L${i}`, { status_normalized: 'ACTIVE' }));
  const pull = (n: number) => async () => ({ source: 'UL' as const, state: 'OK' as const, notes: [],
    layers: [{ layer: 'OG_Lease', url: 'u', entity_type: 'lease' as const, skipped_unchanged: false, source_updated_at: null, records: mk(n), unmapped: [], fields_discovered: [] }] });
  const first = await runSource('UL', pull(10), store, cfg(), NOW);
  assert.equal(first.status, 'OK');
  const second = await runSource('UL', pull(7), store, cfg(), NOW); // 30% drop
  assert.equal(second.status, 'FAILED'); assert.match(second.error_log!, /dropped 30\.0%/);
  assert.equal((await store.getPromoted('UL', 'OG_Lease')).length, 10, 'prior set must remain promoted');
  const third = await runSource('UL', pull(9), store, cfg(), NOW); // 10% drop passes
  assert.equal(third.status, 'OK');
});

test('candidate never gets status above CANDIDATE — TITLE CHECK REQUIRED', () => {
  const out = buildCandidates(inputs(), cfg(), NOW);
  assert.equal(out.length, 1); assert.equal(out[0].status, CANDIDATE_STATUS);
  assert.throws(() => assertStatus('OPEN / LEASABLE'));
  assert.throws(() => assertStatus('OPEN'));
});

test('stale source forces HOLD with STALE_SOURCE', () => {
  const runs = freshRuns(); runs[0].finished_at = '2026-09-01T00:00:00.000Z'; // UL cadence 31d => stale after 62d
  assert.deepEqual(staleSources(runs, cfg(), NOW), ['UL']);
  const out = buildCandidates(inputs({ runs }), cfg(), NOW);
  assert.equal(out[0].status, 'HOLD'); assert.ok(out[0].reason_codes.includes('STALE_SOURCE'));
  const none = buildCandidates(inputs({ runs: [] }), cfg(), NOW);
  assert.equal(none[0].status, 'HOLD', 'no good sync ever => stale');
});

// ---- candidate logic
test('active lease, recent permit, or recent production each exclude a tract; empty county list yields none', () => {
  assert.equal(buildCandidates(inputs({ leases: [lease('L', { status_normalized: 'ACTIVE' })] }), cfg(), NOW).length, 0);
  const permit = { entity_type: 'permit', source_name: 'RRC', source_url: 'u', source_layer: 'permit', source_record_id: 'P', source_updated_at: null, retrieved_at: NOW, raw: {},
    geom: { type: 'Point', coordinates: [-103.95, 31.05] }, permit_no: 'P', issued_date: '2025-06-01', county: 'Reeves' } as BaseRecord;
  assert.equal(buildCandidates(inputs({ permits: [permit] }), cfg(), NOW).length, 0);
  assert.equal(buildCandidates(inputs({ permits: [{ ...permit, issued_date: '2020-01-01' }] }), cfg(), NOW).length, 1);
  const well = { entity_type: 'well', source_name: 'UL', source_url: 'u', source_layer: 'WellSHL', source_record_id: 'W', source_updated_at: null, retrieved_at: NOW, raw: {},
    geom: { type: 'Point', coordinates: [-103.95, 31.05] }, api_no: '42-1', status: null, county: 'Reeves' } as BaseRecord;
  const prod = { entity_type: 'production_monthly', source_name: 'RRC', source_url: 'u', source_layer: 'oil', source_record_id: 'x', source_updated_at: null, retrieved_at: NOW, raw: {}, geom: null,
    well_or_lease_id: '42-1', month: '2026-06-01', oil_bbl: 5, gas_mcf: null } as BaseRecord;
  assert.equal(buildCandidates(inputs({ wells: [well], production: [prod] }), cfg(), NOW).length, 0);
  assert.equal(buildCandidates(inputs(), cfg({ targetCounties: [] }), NOW).length, 0);
  assert.equal(buildCandidates(inputs(), cfg({ targetCounties: ['Howard'] }), NOW).length, 0);
});

test('UNKNOWN-status overlapping lease does not block but is flagged; expired-recent flagged', () => {
  const out = buildCandidates(inputs({ leases: [lease('U'), lease('E', { status_normalized: 'EXPIRED', expiration_date: '2026-03-01' })] }), cfg(), NOW);
  assert.ok(out[0].reason_codes.includes('LEASE_STATUS_UNKNOWN_OVERLAP')); assert.ok(out[0].reason_codes.includes('LEASE_EXPIRED_RECENT'));
  assert.ok(out[0].reason_codes.includes('GLO_NOT_CONNECTED'));
});

// ---- gate
test('gate: invalid geometry, out-of-bbox, bad date order rejected', () => {
  const g = validateBatch([
    lease('ok'), lease('far', { geom: sq(10, 10) }), lease('open', { geom: { type: 'MultiPolygon', coordinates: [[[[-104, 31], [-103.9, 31], [-103.9, 31.1]]]] } }),
    lease('dates', { effective_date: '2024-01-01', expiration_date: '2023-01-01' }), lease('nogeom', { geom: null })], null, cfg());
  assert.equal(g.valid.length, 1); assert.equal(g.rejected.length, 4);
});

// ---- diff
test('diff emits NEW / REMOVED / STATUS / EXPIRATION / GEOMETRY', () => {
  const prior = [lease('1', { status_normalized: 'ACTIVE', expiration_date: '2027-01-01' }), lease('2'), lease('3')];
  const next = [lease('1', { status_normalized: 'EXPIRED', expiration_date: '2026-01-01' }), lease('2', { geom: sq(-104, 31, 0.2) }), lease('4')];
  const types = diffRecords(prior, next, 'r', NOW).map((c) => `${c.entity_id}:${c.change_type}`).sort();
  assert.deepEqual(types, ['UL:1:EXPIRATION_CHANGE', 'UL:1:STATUS_CHANGE', 'UL:2:GEOMETRY_CHANGE', 'UL:3:REMOVED', 'UL:4:NEW']);
});

// ---- connectors / sources
test('UL: unchanged lastEditDate skips the full re-pull', async () => {
  let queried = false;
  const f = (async (u: string) => { if (u.includes('/query')) queried = true; return new Response(JSON.stringify({ maxRecordCount: 10, fields: [], editingInfo: { lastEditDate: 1762000000000 } })); }) as unknown as typeof fetch;
  const url = UL_LAYERS[1].path;
  const r = await pullUL({ fetch: f, timeoutMs: 1000, retries: 0, now: NOW, priorEdit: { [url]: new Date(1762000000000).toISOString() } }, ['OG_Lease']);
  assert.equal(r.layers[0].skipped_unchanged, true); assert.equal(queried, false);
});

test('UL: pagination honors maxRecordCount; 403 policy denial is not retried and source is UNREACHABLE', async () => {
  const pages: string[] = [];
  const f = (async (u: string) => {
    if (u.includes('?f=json')) return new Response(JSON.stringify({ maxRecordCount: 2, fields: [{ name: 'OBJECTID', type: 'esriFieldTypeOID' }] }));
    pages.push(new URL(u).searchParams.get('resultOffset')!);
    const off = Number(new URL(u).searchParams.get('resultOffset'));
    const n = off === 4 ? 1 : 2;
    return new Response(JSON.stringify({ features: Array.from({ length: n }, (_, i) => ({ properties: { OBJECTID: off + i }, geometry: null })) }));
  }) as unknown as typeof fetch;
  const r = await pullUL({ fetch: f, timeoutMs: 1000, retries: 0, now: NOW, priorEdit: {} }, ['OG_Lease']);
  assert.deepEqual(pages, ['0', '2', '4']); assert.equal(r.layers[0].records.length, 5);

  let calls = 0;
  const denied = (async () => { calls++; return new Response('no', { status: 403 }); }) as unknown as typeof fetch;
  const d = await pullUL({ fetch: denied, timeoutMs: 1000, retries: 3, now: NOW, priorEdit: {}, sleep: async () => {} }, ['OG_Lease']);
  assert.equal(d.state, 'UNREACHABLE'); assert.equal(calls, 1);
});

test('GLO stays NOT FOUND stub; runSource marks PARTIAL and does not throw', async () => {
  const run = await runSource('GLO', pullGLO, new MemoryStore(), cfg(), NOW);
  assert.equal(run.source_state, 'NOT FOUND'); assert.equal(run.status, 'PARTIAL');
});

test('kill switch disables a source without running it', async () => {
  let ran = false;
  const c = cfg({ sources: { ...DEFAULT_CONFIG.sources, BLM: { ...DEFAULT_CONFIG.sources.BLM, enabled: false } } });
  const run = await runSource('BLM', async () => { ran = true; throw new Error('x'); }, new MemoryStore(), c, NOW);
  assert.equal(ran, false); assert.equal(run.source_state, 'DISABLED');
});

// ---- RRC decode
test('fixed-width: packed, zoned, EBCDIC char decode; unverified layout refuses to run', () => {
  assert.equal(decodePacked(Uint8Array.from([0x12, 0x34, 0x5c]), 2), 123.45);
  assert.equal(decodePacked(Uint8Array.from([0x12, 0x3d])), -123);
  assert.equal(decodeZoned(Uint8Array.from([0xf1, 0xf2, 0xc3]), "ebcdic"), 123);
  const layout = { id: 't', recordLength: 12, encoding: 'ebcdic' as const, verified: true, fields: [
    { name: 'well_or_lease_id', start: 0, length: 4, type: 'char' as const }, { name: 'month', start: 4, length: 6, type: 'char' as const }, { name: 'oil_bbl', start: 10, length: 2, type: 'packed' as const }] };
  const rec = Uint8Array.from([0xf0, 0xf0, 0xf4, 0xf2, 0xf2, 0xf0, 0xf2, 0xf6, 0xf0, 0xf6, 0x12, 0x3c]);
  assert.deepEqual(decodeFile(rec, layout).rows[0], { well_or_lease_id: '0042', month: '202606', oil_bbl: 123 });
  assert.throws(() => decodeFile(rec, { ...layout, verified: false }), /UNVERIFIED/);
  const r = decodeRRC([{ kind: 'production_oil', url: 'u', bytes: rec, source_updated_at: null }], { now: NOW });
  assert.equal(r.state, 'UNVERIFIED', 'shipped layouts are unverified'); // real RRC_LAYOUTS refuse
  const ok = decodeRRC([{ kind: 'production_oil', url: 'u', bytes: rec, source_updated_at: null }], { now: NOW }, { production_oil: layout } as never);
  assert.equal((ok.layers[0].records[0] as BaseRecord).month, '2026-06-01');
});

// ---- voice
test('voice: intents parse; answers cite sources + constitution and never say "open"', () => {
  assert.deepEqual(parseIntent("What's new in Reeves County?"), { kind: 'whats_new', county: 'Reeves' });
  assert.deepEqual(parseIntent('Show me expiring leases in Loving County'), { kind: 'expiring_leases', county: 'Loving' });
  assert.deepEqual(parseIntent('How many open acreage candidates in Howard County?'), { kind: 'candidate_count', county: 'Howard' });
  const cands = buildCandidates(inputs(), cfg(), NOW);
  const txt = answer({ kind: 'candidate_count', county: 'Reeves' }, { leases: [], candidates: cands, changes: [], runs: freshRuns(), countyOfEntity: () => null }, cfg(), NOW);
  assert.match(txt, /title check/i); assert.match(txt, new RegExp(CONSTITUTION_VERSION)); assert.match(txt, /last good sync/);
  assert.doesNotMatch(txt, /is open|are open|leasable|OPEN \//);
});

test('BLM: root without layers falls back to /layers, reports keys, picks a single layer without guessing names', async () => {
  const f = (async (u: string) => {
    const j = (o: unknown) => new Response(JSON.stringify(o));
    if (u.endsWith('/FeatureServer?f=json')) return j({ serviceDescription: 'x' });
    if (u.includes('/layers?f=json')) return j({ layers: [{ id: 3, name: 'Federal O&G' }] });
    if (u.includes('/3?f=json')) return j({ maxRecordCount: 5, fields: [{ name: 'OBJECTID', type: 'esriFieldTypeOID' }, { name: 'ADMIN_ST', type: 'esriFieldTypeString' }] });
    if (u.includes('returnDistinctValues')) return j({ features: [{ attributes: { ADMIN_ST: 'NM' } }, { attributes: { ADMIN_ST: 'CO' } }] });
    return j({ features: [{ properties: { OBJECTID: 1, ADMIN_ST: 'NM' }, geometry: null }] });
  }) as unknown as typeof fetch;
  const r = await pullBLM({ fetch: f, timeoutMs: 1000, retries: 0, now: NOW, priorEdit: {} });
  assert.match(r.notes.join('\n'), /no layers array; keys=\[serviceDescription\]/);
  assert.equal(r.layers[0].records.length, 1); assert.match(r.notes.join('\n'), /ADMIN_ST IN \('NM'\)/);
});
