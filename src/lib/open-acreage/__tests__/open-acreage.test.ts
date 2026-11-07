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
import { FileStore, MemoryStore } from '../store.ts';
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { answer, parseIntent } from '../voice.ts';
import { buildDigest } from '../digest.ts';
import { listPendingRuns, parseRrcDsv, pullPending, wellboreCoord } from '../pending.ts';
import { parseDate } from '../normalize.ts';
import { masterFileAsOf, masterFileMonth, monthsBack, parseMasterText, pullPermitMaster } from '../permitmaster.ts';
import { pointNearGeometry } from '../geometry.ts';
import { disambiguateIds } from '../connectors.ts';
import { api8, combineRRC, parseZipMemberDate, pullPdq } from '../pdq.ts';
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
  const all = r.layers[0].records as BaseRecord[];
  const recs = all.filter((x) => x.month !== null);
  assert.equal(all.length - recs.length, 1, 'one lease-to-well link record (month NULL)');
  assert.equal(recs.length, 1, 'old month, unrelated lease and the all-zero month are not stored');
  assert.deepEqual(recs.map((x) => [x.well_or_lease_id, x.month, x.oil_bbl, x.api8_list]), [['O:08:012345', '2026-06-01', 150, ['47536324']]]);
  assert.match(r.notes.join(), /1 zero\/blank rows/);
  assert.match(r.notes.join(), /1 RRC leases link to 1 of 1 wanted wells \(0 wanted wells have no RRC lease link\)/);
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

test('PDQ: export date is read from the zip listing (macOS and Linux formats)', () => {
  assert.equal(parseZipMemberDate('12915190541  10-26-2026 16:19   OG_LEASE_CYCLE_DATA_TABLE.dsv\n', 'OG_LEASE_CYCLE_DATA_TABLE.dsv'), '2026-10-26T16:19:00.000Z');
  assert.equal(parseZipMemberDate('12915190541  2026-10-26 16:19   OG_LEASE_CYCLE_DATA_TABLE.dsv\n', 'OG_LEASE_CYCLE_DATA_TABLE.dsv'), '2026-10-26T16:19:00.000Z');
  assert.equal(parseZipMemberDate('1  10-26-2026 16:19   OTHER.dsv\n', 'OG_LEASE_CYCLE_DATA_TABLE.dsv'), null);
});

test('FileStore: shards state per layer, round-trips, and migrates a legacy state.json', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'oa-'));
  const legacy = join(dir, 'state.json');
  writeFileSync(legacy, JSON.stringify({ promoted: { 'UL|OG_Lease': [lease('A')] }, history: [], rejected: [], runs: [], changes: [], candidates: [], layerEdit: { u: 'x' } }));
  const a = new FileStore(legacy);
  assert.equal((await a.getPromoted('UL', 'OG_Lease')).length, 1);
  assert.equal(existsSync(legacy), false, 'legacy file renamed'); assert.equal(existsSync(`${legacy}.migrated`), true);
  await a.promoteLayer('UL', 'WellSHL', 'well', [lease('B'), lease('C')], 'r');
  await a.saveRun({ run_id: 'r', source: 'UL', started_at: NOW, finished_at: NOW, status: 'OK', records_in: 3, records_valid: 3, records_rejected: 0, source_updated_at: null, error_log: null, constitution_version: CONSTITUTION_VERSION, source_state: 'OK', notes: [] });
  const b = new FileStore(legacy); // reload purely from shards
  assert.equal((await b.getPromoted('UL', 'WellSHL')).length, 2); assert.equal((await b.getPromoted('UL', 'OG_Lease')).length, 1);
  assert.equal((await b.getRuns()).length, 1); assert.equal(await b.getLayerEdit('u'), 'x');
  assert.ok(readFileSync(join(dir, 'state.d', 'meta.json'), 'utf8').length < 5000, 'meta stays small');
});

test('candidates: a well with no RRC link holds the tract unless it is plugged; linked-but-idle wells are fine', () => {
  const w = (id: string, api: string | null, status: string | null, layer = 'Wellbore') => ({ entity_type: 'well', source_name: 'UL', source_url: 'u', source_layer: layer, source_record_id: id,
    source_updated_at: null, retrieved_at: NOW, raw: {}, geom: { type: 'Point', coordinates: [-103.95, 31.05] }, api_no: api, status, county: 'Reeves' }) as BaseRecord;
  const link = { entity_type: 'production_monthly', source_name: 'RRC', source_url: 'u', source_layer: 'production', source_record_id: 'k:LINK', source_updated_at: null, retrieved_at: NOW, raw: {},
    geom: null, well_or_lease_id: 'O:08:1', api8_list: ['47536324'], month: null, oil_bbl: null, gas_mcf: null } as BaseRecord;
  const run = (wells: BaseRecord[], production: BaseRecord[] = [link]) => buildCandidates(inputs({ wells, production }), cfg(), NOW)[0];
  assert.equal(run([w('1', '4247536324', 'PR')]).status, CANDIDATE_STATUS, 'linked to a lease but no production in window: verifiable');
  const bad = run([w('2', '4200000001', 'PR')]); assert.equal(bad.status, 'HOLD'); assert.ok(bad.reason_codes.includes('WELL_NO_PRODUCTION_LINK'));
  assert.equal(run([w('3', '4200000002', 'PA')]).status, CANDIDATE_STATUS, 'unlinked but plugged');
  assert.equal(run([w('4', '4200000003', null, 'WellSHL'), w('5', '4200000003', 'PA')]).status, CANDIDATE_STATUS, 'status taken across both UL layers for the same API');
  assert.equal(run([w('6', '4200000004', null, 'WellSHL')]).status, 'HOLD', 'unlinked with no status at all');
  assert.equal(run([w('7', null, 'PA')]).status, 'HOLD', 'no usable API');
});

test('sections key on GlobalID when present; runs report same-id attribute changes', async () => {
  const mk = (props: Record<string, unknown>) => (async (u: string) => new Response(JSON.stringify(u.includes('?f=json')
    ? { maxRecordCount: 5, fields: [{ name: 'OBJECTID', type: 'esriFieldTypeOID' }, ...Object.keys(props).map((name) => ({ name, type: 'esriFieldTypeString' }))] }
    : { features: [{ properties: { OBJECTID: 3451, ...props }, geometry: { type: 'Polygon', coordinates: [[[-104, 31], [-103.9, 31], [-103.9, 31.1], [-104, 31.1], [-104, 31]]] } }] }))) as unknown as typeof fetch;
  const ctx = (f: typeof fetch) => ({ fetch: f, timeoutMs: 1000, retries: 0, now: NOW, priorEdit: {} });
  const a = await pullUL(ctx(mk({ globalid: '{ABC}', blocknum: '6', sectionnum: '7', county: 'ANDREWS' })), ['UL_Section']);
  assert.equal(a.layers[0].records[0].source_record_id, '{ABC}');
  const store = new MemoryStore();
  await runSource('UL', async () => a, store, cfg(), NOW);
  const b = await pullUL(ctx(mk({ globalid: '{ABC}', blocknum: '6', sectionnum: '8', county: 'ANDREWS' })), ['UL_Section']);
  const run = await runSource('UL', async () => b, store, cfg(), NOW);
  assert.match(run.notes.join('\n'), /1 of 1 existing records changed attributes under the same id — MORE THAN 5%/);
});

test('duplicate source ids are disambiguated by OBJECTID in the connector and rejected by the gate if any remain', async () => {
  const mk = (oid: number, id: string) => ({ ...lease(id), raw: { OBJECTID: oid } }) as LeaseRecord;
  const recs = [mk(1, '{G}'), mk(2, '{G}'), mk(3, '{H}')];
  assert.equal(disambiguateIds(recs, 'OBJECTID'), 2);
  assert.deepEqual(recs.map((r) => r.source_record_id), ['{G}#1', '{G}#2', '{H}']);
  const g = validateBatch([lease('X'), lease('X'), lease('Y')], null, cfg());
  assert.equal(g.valid.length, 2); assert.match(g.rejected[0].reasons.join(), /duplicate source_record_id/);
  // end to end through the UL connector: two features, one GlobalID
  const f = (async (u: string) => new Response(JSON.stringify(u.includes('?f=json')
    ? { maxRecordCount: 5, fields: [{ name: 'OBJECTID', type: 'esriFieldTypeOID' }, { name: 'globalid', type: 'esriFieldTypeString' }] }
    : { features: [10, 11].map((o) => ({ properties: { OBJECTID: o, globalid: '{DUP}' }, geometry: { type: 'Polygon', coordinates: [[[-104, 31], [-103.9, 31], [-103.9, 31.1], [-104, 31]]] } })) }))) as unknown as typeof fetch;
  const r = await pullUL({ fetch: f, timeoutMs: 1000, retries: 0, now: NOW, priorEdit: {} }, ['UL_Section']);
  assert.deepEqual(r.layers[0].records.map((x) => x.source_record_id), ['{DUP}#10', '{DUP}#11']);
  assert.match(r.notes.join('\n'), /2 records shared an id/);
});

test('pending permits: wrapped header, quotes, join, decimal-only coordinates, NO_WELLBORE, UNVERIFIED county, mismatched rows reported', () => {
  const wrapped = parseRrcDsv('UNIVERSAL_DOC_NO}CASES_EXCEP_A\nPPROVAL_CODE\n"7"}"a"\n"8"\n');
  assert.deepEqual(wrapped.header, ['UNIVERSAL_DOC_NO', 'CASES_EXCEP_APPROVAL_CODE']);
  assert.equal(wrapped.rows.length, 1); assert.equal(wrapped.rejects.length, 1);

  const permitTxt = ['UNIVERSAL_DOC_NO}STATUS_NUMBER}OPERATOR_NAME}OPERATOR_NUMBER}DISTRICT}DEFAULT_LEASE_NAME}SUBMIT_DATE',
    '"1001"}"1"}"ACME OIL"}"000111"}"08"}"SMITH"}"11/03/2026"', '"1002"}"1"}"BETA"}"000222"}"8A"}"JONES"}"11/04/2026"', '"1003"}"1"}"GAMMA"}"000333"}"08"}"LEE"}"11/04/2026"'].join('\n');
  const wbTxt = ['UNIVERSAL_DOC_NO}COUNTY_CODE}NEAREST_TOWN}LAT_DEGREES}LAT_MINUTES}LAT_SECONDS}LONG_DEGREES}LONG_MINUTES}LONG_SECONDS}LATLONG_TYPE_CODE',
    '"1001"}"003"}"ANDREWS"}"31.05"}""}""}"-103.95"}""}""}"1"', '"1002"}"227"}"SOMEWHERE"}""}""}""}""}""}""}"1"', '"1001"}"003"}"ANDREWS"}"31.06"}""}""}"-103.96"}""}""}"2"', 'broken}line'].join('\n');
  const r = pullPending([{ permitText: permitTxt, wellboreText: wbTxt, ts: '20261105110800', permitFile: 'p.txt', wellboreFile: 'w.txt' }], NOW);
  const recs = r.layers[0].records as BaseRecord[];
  assert.equal(recs.length, 4, 'doc 1001 has two wellbores, 1002 one, 1003 none');
  assert.deepEqual(recs.map((x) => x.source_record_id), ['1001:0', '1001:1', '1002:0', '1003:0']);
  assert.deepEqual(recs[0].geom, { type: 'Point', coordinates: [-103.95, 31.05] }); assert.equal(recs[0].county, 'ANDREWS'); assert.equal(recs[0].permit_status, 'SUBMITTED');
  assert.equal(recs[2].geom, null, 'no lat/long (e.g. state plane) stays NULL'); assert.equal(recs[1].datum, 'NAD83'); assert.equal(recs[0].datum, 'NAD27'); assert.equal(recs[2].county, 'UNVERIFIED code 227');
  assert.deepEqual(recs[3].flags, ['NO_WELLBORE']); assert.equal(recs[3].geom, null);
  assert.equal(recs[0].issued_date, null); assert.equal(recs[0].submit_date, '2026-11-03');
  assert.equal(r.state, 'UNVERIFIED', 'a mismatched row means not OK'); assert.match(r.notes.join('\n'), /1 rows had a value count different/);
  assert.throws(() => pullPending([{ permitText: permitTxt.replace('OPERATOR_NUMBER}', 'OP_NO}'), wellboreText: wbTxt, ts: '20261105110800', permitFile: 'p', wellboreFile: 'w' }], NOW), /missing required columns/);
  assert.deepEqual(listPendingRuns(['dp_drilling_permit_pending_20261105170800.txt', 'dp_wellbore_pending_20261105110800.txt', 'dp_drilling_permit_pending_20261105110800.txt', 'dp_latlongs_pending_20261105111500.txt', 'junk.txt']),
    [{ ts: '20261105110800', permitFile: 'dp_drilling_permit_pending_20261105110800.txt', wellboreFile: 'dp_wellbore_pending_20261105110800.txt' }, { ts: '20261105170800', permitFile: 'dp_drilling_permit_pending_20261105170800.txt', wellboreFile: null }]);
});

test('pending feed is incremental: runs accumulate, a repeated document is replaced, and the window decides the hold', () => {
  const permit = (docs: string[]) => ['UNIVERSAL_DOC_NO}STATUS_NUMBER}OPERATOR_NAME}OPERATOR_NUMBER}SUBMIT_DATE', ...docs.map((d) => `"${d}"}"1"}"OP"}"1"}"03-NOV-26 01.00.00.000000 PM"}`)].join('\n');
  const wb = (docs: string[]) => ['UNIVERSAL_DOC_NO}COUNTY_CODE}LAT_DEGREES}LAT_MINUTES}LAT_SECONDS}LONG_DEGREES}LONG_MINUTES}LONG_SECONDS}LATLONG_TYPE_CODE', ...docs.map((d) => `"${d}"}"003"}"31.05"}""}""}"-103.95"}""}""}"1"}`)].join('\n');
  const run = (ts: string, docs: string[]) => ({ ts, permitFile: `p${ts}`, wellboreFile: `w${ts}`, permitText: permit(docs), wellboreText: wb(docs) });
  const r = pullPending([run('20261104110800', ['1', '2']), run('20261104170800', []), run('20261105110800', ['3', '2'])], NOW);
  assert.equal(r.layers[0].records.length, 3, 'docs 1,2,3 accumulate; doc 2 repeated across runs counts once');
  assert.match(r.notes.join(), /3 runs/); assert.match(r.notes.join(), /1 with no permits/); assert.match(r.notes.join(), /1 documents repeated/);
  assert.equal(r.layers[0].records.find((x) => x.source_record_id === '2:0')!.source_updated_at, '2026-11-05T11:08:00');

  const sub = (date: string) => ({ entity_type: 'permit', source_name: 'RRC', source_url: 'u', source_layer: 'permit_pending', source_record_id: '1:0', source_updated_at: null, retrieved_at: NOW, raw: {},
    geom: { type: 'Point', coordinates: [-103.95, 31.05] }, permit_status: 'SUBMITTED', issued_date: null, submit_date: date, county: 'Reeves' }) as BaseRecord;
  const c = buildCandidates(inputs({ permits: [sub('2026-10-01')] }), cfg(), NOW)[0];
  assert.equal(c.status, 'HOLD'); assert.ok(c.reason_codes.includes('PERMIT_SUBMITTED_RECENT')); assert.ok(c.reason_codes.includes('HOLD:PERMIT_SUBMITTED'));
  assert.equal(buildCandidates(inputs({ permits: [sub('2020-01-01')] }), cfg(), NOW)[0].status, CANDIDATE_STATUS, 'submitted long before the window');
  assert.equal(buildCandidates(inputs({ permits: [{ ...sub('2026-10-01'), geom: { type: 'Point', coordinates: [-100, 30] } } as BaseRecord] }), cfg(), NOW)[0].status, CANDIDATE_STATUS, 'elsewhere');
});

test('pending permits: real RRC format (47-column header, trailing delimiter, Oracle dates, header-only empty runs)', () => {
  const header = 'SWR38_ABBR_NOTICE}IS_REAPPLIED}UNIVERSAL_DOC_NO}STATUS_NUMBER}EFFECTIVE_DT}RETURN_DT}TOTAL_DEPTH}IS_AMENDMENT}SWR_36_FLAG}DEVELOP_MINERALS_FLAG}CASE_DOCKET_NO}FINAL_PROTEST_DT}STATUS_SEQ_NO}SPUD_DT}EXPEDITE_FLAG}EXPEDITE_DATE_TIME}FILING_PURPOSE_CODE}SURFACE_CASING_DT}DEFAULT_LEASE_NAME}DEFAULT_WELL_NUMBER}DEFAULT_VERTICAL}DEFAULT_HORIZANTAL}DEFAULT_SIDETRACK}LOCKED_BY}DEFAULT_DIRECTIONAL}STATUS_CODE}EXPIRATION_DATE}WALKIN_CONTACT_NAME}WALKIN_CONTACT_PHONE}COMPLETION_CODE}SWR_SUBSECT_CODE}STAT_DT}CURRENT_STATE_CODE}BRIDGE_FLAG}SWR_LIST}BRIDGE_PRINT_FLAG}HAS_DISCREPANCY}SUBMIT_DATE}CREATE_DATE}UNIQUE_ADDRESS_NUMBER}DKT_SUFFIX_CODE}DKT_EXAMINER_CODE}REAPPLIED_STATUS_NO}OPERATOR_NAME}OPERATOR_NUMBER}OPERATOR_PHONE}DISTRICT';
  const row = '""} ""} "493747045"} "864960"} ""} ""} "14000"} "N"} "N"} "Y"} ""} ""} "99"} ""} "Y"} ""} "01"} ""} "CKODRE A"} "   6H "} "N"} "Y"} "N"}""} "N"} "P"} ""} ""} ""} ""} ""} ""} "MAP"} ""} "36,37"} ""} "N"} "03-SEP-20 12.26.41.382000 PM"} "02-SEP-20 02.00.16.926000 PM"} ""} ""} ""} ""}"BPX OPERATING COMPANY"}"085408"}"7204395104"}"02"}';
  assert.equal(header.split('}').length, 47);
  const p = parseRrcDsv(`${header}\n${row}\n${row.replace('493747045', '493743020')}\n`);
  assert.equal(p.rows.length, 2); assert.equal(p.rejects.length, 0);
  assert.equal(p.rows[0].UNIVERSAL_DOC_NO, '493747045'); assert.equal(p.rows[0].OPERATOR_NAME, 'BPX OPERATING COMPANY'); assert.equal(p.rows[0].DEFAULT_LEASE_NAME, 'CKODRE A'); assert.equal(p.rows[0].DISTRICT, '02');
  const empty = parseRrcDsv(header); assert.equal(empty.rows.length, 0); assert.equal(empty.header.length, 47);
  assert.equal(parseDate('03-SEP-20 12.26.41.382000 PM'), '2020-09-03'); assert.equal(parseDate('06-FEB-21'), '2021-02-06'); assert.equal(parseDate('01-JAN-99'), '1999-01-01'); assert.equal(parseDate('99-XXX-20'), null);
});

test('wellbore coordinates: decimal, degrees-minutes-seconds and state plane (real 2026-11-06 rows); LATLONG_TYPE_CODE is the datum', () => {
  const near = (a: number, b: number) => Math.abs(a - b) < 1e-5;
  const dec = wellboreCoord({ LATLONG_TYPE_CODE: '1', LAT_DEGREES: '31.128878', LAT_MINUTES: '', LAT_SECONDS: '', LONG_DEGREES: '-103.20394', LONG_MINUTES: '', LONG_SECONDS: '' })!;
  assert.ok(near(dec.lat, 31.128878) && near(dec.lon, -103.20394)); assert.equal(dec.style, 'decimal'); assert.equal(dec.datum, 'NAD27');
  const dms = wellboreCoord({ LATLONG_TYPE_CODE: '1', LAT_DEGREES: '31', LAT_MINUTES: '36', LAT_SECONDS: '18.18', LONG_DEGREES: '-103', LONG_MINUTES: '4', LONG_SECONDS: '36.16' })!;
  assert.ok(near(dms.lat, 31 + 36 / 60 + 18.18 / 3600) && near(dms.lon, -(103 + 4 / 60 + 36.16 / 3600))); assert.equal(dms.style, 'dms');
  assert.ok(dms.lon < -103.07 && dms.lon > -103.08, 'minutes/seconds are NOT dropped (the whole-degree bug put this at -103)');
  assert.equal(wellboreCoord({ LATLONG_TYPE_CODE: '1', LAT_DEGREES: '', LONG_DEGREES: '', LAT_MINUTES: '', LAT_SECONDS: '', LONG_MINUTES: '', LONG_SECONDS: '' }), null, 'state plane only');
  assert.equal(wellboreCoord({ LATLONG_TYPE_CODE: '3', LAT_DEGREES: '31', LAT_MINUTES: '75', LAT_SECONDS: '0', LONG_DEGREES: '-103', LONG_MINUTES: '0', LONG_SECONDS: '0' }), null, 'malformed minutes are rejected, not guessed');
  assert.equal(wellboreCoord({ LATLONG_TYPE_CODE: '9', LAT_DEGREES: '31.1', LONG_DEGREES: '-103.1', LAT_MINUTES: '', LAT_SECONDS: '', LONG_MINUTES: '', LONG_SECONDS: '' })!.datum, 'UNKNOWN');
});

// Real RRC lines (daf420.dat.10-31-2026): an Andrews UL permit WITH GIS coordinates and a Reagan UL permit WITHOUT them.
const MASTER_ANDREWS = [
    "01087837898003UNIVERSITY 8-10E                10628658    20260929OVINTIV USA INC.                00ANNNNNNNNNNN08783782022032900000000N                       3MSN000000000000000000 E00000000000000000000000000000",
    "02087837898003UNIVERSITY 8-10E                10   3MS1050062865801                              000000797020000000000000202609292022032920261013000000002022060220220603 000000002024032900000000                              NNNN00000000000000N10      8         UL                                                     U228  00000097103001397NE    ANDREWS      00160000NORTH        00220600EAST         00160000NORTH        00220600EAST         1.0                         O00000000 NNNN09779671 YN       00348561",
    "0515      8               UL/ A-U233                                             000971031.0                         00001200SOUTH        00219200EAST         00001200SOUTH        00219200EAST         ANDREWS      00222800NORTH        00175000EAST         000000",
    "14: -102.3218569  32.3932279",
    "15: -102.3142400  32.3694920",
  ].join('\r\n') + '\r\n';
const MASTER_REAGAN = [
    "01091964899383UL ACFD 4475 D                  09101935    20261023BLACK SWAN OPCO, LLC            00ANNNNNNNNNNN09196482026102800000000N                    1701UBN000000000000000000 P00000000000000000000000000000",
    "02091964899383UL ACFD 4475 D                  091701UB0802010193501                              000000730130000000000000202610232026102800000000000000000000000000000000 000000002028102800000000                              NNNN00000000000000N17      8         UNIVERSITY LANDS                                       U152  00001127359000800W     BIG LAKE     00631800W            01093100N            00104000W            00032100N            258.0                       O00000000 NNNN09738972 YN       38342507",
    "0528      8         163   UNIVERSITY LANDS                                       01127359258.0                       00033000W            00010000S            00098900E            00010000S            REAGAN       00473000W            01071100N            000000",
  ].join('\r\n') + '\r\n';

test('permit master (OGA049M): root fields, issue date, surface text location, BHL text, NAD27 coordinates, and counts on real lines', () => {
  const a = parseMasterText(MASTER_ANDREWS); assert.equal(a.roots.length, 1);
  const r = a.roots[0];
  assert.equal(r.county_code, '003'); assert.equal(r.flag, 'A'); assert.match(r.lease, /^UNIVERSITY 8-10/); assert.equal(r.issued, '2022-03-29'); assert.equal(r.amended, '2026-10-13'); assert.equal(r.spud, '2022-06-02'); assert.ok(r.permit_no);
  assert.ok(r.lon !== null && r.lat !== null && r.lon < -100 && r.lon > -105 && r.lat > 31 && r.lat < 33, 'longitude then latitude, Texas-sized');
  const surf = r.locations.find((l) => l.kind === 'surface')!; assert.equal(surf.survey, 'UL'); assert.equal(surf.block, '8'); assert.equal(surf.section, '10');
  const g = parseMasterText(MASTER_REAGAN).roots[0]; assert.equal(g.lon, null); const gs = g.locations.find((l) => l.kind === 'surface')!;
  assert.equal(gs.survey, 'UNIVERSITY LANDS'); assert.equal(gs.block, '8'); assert.equal(gs.section, '17'); assert.equal(g.county_code, '383');
  assert.deepEqual([masterFileMonth('daf420.dat.10-31-2026', '2026-11-06T00:00:00Z'), masterFileMonth('daf420.dat', '2026-11-06T00:00:00Z'), masterFileMonth('other.txt', 'x')], ['2026-10', '2026-11', null]);
  assert.equal(masterFileAsOf('daf420.dat.10-31-2026', '2026-11-06T00:00:00.000Z'), '2026-10-31T00:00:00.000Z');
  assert.deepEqual(monthsBack('2026-11-07T00:00:00Z', 3), ['2026-11', '2026-10', '2026-09']);
});

test('permit master: coverage of the required months decides OK; a later file replaces a repeated permit; datum recorded', () => {
  const files = [{ name: 'daf420.dat.10-31-2026', text: MASTER_ANDREWS, asOfIso: '2026-11-06T00:00:00.000Z' }, { name: 'daf420.dat', text: MASTER_ANDREWS + MASTER_REAGAN, asOfIso: '2026-11-06T00:00:00.000Z' }];
  const short = pullPermitMaster(files, '2026-11-07T00:00:00.000Z', 24);
  assert.equal(short.state, 'UNVERIFIED'); assert.match(short.notes.join('\n'), /permit coverage INCOMPLETE: 2 of 24 months/);
  const ok = pullPermitMaster(files, '2026-11-07T00:00:00.000Z', 2);
  assert.equal(ok.state, 'OK'); const recs = ok.layers[0].records as BaseRecord[];
  assert.equal(recs.length, 2, 'the repeated Andrews permit counts once'); assert.match(ok.notes.join('\n'), /1 repeated across files/);
  assert.equal(recs.find((x) => x.county === 'ANDREWS')!.datum, 'NAD27'); assert.equal(recs.find((x) => x.county === 'REAGAN')!.geom, null);
  assert.equal(combineRRC([ok, { source: 'RRC', state: 'OK', notes: [], layers: [{ layer: 'production', url: 'u', entity_type: 'production_monthly', skipped_unchanged: false, source_updated_at: null, records: [], unmapped: [], fields_discovered: [] }] }]).state, 'OK', 'permits + production = good sync');
});

test('permit rule 2: buffered coordinate test and text match for permits without coordinates', () => {
  const sec = (county: string, block: string, section: string) => ({ ...section_(), county, block_no: block, section_no: section }) as Section;
  function section_() { return section('S1'); }
  const permit = (over: Record<string, unknown>) => ({ entity_type: 'permit', source_name: 'RRC', source_url: 'u', source_layer: 'permit', source_record_id: 'p', source_updated_at: null, retrieved_at: NOW, raw: {},
    geom: null, permit_status: 'APPROVED', issued_date: '2026-09-01', county: 'Reeves', locations: [], ...over }) as BaseRecord;
  const run = (permits: BaseRecord[], s: Section = sec('Reeves', '8', '17')) => buildCandidates(inputs({ sections: [s], permits }), cfg(), NOW);
  // section is the 0.1-degree square at (-104,31): a point 0.0009 deg (~0.06 mi) outside the west edge is within the 0.15 mi buffer, 0.01 deg (~0.7 mi) is not
  assert.equal(run([permit({ geom: { type: 'Point', coordinates: [-104.0009, 31.05] } })]).length, 0, 'inside the NAD27 buffer');
  assert.equal(run([permit({ geom: { type: 'Point', coordinates: [-104.01, 31.05] } })]).length, 1, 'clearly outside');
  const text = (survey: string, block: string, sect: string, county = 'REEVES') => permit({ locations: [{ kind: 'surface', county, section: sect, block, survey, abstract: '' }], county });
  assert.equal(run([text('UNIVERSITY LANDS', '8', '17')]).length, 0, 'UL survey, same block and section: excluded');
  assert.equal(run([text('UL', '08', '017')]).length, 0, 'leading zeros ignored');
  assert.equal(run([text('T&P RR CO', '8', '17')]).length, 1, 'a T&P survey is not University Lands');
  assert.equal(run([text('UNIVERSITY LANDS', '8', '18')]).length, 1, 'different section');
  assert.equal(run([text('UNIVERSITY LANDS', '8', '17', 'LOVING')]).length, 1, 'different county');
  assert.equal(run([permit({ geom: { type: 'Point', coordinates: [-103.95, 31.05] }, issued_date: '2020-01-01' })]).length, 1, 'older than the 24-month window');
  assert.equal(pointNearGeometry([-103.95, 31.05], sec('x', '1', '1').geom!, 0.01), true, 'inside is near');
  // issued long ago but amended recently: still active (the real Andrews permit: issued 2022-03-29, amended 2026-10-13)
  const amended = permit({ geom: { type: 'Point', coordinates: [-103.95, 31.05] }, issued_date: '2022-03-29', activity_date: '2026-10-13' });
  assert.equal(run([amended]).length, 0, 'recent amendment keeps the tract off the list');
  assert.equal(run([{ ...amended, activity_date: '2022-03-29' } as BaseRecord]).length, 1, 'nothing recent');
});

test('text matching works on sections that only have raw attributes (loaded before block_no/section_no existed)', () => {
  const old = { ...section('S1'), county: 'Reeves', raw: { blocknum: '8', sectionnum: '17' } } as Section; // no block_no / section_no fields
  const permit = { entity_type: 'permit', source_name: 'RRC', source_url: 'u', source_layer: 'permit', source_record_id: 'p', source_updated_at: null, retrieved_at: NOW, raw: {},
    geom: null, permit_status: 'APPROVED', issued_date: '2026-09-01', county: 'REEVES', locations: [{ kind: 'surface', county: null, section: '17', block: '8', survey: 'UL', abstract: '' }] } as BaseRecord;
  assert.equal(buildCandidates(inputs({ sections: [old], permits: [permit] }), cfg(), NOW).length, 0);
  const noNumbers = { ...old, raw: {} } as Section;
  assert.equal(buildCandidates(inputs({ sections: [noNumbers], permits: [permit] }), cfg(), NOW).length, 1, 'a section with no block/section can never be text-matched (blank never equals blank)');
});

test('a University Lands permit with a blank block or section is flagged on candidates in its county', () => {
  const mk = (block: string, section: string, county = 'REEVES') => ({ entity_type: 'permit', source_name: 'RRC', source_url: 'u', source_layer: 'permit', source_record_id: 'p', source_updated_at: null, retrieved_at: NOW, raw: {},
    geom: null, permit_status: 'APPROVED', issued_date: '2026-09-01', county, locations: [{ kind: 'surface', county: null, section, block, survey: 'UL', abstract: '' }] }) as BaseRecord;
  const flagged = (permits: BaseRecord[]) => buildCandidates(inputs({ permits }), cfg(), NOW)[0].reason_codes.includes('UL_PERMIT_UNPLACED_IN_COUNTY');
  assert.equal(flagged([mk('', '34')]), true); assert.equal(flagged([mk('8', '')]), true);
  assert.equal(flagged([mk('8', '34')]), false, 'complete location: it is placed (or matches a different section)');
  assert.equal(flagged([mk('', '34', 'LOVING')]), false, 'different county');
});
