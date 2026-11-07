import type { ConnectorResult, LayerPull } from './connectors.ts';
import { parseDate } from './normalize.ts';
import type { BaseRecord } from './types.ts';

/**
 * RRC "Drilling Permits Pending Approval" (per AMARA-RRC-PERMIT-LANE-SPEC, observed 2026-11-05): files dp_<table>_pending_<YYYYMMDDHHMMSS>.txt,
 * "}"-delimited, values double-quoted, header row may wrap across lines (joined up to the first line that starts with a quote),
 * tables join on UNIVERSAL_DOC_NO. Read by COLUMN NAME; refuse on a missing column; keep every row (mismatched rows are reported, not dropped).
 * VERIFIED on 250 real runs (2020-08-30..2021-02-01): each run lists only permits NEWLY submitted since the previous run (no document appears in two
 * runs; every STATUS_CODE is "P"), so the feed is INCREMENTAL, not a snapshot of the pending queue, and it never says when a permit is approved.
 * We therefore accumulate every run in the folder and treat each record as "submitted on SUBMIT_DATE" (permit_status 'SUBMITTED'), never as
 * issued. It does not satisfy the issued-permit rule and does not make RRC a good sync; the Drilling Permit Master (OGA049) is the cumulative source.
 */
export const PENDING_SHARE_URL = 'https://mft.rrc.texas.gov/link/0ad92a65-4212-49a1-98a7-d667a55fb497';
const PERMIT_COLS = ['UNIVERSAL_DOC_NO', 'OPERATOR_NAME', 'OPERATOR_NUMBER', 'SUBMIT_DATE'];
const WELLBORE_COLS = ['UNIVERSAL_DOC_NO', 'COUNTY_CODE', 'LAT_DEGREES', 'LONG_DEGREES', 'LAT_MINUTES', 'LAT_SECONDS', 'LONG_MINUTES', 'LONG_SECONDS', 'LATLONG_TYPE_CODE'];

// FIPS codes of the configured target counties only (public standard). Any other code is written as "UNVERIFIED code NNN", never guessed.
export const PENDING_COUNTY: Record<string, string> = { '003': 'ANDREWS', '135': 'ECTOR', '173': 'GLASSCOCK', '317': 'MARTIN', '329': 'MIDLAND', '383': 'REAGAN', '461': 'UPTON' };

/**
 * Wellbore surface coordinates (manual pendingdrillingpermits.pdf v2.2): reported in ONE of three styles —
 *   decimal degrees (LAT_DEGREES/LONG_DEGREES hold the full value; minutes/seconds empty),
 *   degrees-minutes-seconds (LAT_DEGREES + LAT_MINUTES + LAT_SECONDS; LONG_DEGREES is negative), or
 *   state plane (STATE_PLANE_ZONE_CODE + X/Y, no lat/long) — not converted here (needs a projection): geometry NULL.
 * LATLONG_TYPE_CODE is the DATUM (1 NAD27, 2 NAD83, 3 WGS84), not a format flag.
 */
export function wellboreCoord(wb: Record<string, string>): { lon: number; lat: number; style: 'decimal' | 'dms'; datum: string } | null {
  const num = (v: string | undefined) => (v === undefined || v.trim() === '' ? null : Number(v));
  const lat0 = num(wb.LAT_DEGREES); const lon0 = num(wb.LONG_DEGREES);
  if (lat0 === null || lon0 === null || !Number.isFinite(lat0) || !Number.isFinite(lon0) || lat0 === 0 || lon0 === 0) return null;
  const dms = [wb.LAT_MINUTES, wb.LAT_SECONDS, wb.LONG_MINUTES, wb.LONG_SECONDS].some((v) => v !== undefined && v.trim() !== '');
  const datum = ({ '1': 'NAD27', '2': 'NAD83', '3': 'WGS84' } as Record<string, string>)[wb.LATLONG_TYPE_CODE] ?? 'UNKNOWN';
  if (!dms) return { lon: lon0, lat: lat0, style: 'decimal', datum };
  const parts = [wb.LAT_MINUTES, wb.LAT_SECONDS, wb.LONG_MINUTES, wb.LONG_SECONDS].map((v) => num(v) ?? 0);
  if (parts.some((x) => !Number.isFinite(x) || x < 0 || x >= 60)) return null; // malformed minutes/seconds: never guess
  const lat = lat0 + parts[0] / 60 + parts[1] / 3600;
  const lon = (lon0 < 0 ? -1 : 1) * (Math.abs(lon0) + parts[2] / 60 + parts[3] / 3600);
  return { lon, lat, style: 'dms', datum };
}

export function parseRrcDsv(text: string): { header: string[]; rows: Array<Record<string, string>>; rejects: string[] } {
  const lines = text.split(/\r?\n/);
  const clean = (v: string) => v.trim().replace(/^"|"$/g, '').trim();
  const k = lines.findIndex((l) => l.trim().startsWith('"'));
  // A header-only file is a valid empty run. Otherwise the header is every line before the first quoted data line (it can wrap across lines).
  const headLines = k < 0 ? lines.filter((l) => l.trim()) : lines.slice(0, k);
  if (!headLines.length) throw new Error('file has no header');
  const header = headLines.join('').split('}').map((h) => clean(h).toUpperCase());
  const rows: Array<Record<string, string>> = []; const rejects: string[] = [];
  if (k < 0) return { header, rows, rejects };
  for (const l of lines.slice(k)) {
    if (!l.trim()) continue;
    const vals = l.split('}').map(clean);
    // RRC terminates every data line with a trailing delimiter (observed 2026-11: 47 header columns, 48 split values with an empty last one).
    if (vals.length === header.length + 1 && vals[vals.length - 1] === '') vals.pop();
    if (vals.length !== header.length) { rejects.push(l); continue; }
    rows.push(Object.fromEntries(header.map((h, i) => [h, vals[i]])));
  }
  return { header, rows, rejects };
}

export interface PendingRunFiles { ts: string; permitFile: string; wellboreFile: string | null }

/** Every run that has a drilling_permit file (a wellbore file is optional: without it the permits are kept, flagged NO_WELLBORE), oldest first. */
export function listPendingRuns(names: string[]): PendingRunFiles[] {
  const by = new Map<string, { permit?: string; wellbore?: string }>();
  for (const n of names) {
    const m = /^dp_(drilling_permit|wellbore)_pending_(\d{14})\.txt$/.exec(n); if (!m) continue;
    const e = by.get(m[2]) ?? {}; if (m[1] === 'drilling_permit') e.permit = n; else e.wellbore = n; by.set(m[2], e);
  }
  return [...by.entries()].filter(([, e]) => e.permit).sort(([a], [b]) => a.localeCompare(b)).map(([ts, e]) => ({ ts, permitFile: e.permit!, wellboreFile: e.wellbore ?? null }));
}

const needCols = (header: string[], cols: string[], what: string) => {
  const miss = cols.filter((c) => !header.includes(c));
  if (miss.length) throw new Error(`${what}: header is missing required columns ${miss.join(', ')} — refusing to read`);
};

export interface PendingRunInput extends PendingRunFiles { permitText: string; wellboreText: string | null }

export function pullPending(runs: PendingRunInput[], now: string): ConnectorResult {
  const notes: string[] = []; const byDoc = new Map<string, BaseRecord[]>(); const rejects: string[] = [];
  let permitRows = 0; let wellboreRows = 0; let noWellbore = 0; let nonDecimal = 0; let unverifiedCounty = 0; let dupDocs = 0; let emptyRuns = 0; const widths = new Set<number>();
  const iso = (t: string) => `${t.slice(0, 4)}-${t.slice(4, 6)}-${t.slice(6, 8)}T${t.slice(8, 10)}:${t.slice(10, 12)}:${t.slice(12, 14)}`; // timezone UNVERIFIED (no zone suffix)
  for (const run of [...runs].sort((a, b) => a.ts.localeCompare(b.ts))) {
    const p = parseRrcDsv(run.permitText); needCols(p.header, PERMIT_COLS, run.permitFile); widths.add(p.header.length);
    const w = run.wellboreText ? parseRrcDsv(run.wellboreText) : null; if (w) needCols(w.header, WELLBORE_COLS, run.wellboreFile!);
    rejects.push(...p.rejects.map((l) => `${run.permitFile}: ${l.slice(0, 100)}`), ...(w ? w.rejects.map((l) => `${run.wellboreFile}: ${l.slice(0, 100)}`) : []));
    permitRows += p.rows.length; wellboreRows += w?.rows.length ?? 0; if (!p.rows.length) emptyRuns++;
    const wbByDoc = new Map<string, Array<Record<string, string>>>();
    for (const r of w?.rows ?? []) (wbByDoc.get(r.UNIVERSAL_DOC_NO) ?? wbByDoc.set(r.UNIVERSAL_DOC_NO, []).get(r.UNIVERSAL_DOC_NO)!).push(r);
    const base = { entity_type: 'permit' as const, source_name: 'RRC', source_url: PENDING_SHARE_URL, source_layer: 'permit_pending', source_updated_at: iso(run.ts), retrieved_at: now };
    for (const pr of p.rows) {
      const doc = pr.UNIVERSAL_DOC_NO; const wbs = wbByDoc.get(doc) ?? [];
      const common = { permit_no: null, permit_status: 'SUBMITTED', issued_date: null, universal_doc_no: doc, operator_name: pr.OPERATOR_NAME || null, operator_no: pr.OPERATOR_NUMBER || null,
        submit_date: parseDate(pr.SUBMIT_DATE), lease_name: pr.DEFAULT_LEASE_NAME || null, district: pr.DISTRICT || null };
      const recs: BaseRecord[] = [];
      if (!wbs.length) { noWellbore++; recs.push({ ...base, ...common, source_record_id: `${doc}:0`, county: null, geom: null, flags: ['NO_WELLBORE'], raw: { permit: pr, file: run.permitFile } } as BaseRecord); }
      else wbs.forEach((wb, i) => {
        const c = wellboreCoord(wb); // no sign flip, no guess: the bbox gate judges the values as given
        if (!c) nonDecimal++;
        const known = PENDING_COUNTY[wb.COUNTY_CODE]; if (!known) unverifiedCounty++;
        recs.push({ ...base, ...common, source_record_id: `${doc}:${i}`, county: known ?? `UNVERIFIED code ${wb.COUNTY_CODE}`, county_code: wb.COUNTY_CODE, nearest_town: wb.NEAREST_TOWN || null,
          geom: c ? { type: 'Point', coordinates: [c.lon, c.lat] } : null, datum: c?.datum ?? null, coord_style: c?.style ?? (wb.STATE_PLANE_X ? 'state_plane (not converted)' : null), raw: { permit: pr, wellbore: wb, files: [run.permitFile, run.wellboreFile] } } as BaseRecord);
      });
      if (byDoc.has(doc)) dupDocs++; byDoc.set(doc, recs); // a document seen again in a later run replaces the earlier one (never observed in 250 real runs)
    }
  }
  const records = [...byDoc.values()].flat(); const ts = runs.map((r) => r.ts).sort();
  notes.push(`pending-permit feed (INCREMENTAL: each run lists only newly submitted permits): ${runs.length} runs (${runs.length ? `${iso(ts[0])} .. ${iso(ts[ts.length - 1])}` : 'none'}, timezone UNVERIFIED), ${emptyRuns} with no permits; ${permitRows} permit rows, ${wellboreRows} wellbore rows -> ${records.length} records over ${byDoc.size} permits; ${noWellbore} permits with no wellbore row (flagged NO_WELLBORE); ${nonDecimal} wellbores without usable lat/long (state-plane or empty; geometry NULL); ${unverifiedCounty} wellbores outside the configured counties (county written as UNVERIFIED code); ${dupDocs} documents repeated across runs; header widths seen: ${[...widths].join(',')}`);
  if (rejects.length) notes.push(`${rejects.length} rows had a value count different from the header and were NOT loaded: ${rejects.slice(0, 3).join(' | ')}`);
  const layer: LayerPull = { layer: 'permit_pending', url: PENDING_SHARE_URL, entity_type: 'permit', skipped_unchanged: false, source_updated_at: runs.length ? iso(ts[ts.length - 1]) : null, records, unmapped: [], fields_discovered: [] };
  return { source: 'RRC', state: rejects.length || !runs.length ? 'UNVERIFIED' : 'OK', layers: [layer], notes };
}
