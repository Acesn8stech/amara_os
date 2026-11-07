import { ALLOWED_CANDIDATE_STATUSES, CANDIDATE_STATUS, CONSTITUTION_VERSION, type CandidateStatus } from './constitution.ts';
import { api8 } from './pdq.ts';
import { centroid, haversineMiles, intersects, pointNearGeometry } from './geometry.ts';
import type { OpenAcreageConfig } from './config.ts';
import type { Candidate, LeaseRecord, PermitRecord, ProductionRecord, Section, SyncRun, WellRecord, BaseRecord } from './types.ts';

export interface CandidateInputs {
  sections: Section[];
  leases: LeaseRecord[];
  permits: PermitRecord[];
  wells: WellRecord[];
  production: ProductionRecord[];
  parcels: BaseRecord[];
  runs: SyncRun[];
}

/** Sources whose data the candidate decision depends on. GLO is stubbed (NOT FOUND) and reported, not required. */
export const REQUIRED_SOURCES = ['UL', 'BLM', 'RRC'] as const;

const norm = (c: string | null | undefined) => (c ?? '').toLowerCase().replace(/\s+county$/, '').trim();
const num = (v: unknown) => String(v ?? '').trim().replace(/^0+(?=\d)/, '').toUpperCase();
const isUlSurvey = (s: string) => /^(UL\b|UNIVERSITY)/i.test(s.trim());
const addMonths = (iso: string, m: number) => { const d = new Date(iso); d.setUTCMonth(d.getUTCMonth() - m); return d.toISOString().slice(0, 10); };
const addDays = (iso: string, n: number) => new Date(Date.parse(iso) + n * 86_400_000).toISOString().slice(0, 10);

export function staleSources(runs: SyncRun[], cfg: OpenAcreageConfig, nowIso: string): string[] {
  const stale: string[] = [];
  for (const s of REQUIRED_SOURCES) {
    const cadenceMs = cfg.sources[s].cadenceHours * 3_600_000 * 2;
    const good = runs.filter((r) => r.source === s && r.status === 'OK' && r.finished_at).map((r) => Date.parse(r.finished_at!));
    const last = good.length ? Math.max(...good) : null;
    if (last === null || Date.parse(nowIso) - last > cadenceMs) stale.push(s);
  }
  return stale;
}

/** Defense in depth for Constitution Control 9. */
export function assertStatus(s: string): CandidateStatus {
  if (!(ALLOWED_CANDIDATE_STATUSES as readonly string[]).includes(s)) throw new Error(`Illegal candidate status "${s}" — pipeline may not exceed "${CANDIDATE_STATUS}"`);
  return s as CandidateStatus;
}

/** Block and section of a UL section: the derived fields if present, else the source attributes (sections loaded before those fields existed only have raw). */
export const sectionBlock = (sec: { block_no?: unknown; raw?: Record<string, unknown> }) => sec.block_no ?? sec.raw?.blocknum;
export const sectionNumber = (sec: { section_no?: unknown; raw?: Record<string, unknown> }) => sec.section_no ?? sec.raw?.sectionnum;
export const normCounty = norm;
export const normNum = num;
export const ulSurvey = isUlSurvey;

/** Does a permit touch this section? spatial: its coordinate is inside or within the NAD27 buffer; text: a University Lands survey with the section's county, block and section. */
export function permitHits(p: PermitRecord, sec: Section, cfg: Pick<OpenAcreageConfig, 'permitBufferMiles'>): { spatial: boolean; text: boolean } {
  const spatial = !!p.geom && !!sec.geom && pointNearGeometry([p.geom.coordinates[0] as number, p.geom.coordinates[1] as number], sec.geom, cfg.permitBufferMiles);
  const locs = (p.locations as Array<{ county: string | null; section: string; block: string; survey: string }> | undefined) ?? [];
  const sb = num(sectionBlock(sec)); const ss = num(sectionNumber(sec));
  const text = sb !== '' && ss !== '' && locs.some((l) => isUlSurvey(l.survey) && norm(l.county ?? p.county) === norm(sec.county) && num(l.block) === sb && num(l.section) === ss);
  return { spatial, text };
}

export function buildCandidates(inp: CandidateInputs, cfg: OpenAcreageConfig, nowIso: string): Candidate[] {
  const targets = new Set(cfg.targetCounties.map(norm));
  if (!targets.size) return []; // Scott sets the list; no list => no candidates (no guessing)
  const today = nowIso.slice(0, 10);
  const permitCut = addMonths(today, cfg.permitMonths); const prodCut = addMonths(today, cfg.productionMonths).slice(0, 7) + '-01';
  const stale = staleSources(inp.runs, cfg, nowIso);

  const producingIds = new Set<string>();
  for (const p of inp.production) {
    if (!(p.month && p.month >= prodCut && ((p.oil_bbl ?? 0) > 0 || (p.gas_mcf ?? 0) > 0 || (Number(p.cond_bbl) || 0) > 0 || (Number(p.csgd_mcf) || 0) > 0))) continue;
    if (p.well_or_lease_id) producingIds.add(p.well_or_lease_id);
    for (const a of (p.api8_list as string[] | undefined) ?? []) producingIds.add(a);
  }
  const wellProducing = (w: WellRecord) => { const a = api8(w.api_no); return (a !== null && producingIds.has(a)) || (!!w.api_no && producingIds.has(w.api_no)) || (!!w.source_record_id && producingIds.has(w.source_record_id)); };
  // Wells RRC ties to a lease (any month, any volume) and the statuses UL reports per API (the two UL well layers can differ).
  const linkedApis = new Set<string>(); for (const p of inp.production) for (const a of (p.api8_list as string[] | undefined) ?? []) linkedApis.add(a);
  const okStatus = new Set(cfg.unlinkedWellOkStatuses.map((x) => x.toUpperCase()));
  const statusesByApi = new Map<string, Set<string>>();
  for (const w of inp.wells) { const a = api8(w.api_no); if (!a || !w.status) continue; (statusesByApi.get(a) ?? statusesByApi.set(a, new Set()).get(a)!).add(String(w.status).toUpperCase()); }
  const verifiable = (w: WellRecord) => {
    const a = api8(w.api_no); if (!a) return false; // no usable API: cannot be checked
    if (linkedApis.has(a)) return true;
    const st = statusesByApi.get(a); return !!st && st.size > 0 && [...st].every((x) => okStatus.has(x));
  };
  // University Lands permits in the window that cannot be placed by block+section (blank block or section, no coordinates): they may sit on a tract we cannot see.
  const activityOf = (p: PermitRecord) => (typeof p.activity_date === 'string' ? p.activity_date : p.issued_date);
  const unplacedCounties = new Set<string>();
  for (const p of inp.permits) {
    const d = activityOf(p); if (!d || d < permitCut || p.geom) continue;
    for (const l of (p.locations as Array<{ county: string | null; section: string; block: string; survey: string }> | undefined) ?? [])
      if (isUlSurvey(l.survey) && (num(l.block) === '' || num(l.section) === '')) unplacedCounties.add(norm(l.county ?? p.county));
  }
  const out: Candidate[] = [];

  for (const sec of inp.sections) {
    if (!sec.geom || !targets.has(norm(sec.county))) continue;
    const g = sec.geom;
    const hits = inp.leases.filter((l) => l.geom && intersects(g, l.geom));
    if (hits.some((l) => l.status_normalized === 'ACTIVE')) continue; // (1)
    const hit = (p: PermitRecord) => { const h = permitHits(p, sec, cfg); return h.spatial || h.text; };
    const activity = (p: PermitRecord) => (typeof p.activity_date === 'string' ? p.activity_date : p.issued_date); // latest of issued / amended / extended / spud
    const recentPermits = inp.permits.filter((p) => { const d = activity(p); return !!d && d >= permitCut && hit(p); });
    if (recentPermits.length) continue; // (2)
    const inside = inp.wells.filter((w) => w.geom && intersects(g, w.geom));
    if (inside.some(wellProducing)) continue; // (3)

    const reasons: string[] = []; const evidence: Candidate['evidence_refs'] = [];
    const ref = (r: BaseRecord) => evidence.push({ entity_type: r.entity_type, source_name: r.source_name, source_record_id: r.source_record_id as string });
    ref(sec);
    if (inp.parcels.some((p) => p.geom && intersects(g, p.geom))) { reasons.push('UL_AVAILABLE_PARCEL'); inp.parcels.filter((p) => p.geom && intersects(g, p.geom)).forEach(ref); }
    for (const l of hits) {
      ref(l);
      if (l.source_name === 'GLO' && l.status_normalized === 'INACTIVE') reasons.push('GLO_INACTIVE_LEASE');
      if (l.status_normalized === 'EXPIRED' && l.expiration_date && l.expiration_date <= today && l.expiration_date >= addDays(today, -cfg.recentExpiredDays)) reasons.push('LEASE_EXPIRED_RECENT');
      if (l.status_normalized === 'UNKNOWN') reasons.push('LEASE_STATUS_UNKNOWN_OVERLAP');
    }
    const c = centroid(g);
    if (c) {
      const near = inp.wells.filter((w) => w.geom && wellProducing(w) && (() => { const wc = centroid(w.geom!); return wc && haversineMiles(c, wc) <= cfg.offsetMiles; })());
      if (near.length) { reasons.push('OFFSET_PRODUCTION'); near.forEach(ref); }
    }
    if (inp.permits.some((p) => !p.geom && !((p.locations as unknown[] | undefined)?.length) && norm(p.county) === norm(sec.county))) reasons.push('PERMITS_WITHOUT_GEOMETRY');
    if (unplacedCounties.has(norm(sec.county))) reasons.push('UL_PERMIT_UNPLACED_IN_COUNTY'); // informational: a UL permit here could not be placed on any section
    reasons.push('GLO_NOT_CONNECTED'); // coverage gap, informational — status unchanged
    // A permit SUBMITTED inside the window (RRC pending feed; approval is not visible there) means drilling is intended: visible HOLD, not a clean candidate.
    const pending = inp.permits.filter((p) => p.permit_status === 'SUBMITTED' && typeof p.submit_date === 'string' && p.submit_date >= permitCut && permitHits(p, sec, cfg).spatial);
    if (pending.length) { reasons.push('PERMIT_SUBMITTED_RECENT'); pending.forEach(ref); }
    const unverifiable = inside.filter((w) => !verifiable(w));
    if (unverifiable.length) { reasons.push('WELL_NO_PRODUCTION_LINK'); unverifiable.forEach(ref); }
    let status: CandidateStatus = CANDIDATE_STATUS;
    if (unverifiable.length) { status = 'HOLD'; reasons.push('HOLD:UNVERIFIABLE_WELL'); }
    if (pending.length) { status = 'HOLD'; reasons.push('HOLD:PERMIT_SUBMITTED'); }
    // A lease we cannot classify may be active. Never present such a tract as a clean candidate (Celilo / UNVERIFIED over approximation).
    if (reasons.includes('LEASE_STATUS_UNKNOWN_OVERLAP')) { status = 'HOLD'; reasons.push('HOLD:UNKNOWN_LEASE_STATUS'); }
    if (stale.length) { status = 'HOLD'; reasons.push('STALE_SOURCE', ...stale.map((s) => `STALE:${s}`)); }
    out.push({ tract_id: `${sec.source_name}:${sec.source_record_id}`, county: sec.county, reason_codes: [...new Set(reasons)], evidence_refs: evidence,
      status: assertStatus(status), constitution_version: CONSTITUTION_VERSION, created_at: nowIso });
  }
  return out;
}

export function expiringLeases(leases: LeaseRecord[], cfg: OpenAcreageConfig, nowIso: string) {
  const today = nowIso.slice(0, 10); const horizon = addDays(today, cfg.expiringDays);
  return leases.filter((l) => l.status_normalized === 'EXPIRED' || l.status_normalized === 'INACTIVE'
    || (l.status_normalized === 'ACTIVE' && !!l.expiration_date && l.expiration_date >= today && l.expiration_date <= horizon))
    .map((l) => ({ lease: l, kind: l.status_normalized === 'ACTIVE' ? 'EXPIRING_SOON' as const : l.status_normalized }));
}
