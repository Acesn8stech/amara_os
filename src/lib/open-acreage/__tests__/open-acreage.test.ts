import test from 'node:test';
import assert from 'node:assert/strict';
import { DEFAULT_CONFIG, type OpenAcreageConfig } from '../config.ts';
import { CANDIDATE_STATUS, CONSTITUTION_VERSION } from '../constitution.ts';
import { validateBatch } from '../validate.ts';
import { buildCandidates, assertStatus, staleSources } from '../candidates.ts';
import { intersects } from '../geometry.ts';
import { diffRecords } from '../diff.ts';
import { decodePacked, decodeFile, decodeZoned } from '../fixedwidth.ts';
import { dateField, normalizeStatus } from '../normalize.ts';
import { BLM_SERVICE, pullUL, pullBLM, pullGLO, decodeRRC, UL_LAYERS } from '../connectors.ts';
import { runSource } from '../runner.ts';
import { MemoryStore } from '../store.ts';
import { answer, parseIntent } from '../voice.ts';
import { buildDigest } from '../digest.ts';
import { api8, combineRRC, pullPdq } from '../pdq.ts';
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

test('UL real field names map (lease, well, section); no expiration field stays NULL; Non-Producing is not ACTIVE', async () => {
  const mk = (fields: string[], props: Record<string, unknown>) => (async (u: string) => new Response(JSON.stringify(u.includes('?f=json')
    ? { maxRecordCount: 5, fields: [{ name: 'OBJECTID', type: 'esriFieldTypeOID' }, ...fields.map((name) => ({ name, type: 'esriFieldTypeString' }))] }
    : { features: [{ properties: { OBJECTID: 1, ...props }, geometry: null }] }))) as unknown as typeof fetch;
  const ctx = (f: typeof fetch) => ({ fetch: f, timeoutMs: 1000, retries: 0, now: NOW, priorEdit: {} });
  const lp = { UTLeaseNbr: '41298', MapLesseeName: 'ACME', LeaseStatus: 'Producing', DateBeginning: 1100000000000, County: 'Andrews', MineralAcres: 640 };
  const l = (await pullUL(ctx(mk(Object.keys(lp), lp)), ['OG_Lease'])).layers[0];
  const rec = l.records[0] as LeaseRecord;
  assert.deepEqual([rec.lease_no, rec.lessee, rec.status_normalized, rec.acres, rec.county], ['41298', 'ACME', 'ACTIVE', 640, 'Andrews']);
  assert.equal(rec.effective_date, '2004-11-09'); assert.equal(rec.expiration_date, null);
  assert.deepEqual(l.unmapped, ['expiration_date']);
  assert.equal(normalizeStatus('Non-Producing'), 'UNKNOWN');
  const want: Record<string, string> = { 'Held by Unit': 'ACTIVE', 'Non Producing': 'UNKNOWN', 'Pending': 'UNKNOWN', 'Primary Term Non-Producing': 'ACTIVE',
    'Producing': 'ACTIVE', 'Producing (PT)': 'ACTIVE', 'Shut-In Royalty': 'ACTIVE', 'Under Review': 'UNKNOWN' };
  for (const [k, v] of Object.entries(want)) assert.equal(normalizeStatus(k), v, k);
  const wp = { API: '4247536324', CompanyName: 'WPX', ZoneStatus: 'ACTIVE', Latitude: 31.5, Longitude: -103.5 };
  const w = (await pullUL(ctx(mk(Object.keys(wp), wp)), ['Wellbore'])).layers[0].records[0] as BaseRecord;
  const na = { API: '1', WellboreStatusCode: 'NA' };
  const wn = (await pullUL(ctx(mk(Object.keys(na), na)), ['WellSHL'])).layers[0].records[0] as BaseRecord;
  assert.equal(wn.status, null); assert.equal((wn.raw as Record<string, unknown>).WellboreStatusCode, 'NA');
  assert.equal(w.operator, 'WPX'); assert.deepEqual(w.geom, { type: 'Point', coordinates: [-103.5, 31.5] });
  const sp = { blocknum: '41', sectionnum: '7', county: 'Ward' };
  const s = (await pullUL(ctx(mk(Object.keys(sp), sp)), ['UL_Section'])).layers[0].records[0] as BaseRecord;
  assert.equal(s.label, 'Block 41 Section 7');
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

test('UNKNOWN-status overlapping lease forces HOLD (not a clean candidate); expired-recent flagged', () => {
  const out = buildCandidates(inputs({ leases: [lease('U'), lease('E', { status_normalized: 'EXPIRED', expiration_date: '2026-03-01' })] }), cfg(), NOW);
  assert.equal(out[0].status, 'HOLD');
  assert.equal(buildCandidates(inputs({ leases: [lease('E', { status_normalized: 'EXPIRED', expiration_date: '2026-03-01' })] }), cfg(), NOW)[0].status, CANDIDATE_STATUS);
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
    if (u === `${BLM_SERVICE}?f=json`) return j({ serviceDescription: 'x' });
    if (u.includes('/layers?f=json')) return j({ layers: [{ id: 3, name: 'Federal O&G' }] });
    if (u.includes('/3?f=json')) return j({ maxRecordCount: 5, fields: [{ name: 'OBJECTID', type: 'esriFieldTypeOID' }, { name: 'ADMIN_ST', type: 'esriFieldTypeString' }] });
    if (u.includes('returnDistinctValues')) return j({ features: [{ attributes: { ADMIN_ST: 'NM' } }, { attributes: { ADMIN_ST: 'CO' } }] });
    return j({ features: [{ properties: { OBJECTID: 1, ADMIN_ST: 'NM' }, geometry: null }] });
  }) as unknown as typeof fetch;
  const r = await pullBLM({ fetch: f, timeoutMs: 1000, retries: 0, now: NOW, priorEdit: {} });
  assert.match(r.notes.join('\n'), /no layers array; keys=\[serviceDescription\]/);
  assert.equal(r.layers[0].records.length, 1); assert.match(r.notes.join('\n'), /ADMIN_ST IN \('NM'\)/);
});

test('BLM Case_Disp: pulls all disposition layers, filters on GEO_STATE (not ADMIN_STATE), maps CSE_* fields, prefixes ids by layer', async () => {
  const fields = ['ADMIN_STATE', 'GEO_STATE', 'CSE_DISP', 'CSE_NR', 'LEG_CSE_NR', 'EFF_DT', 'EXP_DT', 'RCRD_ACRS'];
  const wheres: string[] = [];
  const f = (async (u: string) => {
    const j = (o: unknown) => new Response(JSON.stringify(o));
    if (u === `${BLM_SERVICE}?f=json`) return j({ layers: [{ id: 0, name: 'Authorized' }, { id: 3, name: 'Closed' }] });
    if (/\/\d\?f=json$/.test(u)) return j({ maxRecordCount: 2000, fields: [{ name: 'OBJECTID', type: 'esriFieldTypeOID' }, ...fields.map((name) => ({ name, type: 'esriFieldTypeString' }))] });
    if (u.includes('returnDistinctValues')) return j({ features: ['NM', 'TX', 'OK'].map((v) => ({ attributes: { GEO_STATE: v } })) });
    wheres.push(decodeURIComponent(new URL(u).searchParams.get('where')!));
    return j({ features: [{ properties: { OBJECTID: 5, ADMIN_STATE: 'NM', GEO_STATE: 'TX', CSE_DISP: u.includes('/3/') ? 'Closed' : 'Authorized', CSE_NR: 'X1', LEG_CSE_NR: 'NMNM   075445', EFF_DT: 815356740000, EXP_DT: 1900000000000, RCRD_ACRS: 640 }, geometry: null }] });
  }) as unknown as typeof fetch;
  const r = await pullBLM({ fetch: f, timeoutMs: 1000, retries: 0, now: NOW, priorEdit: {} });
  assert.equal(r.layers.length, 2); assert.ok(wheres.every((w) => w === "GEO_STATE IN ('NM','TX')"), wheres.join());
  const a = r.layers[0].records[0] as LeaseRecord; const c = r.layers[1].records[0] as LeaseRecord;
  assert.deepEqual([a.status_normalized, c.status_normalized], ['ACTIVE', 'INACTIVE']);
  assert.deepEqual([a.lease_no, a.state, a.acres, a.effective_date, a.expiration_date, a.county, a.lessee], ['NMNM   075445', 'TX', 640, '1995-11-02', '2030-03-17', null, null]);
  assert.deepEqual([a.source_record_id, c.source_record_id], ['0:5', '3:5']);
});

test('first load of a layer emits no change events; later changes do; digest uses latest run per source', async () => {
  const store = new MemoryStore();
  const pull = (status: LeaseRecord['status_normalized']) => async () => ({ source: 'UL' as const, state: 'OK' as const, notes: [],
    layers: [{ layer: 'OG_Lease', url: 'u', entity_type: 'lease' as const, skipped_unchanged: false, source_updated_at: null, records: [lease('A', { status_normalized: status })], unmapped: [], fields_discovered: [] }] });
  const failed = async () => { throw new Error('boom'); };
  await runSource('UL', failed, store, cfg(), NOW);
  await runSource('UL', pull('ACTIVE'), store, cfg(), NOW);
  assert.equal((await store.getChanges()).length, 0, 'baseline load must not emit NEW events');
  await runSource('UL', pull('EXPIRED'), store, cfg(), new Date().toISOString());
  const ch = await store.getChanges();
  assert.deepEqual(ch.map((c) => c.change_type), ['STATUS_CHANGE']);
  const digest = await buildDigest(store, cfg(), new Date().toISOString());
  assert.match(digest, /newly expired\/inactive \(24h\): 1/); assert.match(digest, /latest run\): none/);
});

test('geometry: identical, contained, partially-overlapping and disjoint polygons', () => {
  const a = sq(-104, 31); // 0.1 square
  assert.equal(intersects(a, sq(-104, 31)), true, 'identical (whole-section lease over its section)');
  assert.equal(intersects(a, sq(-103.98, 31.02, 0.05)), true, 'contained');
  assert.equal(intersects(a, sq(-103.95, 31.05)), true, 'partial overlap');
  assert.equal(intersects(sq(-104, 31, 0.1), sq(-104, 31, 0.1)), true);
  assert.equal(intersects(a, sq(-103, 31)), false, 'disjoint');
  assert.equal(buildCandidates(inputs({ leases: [lease('Same', { status_normalized: 'ACTIVE', geom: sq(-104, 31) })] }), cfg(), NOW).length, 0, 'same-shape active lease excludes the section');
});

test('voice: a target county with no section data answers NOT FOUND, not zero', () => {
  const txt = answer({ kind: 'candidate_count', county: 'Midland' }, { leases: [], candidates: [], changes: [], runs: freshRuns(), countyOfEntity: () => null, sectionCounts: { reeves: 5 } }, cfg({ targetCounties: ['Midland'] }), NOW);
  assert.match(txt, /NOT FOUND/); assert.doesNotMatch(txt, /^0 /);
});

test('RRC is a good sync only with permits AND production; permits alone is UNVERIFIED', () => {
  const mkLayout = (id: string, fields: Array<{ name: string; start: number; length: number; type: 'char' }>) => ({ id, recordLength: 10, encoding: 'ascii' as const, verified: true, fields });
  const layouts = {
    permit: mkLayout('p', [{ name: 'permit_no', start: 0, length: 5, type: 'char' }]),
    production_oil: mkLayout('o', [{ name: 'well_or_lease_id', start: 0, length: 5, type: 'char' }, { name: 'month', start: 5, length: 5, type: 'char' }]),
    production_gas: mkLayout('g', [{ name: 'well_or_lease_id', start: 0, length: 5, type: 'char' }]),
  };
  const bytes = new TextEncoder().encode('12345abcde');
  const f = (kind: 'permit' | 'production_oil' | 'production_gas') => ({ kind, url: 'u', bytes, source_updated_at: null });
  const partial = decodeRRC([f('permit')], { now: NOW }, layouts as never);
  assert.equal(partial.state, 'UNVERIFIED'); assert.match(partial.notes.join(), /missing required datasets: production_oil, production_gas/);
  assert.equal(decodeRRC([f('permit'), f('production_oil'), f('production_gas')], { now: NOW }, layouts as never).state, 'OK');
});

test('PDQ: reads by column name, joins lease->API->our wells, windows by month, refuses a bad header, UL 10-digit API matches', async () => {
  assert.equal(api8('4247536324'), '47536324'); assert.equal(api8('47536324'), '47536324'); assert.equal(api8('abc'), null); assert.equal(api8('123'), null);
  const wc = ['OIL_GAS_CODE}DISTRICT_NO}LEASE_NO}WELL_NO}API_COUNTY_CODE}API_UNIQUE_NO}COUNTY_NAME',
    'O}08}012345}1}475}36324}WARD', 'O}08}999999}1}003}00001}ANDREWS', 'O}08}012345}2}475}99999}WARD'];
  const lc = ['LEASE_NO}OIL_GAS_CODE}DISTRICT_NO}CYCLE_YEAR_MONTH}LEASE_OIL_PROD_VOL}LEASE_GAS_PROD_VOL}LEASE_COND_PROD_VOL}LEASE_CSGD_PROD_VOL}EXTRA',
    '012345}O}08}202606}150}}}0}x', '012345}O}08}201001}999}}}0}x', '999999}O}08}202606}500}}}0}x', '012345}O}08}202607}0}0}0}0}x'];
  const o = { wantedApi8: new Set(['47536324']), cutoffYm: '202501', now: NOW, sourceUrl: 'https://rrc/pdq', sourceUpdatedAt: null };
  const r = await pullPdq({ wellCompletion: wc, leaseCycle: lc }, o);
  const recs = r.layers[0].records as BaseRecord[];
  assert.equal(recs.length, 2, 'old month and unrelated lease excluded');
  assert.deepEqual(recs.map((x) => [x.well_or_lease_id, x.month, x.oil_bbl]), [['47536324', '2026-06-01', 150], ['47536324', '2026-07-01', 0]]);
  assert.equal(recs[0].gas_mcf, null, 'blank volume stays NULL, not 0');
  await assert.rejects(pullPdq({ wellCompletion: ['OIL_GAS_CODE}LEASE_NO', 'O}1'], leaseCycle: lc }, o), /missing required columns/);
  const well = { entity_type: 'well', source_name: 'UL', source_url: 'u', source_layer: 'WellSHL', source_record_id: 'W', source_updated_at: null, retrieved_at: NOW, raw: {},
    geom: { type: 'Point', coordinates: [-103.95, 31.05] }, api_no: '4247536324', status: null, county: 'Reeves' } as BaseRecord;
  const prod = { ...recs[0], month: '2026-06-01' } as BaseRecord;
  assert.equal(buildCandidates(inputs({ wells: [well], production: [prod] }), cfg(), NOW).length, 0);
  assert.equal(buildCandidates(inputs({ wells: [well], production: [{ ...prod, oil_bbl: 0, gas_mcf: 0, cond_bbl: 0, csgd_mcf: 0 } as BaseRecord] }), cfg(), NOW).length, 1);
});

test('combineRRC: permits alone or production alone is UNVERIFIED; both is OK', () => {
  const part = (layer: string, state: 'OK' | 'UNVERIFIED' = 'OK') => ({ source: 'RRC' as const, state, notes: [], layers: [{ layer, url: 'u', entity_type: 'permit' as const, skipped_unchanged: false, source_updated_at: null, records: [], unmapped: [], fields_discovered: [] }] });
  assert.equal(combineRRC([part('production')]).state, 'UNVERIFIED');
  assert.equal(combineRRC([part('permit')]).state, 'UNVERIFIED');
  assert.equal(combineRRC([part('permit'), part('production')]).state, 'OK');
  assert.equal(combineRRC([]).state, 'NOT FOUND');
});
