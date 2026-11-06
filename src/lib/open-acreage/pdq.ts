import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import type { ConnectorResult, LayerPull } from './connectors.ts';
import type { BaseRecord } from './types.ts';

/**
 * RRC Production Data Query Dump (PDQ_DSV.zip). Format per the RRC manual (pdq-dump-user-manual.pdf, read 2026-11-06):
 * Oracle SQL*Plus export, delimiter "}", header row included, no enclosures, one file per table named <TABLE>_DATA_TABLE.dsv.
 * Because the header is present we read by COLUMN NAME (names verified against the manual), never by position.
 * Units per the manual's data dictionary (verified): oil + condensate BBL, gas + casinghead gas MCF; LEASE_NO is unique within a district, so the
 * lease key is OIL_GAS_CODE:DISTRICT_NO:LEASE_NO. UNVERIFIED until a real file is read: that the share link serves the zip, the member names
 * inside it, and the header spelling (the loader refuses to run on a missing column).
 */
export const PDQ_WELL_COMPLETION = 'OG_WELL_COMPLETION_DATA_TABLE.dsv';
export const PDQ_LEASE_CYCLE = 'OG_LEASE_CYCLE_DATA_TABLE.dsv';
const WELL_COLS = ['OIL_GAS_CODE', 'DISTRICT_NO', 'LEASE_NO', 'API_COUNTY_CODE', 'API_UNIQUE_NO'];
const CYCLE_COLS = ['OIL_GAS_CODE', 'DISTRICT_NO', 'LEASE_NO', 'CYCLE_YEAR_MONTH', 'LEASE_OIL_PROD_VOL', 'LEASE_GAS_PROD_VOL', 'LEASE_COND_PROD_VOL', 'LEASE_CSGD_PROD_VOL'];

type Lines = AsyncIterable<string> | Iterable<string>;

/** Streams one member of a zip through the system `unzip -p` (no dependency; the dump is >25 GB uncompressed). */
export async function* unzipLines(zipPath: string, member: string): AsyncGenerator<string> {
  const p = spawn('unzip', ['-p', zipPath, member], { stdio: ['ignore', 'pipe', 'pipe'] });
  let err = ''; p.stderr.on('data', (d) => { err += d; });
  const exit = new Promise<number>((res) => p.on('close', (c) => res(c ?? -1)));
  for await (const l of createInterface({ input: p.stdout, crlfDelay: Infinity })) yield l;
  const code = await exit;
  if (code !== 0) throw new Error(`unzip ${member} failed (exit ${code}): ${err.trim().slice(0, 200)} — check the member name inside ${zipPath}`);
}

/** Export date of a zip member from `unzip -l` (macOS: MM-DD-YYYY, Linux: YYYY-MM-DD). This is the dump's real freshness, not the download time. */
export function parseZipMemberDate(listing: string, member: string): string | null {
  for (const line of listing.split('\n')) {
    if (!line.trim().endsWith(member)) continue;
    const us = /(\d{2})-(\d{2})-(\d{4})\s+(\d{2}:\d{2})/.exec(line); if (us) return `${us[3]}-${us[1]}-${us[2]}T${us[4]}:00.000Z`;
    const iso = /(\d{4})-(\d{2})-(\d{2})\s+(\d{2}:\d{2})/.exec(line); if (iso) return `${iso[1]}-${iso[2]}-${iso[3]}T${iso[4]}:00.000Z`;
  }
  return null;
}

export function zipMemberDate(zipPath: string, member: string): Promise<string | null> {
  return new Promise((resolve) => {
    const p = spawn('unzip', ['-l', zipPath], { stdio: ['ignore', 'pipe', 'ignore'] }); let out = '';
    p.stdout.on('data', (d) => { out += d; }); p.on('close', () => resolve(parseZipMemberDate(out, member))); p.on('error', () => resolve(null));
  });
}

/** Normalizes a UL/RRC API to RRC's 8 digits (3-digit county + 5-digit unique). Anything else => null (never guessed). */
export function api8(raw: unknown): string | null {
  const d = String(raw ?? '').replace(/\D/g, '');
  if (d.length === 8) return d;
  if (d.length === 10 || d.length === 12 || d.length === 14) return d.slice(2, 10); // leading 2-digit state code (42 = Texas)
  return null;
}

async function* rows(lines: Lines, required: string[], what: string): AsyncGenerator<Record<string, string>> {
  let idx: Map<string, number> | null = null;
  for await (const raw of lines as AsyncIterable<string>) {
    if (!raw) continue;
    const cells = raw.split('}');
    if (!idx) {
      idx = new Map(cells.map((c, i) => [c.trim().toUpperCase(), i]));
      const missing = required.filter((c) => !idx!.has(c));
      if (missing.length) throw new Error(`${what}: header is missing required columns ${missing.join(', ')} — refusing to read (layout UNVERIFIED)`);
      continue;
    }
    const o: Record<string, string> = {};
    for (const c of required) o[c] = (cells[idx.get(c)!] ?? '').trim();
    yield o;
  }
  if (!idx) throw new Error(`${what}: empty file (no header row)`);
}

const leaseKey = (r: Record<string, string>) => `${r.OIL_GAS_CODE}:${r.DISTRICT_NO}:${r.LEASE_NO}`;
const vol = (s: string) => { if (s === '') return null; const n = Number(s); return Number.isFinite(n) ? n : null; };

/** lease key -> the 8-digit APIs of OUR wells on that lease. */
export async function pdqLeaseApiMap(lines: Lines, wanted: Set<string>): Promise<Map<string, string[]>> {
  const m = new Map<string, string[]>();
  for await (const r of rows(lines, WELL_COLS, PDQ_WELL_COMPLETION)) {
    const a = `${r.API_COUNTY_CODE}${r.API_UNIQUE_NO}`;
    if (!/^\d{8}$/.test(a) || !wanted.has(a)) continue;
    const k = leaseKey(r); const cur = m.get(k);
    if (!cur) m.set(k, [a]); else if (!cur.includes(a)) cur.push(a);
  }
  return m;
}

export interface PdqOpts { wantedApi8: Set<string>; cutoffYm: string; now: string; sourceUrl: string; sourceUpdatedAt: string | null }

/**
 * One record per lease-month carrying the 8-digit APIs of OUR wells on that lease (api8_list). Volumes are lease-level, not allocated per well.
 * Rows with no volume above zero are counted but not stored (the candidate rule only needs "produced > 0"); blank volumes stay NULL.
 */
export async function pdqProduction(lines: Lines, leaseApis: Map<string, string[]>, o: PdqOpts): Promise<{ records: BaseRecord[]; scanned: number; skippedZero: number }> {
  const records: BaseRecord[] = []; let scanned = 0; let skippedZero = 0;
  for await (const r of rows(lines, CYCLE_COLS, PDQ_LEASE_CYCLE)) {
    scanned++;
    if (r.CYCLE_YEAR_MONTH < o.cutoffYm) continue;
    const apis = leaseApis.get(leaseKey(r)); if (!apis) continue;
    const ym = r.CYCLE_YEAR_MONTH; if (!/^\d{6}$/.test(ym)) continue;
    const oil = vol(r.LEASE_OIL_PROD_VOL); const gas = vol(r.LEASE_GAS_PROD_VOL); const cond = vol(r.LEASE_COND_PROD_VOL); const csgd = vol(r.LEASE_CSGD_PROD_VOL);
    if (![oil, gas, cond, csgd].some((v) => v !== null && v > 0)) { skippedZero++; continue; }
    const key = leaseKey(r);
    records.push({
      entity_type: 'production_monthly', source_name: 'RRC', source_url: o.sourceUrl, source_layer: 'production', source_record_id: `${key}:${ym}`,
      source_updated_at: o.sourceUpdatedAt, retrieved_at: o.now, geom: null, well_or_lease_id: key, lease_key: key, api8_list: apis, month: `${ym.slice(0, 4)}-${ym.slice(4, 6)}-01`,
      oil_bbl: oil, gas_mcf: gas, cond_bbl: cond, csgd_mcf: csgd, raw: r,
    } as BaseRecord);
  }
  return { records, scanned, skippedZero };
}

export async function pullPdq(src: { wellCompletion: Lines; leaseCycle: Lines }, o: PdqOpts): Promise<ConnectorResult> {
  const notes: string[] = [];
  const map = await pdqLeaseApiMap(src.wellCompletion, o.wantedApi8);
  const linked = new Set<string>(); for (const apis of map.values()) for (const a of apis) linked.add(a);
  notes.push(`PDQ: ${map.size} RRC leases link to ${linked.size} of ${o.wantedApi8.size} wanted wells (${o.wantedApi8.size - linked.size} wanted wells have no RRC lease link)`);
  const { records, scanned, skippedZero } = await pdqProduction(src.leaseCycle, map, o);
  notes.push(`PDQ: scanned ${scanned} lease-month rows; kept ${records.length} lease-months with production > 0 (months >= ${o.cutoffYm}); ${skippedZero} zero/blank rows for linked leases not stored`);
  const layer: LayerPull = { layer: 'production', url: o.sourceUrl, entity_type: 'production_monthly', skipped_unchanged: false, source_updated_at: o.sourceUpdatedAt, records, unmapped: [], fields_discovered: CYCLE_COLS };
  return { source: 'RRC', state: 'OK', layers: [layer], notes };
}

/** RRC is a good sync only with permits AND production (permits alone would let the production rule pass vacuously). */
export function combineRRC(parts: ConnectorResult[]): ConnectorResult {
  const layers = parts.flatMap((p) => p.layers); const notes = parts.flatMap((p) => p.notes);
  const names = new Set(layers.map((l) => l.layer));
  const hasPermit = names.has('permit'); const hasProd = names.has('production') || (names.has('production_oil') && names.has('production_gas'));
  if (!hasPermit) notes.push('RRC incomplete — permits not loaded (not a good sync; candidates stay HOLD)');
  if (!hasProd) notes.push('RRC incomplete — production not loaded (not a good sync; candidates stay HOLD)');
  const allOk = parts.length > 0 && parts.every((p) => p.state === 'OK');
  return { source: 'RRC', state: allOk && hasPermit && hasProd ? 'OK' : layers.length ? 'UNVERIFIED' : 'NOT FOUND', layers, notes };
}
