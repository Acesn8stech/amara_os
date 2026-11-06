import type { ConnectorResult, LayerPull } from './connectors.ts';
import { parseDate } from './normalize.ts';
import type { BaseRecord } from './types.ts';

/**
 * RRC "Drilling Permits Pending Approval" (per AMARA-RRC-PERMIT-LANE-SPEC, observed 2026-11-05): files dp_<table>_pending_<YYYYMMDDHHMMSS>.txt,
 * "}"-delimited, values double-quoted, header row may wrap across lines (joined up to the first line that starts with a quote),
 * tables join on UNIVERSAL_DOC_NO. Read by COLUMN NAME; refuse on a missing column; keep every row (mismatched rows are reported, not dropped).
 * A pending permit is not an issued permit: it never counts toward the "issued permit" rule and does not make RRC a good sync.
 */
export const PENDING_SHARE_URL = 'https://mft.rrc.texas.gov/link/0ad92a65-4212-49a1-98a7-d667a55fb497';
const PERMIT_COLS = ['UNIVERSAL_DOC_NO', 'OPERATOR_NAME', 'OPERATOR_NUMBER', 'SUBMIT_DATE'];
const WELLBORE_COLS = ['UNIVERSAL_DOC_NO', 'COUNTY_CODE', 'LAT_DEGREES', 'LONG_DEGREES', 'LATLONG_TYPE_CODE'];

// FIPS codes of the configured target counties only (public standard). Any other code is written as "UNVERIFIED code NNN", never guessed.
export const PENDING_COUNTY: Record<string, string> = { '003': 'ANDREWS', '135': 'ECTOR', '173': 'GLASSCOCK', '317': 'MARTIN', '329': 'MIDLAND', '383': 'REAGAN', '461': 'UPTON' };

export function parseRrcDsv(text: string): { header: string[]; rows: Array<Record<string, string>>; rejects: string[] } {
  const lines = text.split(/\r?\n/);
  const k = lines.findIndex((l) => l.trim().startsWith('"'));
  if (k < 1) throw new Error('no header found before the first quoted data line');
  const clean = (v: string) => v.trim().replace(/^"|"$/g, '').trim();
  const header = lines.slice(0, k).join('').split('}').map((h) => clean(h).toUpperCase());
  const rows: Array<Record<string, string>> = []; const rejects: string[] = [];
  for (const l of lines.slice(k)) {
    if (!l.trim()) continue;
    const vals = l.split('}').map(clean);
    if (vals.length !== header.length) { rejects.push(l); continue; }
    rows.push(Object.fromEntries(header.map((h, i) => [h, vals[i]])));
  }
  return { header, rows, rejects };
}

/** Newest run (timestamp) that has BOTH tables, from a list of file names. */
export function pickLatestPending(names: string[]): { ts: string; permitFile: string; wellboreFile: string } | null {
  const by = new Map<string, { permit?: string; wellbore?: string }>();
  for (const n of names) {
    const m = /^dp_(drilling_permit|wellbore)_pending_(\d{14})\.txt$/.exec(n); if (!m) continue;
    const e = by.get(m[2]) ?? {}; if (m[1] === 'drilling_permit') e.permit = n; else e.wellbore = n; by.set(m[2], e);
  }
  const ts = [...by.entries()].filter(([, e]) => e.permit && e.wellbore).map(([t]) => t).sort().pop();
  return ts ? { ts, permitFile: by.get(ts)!.permit!, wellboreFile: by.get(ts)!.wellbore! } : null;
}

const needCols = (header: string[], cols: string[], what: string) => {
  const miss = cols.filter((c) => !header.includes(c));
  if (miss.length) throw new Error(`${what}: header is missing required columns ${miss.join(', ')} — refusing to read`);
};

export function pullPending(inp: { permitText: string; wellboreText: string; ts: string; permitFile: string; wellboreFile: string }, now: string): ConnectorResult {
  const notes: string[] = [];
  const p = parseRrcDsv(inp.permitText); const w = parseRrcDsv(inp.wellboreText);
  needCols(p.header, PERMIT_COLS, inp.permitFile); needCols(w.header, WELLBORE_COLS, inp.wellboreFile);
  const rejects = [...p.rejects.map((l) => `${inp.permitFile}: ${l.slice(0, 120)}`), ...w.rejects.map((l) => `${inp.wellboreFile}: ${l.slice(0, 120)}`)];
  const t = inp.ts; const runIso = `${t.slice(0, 4)}-${t.slice(4, 6)}-${t.slice(6, 8)}T${t.slice(8, 10)}:${t.slice(10, 12)}:${t.slice(12, 14)}`; // timezone UNVERIFIED (no zone suffix)
  const wbByDoc = new Map<string, Array<Record<string, string>>>();
  for (const r of w.rows) (wbByDoc.get(r.UNIVERSAL_DOC_NO) ?? wbByDoc.set(r.UNIVERSAL_DOC_NO, []).get(r.UNIVERSAL_DOC_NO)!).push(r);
  const records: BaseRecord[] = []; let noWellbore = 0; let nonDecimal = 0; let unverifiedCounty = 0;
  const base = { entity_type: 'permit' as const, source_name: 'RRC', source_url: PENDING_SHARE_URL, source_layer: 'permit_pending', source_updated_at: runIso, retrieved_at: now };
  for (const pr of p.rows) {
    const doc = pr.UNIVERSAL_DOC_NO; const wbs = wbByDoc.get(doc) ?? [];
    const common = { permit_no: null, permit_status: 'PENDING', issued_date: null, universal_doc_no: doc, operator_name: pr.OPERATOR_NAME || null, operator_no: pr.OPERATOR_NUMBER || null,
      submit_date: parseDate(pr.SUBMIT_DATE), lease_name: pr.DEFAULT_LEASE_NAME || null, district: pr.DISTRICT || null };
    if (!wbs.length) { noWellbore++; records.push({ ...base, ...common, source_record_id: `${doc}:0`, county: null, geom: null, flags: ['NO_WELLBORE'], raw: { permit: pr, file: inp.permitFile } } as BaseRecord); continue; }
    wbs.forEach((wb, i) => {
      const decimal = wb.LATLONG_TYPE_CODE === '1'; const lat = Number(wb.LAT_DEGREES); const lon = Number(wb.LONG_DEGREES);
      const usable = decimal && Number.isFinite(lat) && Number.isFinite(lon) && lat !== 0 && lon !== 0; // no sign flip, no guess: the bbox gate judges the values as given
      if (!usable) nonDecimal++;
      const county = PENDING_COUNTY[wb.COUNTY_CODE] ?? `UNVERIFIED code ${wb.COUNTY_CODE}`; if (!PENDING_COUNTY[wb.COUNTY_CODE]) unverifiedCounty++;
      records.push({ ...base, ...common, source_record_id: `${doc}:${i}`, county, county_code: wb.COUNTY_CODE, nearest_town: wb.NEAREST_TOWN || null,
        geom: usable ? { type: 'Point', coordinates: [lon, lat] } : null, raw: { permit: pr, wellbore: wb, files: [inp.permitFile, inp.wellboreFile] } } as BaseRecord);
    });
  }
  notes.push(`pending permits: run ${runIso} (timezone UNVERIFIED): ${p.rows.length} permits, ${w.rows.length} wellbore rows -> ${records.length} records; ${noWellbore} permits with no wellbore row (flagged NO_WELLBORE); ${nonDecimal} wellbores without usable decimal coordinates (geometry NULL); ${unverifiedCounty} with county code outside the configured counties (written as UNVERIFIED code)`);
  if (rejects.length) notes.push(`${rejects.length} rows had a value count different from the header and were NOT loaded: ${rejects.slice(0, 3).join(' | ')}`);
  const layer: LayerPull = { layer: 'permit_pending', url: PENDING_SHARE_URL, entity_type: 'permit', skipped_unchanged: false, source_updated_at: runIso, records, unmapped: [], fields_discovered: [...new Set([...p.header, ...w.header])] };
  return { source: 'RRC', state: rejects.length ? 'UNVERIFIED' : 'OK', layers: [layer], notes };
}
