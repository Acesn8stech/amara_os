import type { ConnectorResult, LayerPull } from './connectors.ts';
import { PENDING_COUNTY } from './pending.ts';
import type { BaseRecord } from './types.ts';

/**
 * RRC "Drilling Permit Master and Trailer — Daily / End-of-Month File (includes latitudes and longitudes)", manual OGA049M, read 2026-11-06.
 * ASCII, CRLF lines, variable-length segments; the first two bytes are the segment id. Segments of one permit follow its root (01):
 *   01 root (212 chars): status no 3-9, sequence 10-11, county 12-14, lease 15-46, district 47-48, operator no 49-54, date received 59-66,
 *      operator name 67-98, STATUS-OF-APP flag 101 (A approved, P pending, W withdrawn, D dismissed, E denied, C closed, O other, X deleted, Z cancelled),
 *      permit no 113-119, ISSUE DATE 120-127 (YYYYMMDD), withdrawn date 128-135, well no 157-162.   [positions verified against 1,288 real roots]
 *   02 permit master (510), dates (manual position minus 2, verified): received 122-129, issued 130-137 (== root issue on all 1,130 real permits), amended 138-145,
 *      extended 146-153, spud 154-161, expires 179-186. A permit amended/extended/spudded recently is ACTIVE even if first issued long ago
 *      (26 of 1,130 October permits were issued before 2025 but amended in 2026), so rule 2 uses the latest of these as the permit's activity date.
 *      SURFACE location text — section 244-251, block 252-261, survey 262-316, abstract 317-322. The manual's positions for this
 *      segment are 2 too high (it lists the record id twice); the values here were verified on real Andrews/Reagan/Upton permits.
 *   05 field bottom-hole (262, recurring): section 3-10, block 11-20, abstract 21-26, survey 27-81, county name 202-214.
 *   14 / 15 GIS surface / bottom-hole coordinates "14: -95.4315052  32.0453048" (longitude then latitude), horizontally NAD27 per the manual.
 * Observed: only SOME counties carry coordinates (Oct 2026: Andrews, Ector, Glasscock yes; Martin, Midland, Reagan, Upton none), so the location
 * TEXT (county + block + section + survey) is kept for matching to UL sections. Every record in these files is flag A in the samples seen.
 */
export const MASTER_DAILY_URL = 'https://mft.rrc.texas.gov/link/5f07cc72-2e79-4df8-ade1-9aeb792e03fc';
export const MASTER_EOM_URL = 'https://mft.rrc.texas.gov/link/f5dfea9c-bb39-4a5e-a44e-fb522e088cba';

const FLAGS: Record<string, string> = { A: 'APPROVED', P: 'PENDING', W: 'WITHDRAWN', D: 'DISMISSED', E: 'DENIED', C: 'CLOSED', O: 'OTHER', X: 'DELETED', Z: 'CANCELLED' };

export interface PermitLocation { kind: 'surface' | 'bhl'; county: string | null; section: string; block: string; survey: string; abstract: string }
export interface MasterRoot {
  status_no: string; seq: string; county_code: string; lease: string; district: string; operator_no: string; operator_name: string; received: string | null;
  flag: string; permit_no: string | null; issued: string | null; withdrawn: string | null; well_no: string; lon: number | null; lat: number | null;
  locations: PermitLocation[]; bhl_lon: number | null; bhl_lat: number | null;
  amended: string | null; extended: string | null; spud: string | null; expires: string | null;
}

const ymd = (s: string): string | null => {
  if (!/^\d{8}$/.test(s) || s === '00000000') return null;
  const iso = `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`; const d = new Date(`${iso}T00:00:00Z`);
  return Number.isNaN(d.getTime()) || d.toISOString().slice(0, 10) !== iso ? null : iso;
};

export function parseMasterText(text: string): { roots: MasterRoot[]; shortRoots: number; unknownLines: number } {
  const roots: MasterRoot[] = []; let cur: MasterRoot | null = null; let shortRoots = 0; let unknownLines = 0;
  for (const line of text.split(/\r?\n/)) {
    if (!line) continue;
    const id = line.slice(0, 2);
    if (id === '01') {
      if (line.length < 162) { shortRoots++; cur = null; continue; }
      const permit = line.slice(112, 119).trim();
      cur = { status_no: line.slice(2, 9), seq: line.slice(9, 11), county_code: line.slice(11, 14), lease: line.slice(14, 46).trim(), district: line.slice(46, 48), operator_no: line.slice(48, 54),
        operator_name: line.slice(66, 98).trim(), received: ymd(line.slice(58, 66)), flag: line.slice(100, 101), permit_no: /^0*$/.test(permit) ? null : permit, issued: ymd(line.slice(119, 127)),
        withdrawn: ymd(line.slice(127, 135)), well_no: line.slice(156, 162).trim(), lon: null, lat: null, locations: [], bhl_lon: null, bhl_lat: null, amended: null, extended: null, spud: null, expires: null };
      roots.push(cur);
    } else if (!cur) { unknownLines++; }
    else if (id === '02' && line.length >= 322) {
      cur.amended = ymd(line.slice(137, 145)); cur.extended = ymd(line.slice(145, 153)); cur.spud = ymd(line.slice(153, 161)); cur.expires = ymd(line.slice(178, 186));
      cur.locations.push({ kind: 'surface', county: null, section: line.slice(243, 251).trim(), block: line.slice(251, 261).trim(), survey: line.slice(261, 316).trim(), abstract: line.slice(316, 322).trim() });
    } else if (id === '05' && line.length >= 214) {
      cur.locations.push({ kind: 'bhl', county: line.slice(201, 214).trim() || null, section: line.slice(2, 10).trim(), block: line.slice(10, 20).trim(), survey: line.slice(26, 81).trim(), abstract: line.slice(20, 26).trim() });
    } else if (id === '14' || id === '15') {
      const m = /^1[45]:\s*(-?\d+\.\d+)\s+(-?\d+\.\d+)/.exec(line);
      if (m) { if (id === '14') { cur.lon = Number(m[1]); cur.lat = Number(m[2]); } else { cur.bhl_lon = Number(m[1]); cur.bhl_lat = Number(m[2]); } }
    } else if (!/^(03|04|06|07|08|09|10|11|12|13)$/.test(id)) unknownLines++;
  }
  return { roots, shortRoots, unknownLines };
}

/** 'daf420.dat.10-31-2026' -> '2026-10'; the undated daily file takes the month of `asOfIso` (the day it was downloaded). */
export function masterFileMonth(name: string, asOfIso: string): string | null {
  const m = /\.(\d{2})-(\d{2})-(\d{4})$/.exec(name);
  if (m) return `${m[3]}-${m[1]}`;
  return /^daf420\.dat$/.test(name) ? asOfIso.slice(0, 7) : null;
}

/** Freshness of a file: a month-end file is as of that date (from its name); the daily file is as of the day it was downloaded. */
export function masterFileAsOf(name: string, asOfIso: string): string {
  const m = /\.(\d{2})-(\d{2})-(\d{4})$/.exec(name);
  return m ? `${m[3]}-${m[1]}-${m[2]}T00:00:00.000Z` : asOfIso;
}

export interface MasterFile { name: string; text: string; asOfIso: string }

export function monthsBack(nowIso: string, n: number): string[] {
  const d = new Date(`${nowIso.slice(0, 7)}-01T00:00:00Z`); const out: string[] = [];
  for (let i = 0; i < n; i++) { out.push(d.toISOString().slice(0, 7)); d.setUTCMonth(d.getUTCMonth() - 1); }
  return out;
}

export function pullPermitMaster(files: MasterFile[], now: string, requiredMonths: number, acceptedGapMonths: string[] = []): ConnectorResult {
  const notes: string[] = []; const byKey = new Map<string, BaseRecord>(); const months = new Set<string>();
  let roots = 0; let withCoords = 0; let shortRoots = 0; let unknownLines = 0; let dup = 0; let notApproved = 0; let unverifiedCounty = 0;
  for (const f of [...files].sort((a, b) => (masterFileMonth(a.name, a.asOfIso) ?? '').localeCompare(masterFileMonth(b.name, b.asOfIso) ?? ''))) {
    const month = masterFileMonth(f.name, f.asOfIso); if (!month) { notes.push(`${f.name}: not a daf420 file name — skipped`); continue; }
    months.add(month);
    const p = parseMasterText(f.text); roots += p.roots.length; shortRoots += p.shortRoots; unknownLines += p.unknownLines;
    const url = f.name === 'daf420.dat' ? MASTER_DAILY_URL : MASTER_EOM_URL;
    for (const r of p.roots) {
      const key = `${r.status_no}-${r.seq}`; if (byKey.has(key)) dup++;
      if (r.flag !== 'A') notApproved++;
      const cname = PENDING_COUNTY[r.county_code]; if (!cname) unverifiedCounty++;
      if (r.lon !== null) withCoords++;
      byKey.set(key, { entity_type: 'permit', source_name: 'RRC', source_url: url, source_layer: 'permit', source_record_id: key, source_updated_at: masterFileAsOf(f.name, f.asOfIso), retrieved_at: now,
        permit_no: r.permit_no, permit_status: FLAGS[r.flag] ?? `UNVERIFIED flag ${r.flag}`, issued_date: r.issued, received_date: r.received, amended_date: r.amended, extended_date: r.extended, spud_date: r.spud, permit_expires: r.expires,
        activity_date: [r.issued, r.amended, r.extended, r.spud].filter((d): d is string => !!d).sort().pop() ?? null, universal_doc_no: null, operator_name: r.operator_name || null,
        operator_no: r.operator_no || null, lease_name: r.lease || null, district: r.district || null, county: cname ?? `UNVERIFIED code ${r.county_code}`, county_code: r.county_code, well_no: r.well_no || null,
        geom: r.lon !== null && r.lat !== null ? { type: 'Point', coordinates: [r.lon, r.lat] } : null, datum: r.lon !== null ? 'NAD27' : null, locations: r.locations,
        raw: { file: f.name, status_no: r.status_no, seq: r.seq, flag: r.flag, issued: r.issued, locations: r.locations } } as BaseRecord);
    }
  }
  const records = [...byKey.values()];
  const have = [...months].sort(); const need = monthsBack(now, requiredMonths); const allMissing = need.filter((m) => !months.has(m)).sort(); const accepted = allMissing.filter((m) => acceptedGapMonths.includes(m)); const missing = allMissing.filter((m) => !acceptedGapMonths.includes(m));
  notes.push(`permit master files: ${files.length} (${have.length ? `${have[0]} .. ${have[have.length - 1]}` : 'none'}); ${roots} roots -> ${records.length} permits (${dup} repeated across files, later file wins); ${withCoords} roots with GIS coordinates (NAD27); ${notApproved} not flagged approved; ${unverifiedCounty} outside the configured counties (county written as UNVERIFIED code); ${shortRoots} short root lines and ${unknownLines} unrecognised lines ignored`);
  if (accepted.length) notes.push(`permit coverage gap ACCEPTED BY OWNER for ${accepted.join(', ')} (OA_ALLOW_MISSING_PERMIT_MONTHS): permits issued in those months are invisible; every candidate carries PERMIT_MONTH_UNAVAILABLE`);
  if (missing.length) notes.push(`permit coverage INCOMPLETE: ${need.length - allMissing.length} of ${need.length} months present (missing: ${missing.slice(0, 6).join(', ')}${missing.length > 6 ? ', …' : ''}) — not a good sync; a recent permit in a missing month would be invisible`);
  const layer: LayerPull = { layer: 'permit', url: MASTER_EOM_URL, entity_type: 'permit', skipped_unchanged: false, source_updated_at: files.length ? files.map((f) => masterFileAsOf(f.name, f.asOfIso)).sort().pop()! : null, records, unmapped: [], fields_discovered: [] };
  return { source: 'RRC', state: missing.length || !files.length ? 'UNVERIFIED' : 'OK', layers: [layer], notes };
}
