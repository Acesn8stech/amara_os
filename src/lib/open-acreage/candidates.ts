import { ALLOWED_CANDIDATE_STATUSES, CANDIDATE_STATUS, CONSTITUTION_VERSION, type CandidateStatus } from './constitution.ts';
import { api8 } from './pdq.ts';
import { centroid, haversineMiles, intersects, pointInGeometry } from './geometry.ts';
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

export function buildCandidates(inp: CandidateInputs, cfg: OpenAcreageConfig, nowIso: string): Candidate[] {
  const targets = new Set(cfg.targetCounties.map(norm));
  if (!targets.size) return []; // Scott sets the list; no list => no candidates (no guessing)
  const today = nowIso.slice(0, 10);
  const permitCut = addMonths(today, cfg.permitMonths); const prodCut = addMonths(today, cfg.productionMonths).slice(0, 7) + '-01';
  const stale = staleSources(inp.runs, cfg, nowIso);

  const producingIds = new Set(inp.production.filter((p) => p.month && p.month >= prodCut && ((p.oil_bbl ?? 0) > 0 || (p.gas_mcf ?? 0) > 0 || (Number(p.cond_bbl) || 0) > 0 || (Number(p.csgd_mcf) || 0) > 0)).map((p) => p.well_or_lease_id));
  const wellProducing = (w: WellRecord) => { const a = api8(w.api_no); return (a !== null && producingIds.has(a)) || (!!w.api_no && producingIds.has(w.api_no)) || producingIds.has(w.source_record_id); };
  const out: Candidate[] = [];

  for (const sec of inp.sections) {
    if (!sec.geom || !targets.has(norm(sec.county))) continue;
    const g = sec.geom;
    const hits = inp.leases.filter((l) => l.geom && intersects(g, l.geom));
    if (hits.some((l) => l.status_normalized === 'ACTIVE')) continue; // (1)
    const recentPermits = inp.permits.filter((p) => p.geom && p.issued_date && p.issued_date >= permitCut && pointInGeometry([p.geom.coordinates[0] as number, p.geom.coordinates[1] as number], g));
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
    if (inp.permits.some((p) => !p.geom && norm(p.county) === norm(sec.county))) reasons.push('PERMITS_WITHOUT_GEOMETRY');
    reasons.push('GLO_NOT_CONNECTED'); // coverage gap, informational — status unchanged
    let status: CandidateStatus = CANDIDATE_STATUS;
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
