import { CANDIDATE_STATUS, CONSTITUTION_VERSION } from './constitution.ts';
import { expiringLeases } from './candidates.ts';
import type { OpenAcreageConfig } from './config.ts';
import type { BaseRecord, Candidate, Geometry, LeaseRecord, Section, WellRecord } from './types.ts';

export interface Feature { type: 'Feature'; geometry: Geometry | null; properties: Record<string, unknown> }
export interface FC { type: 'FeatureCollection'; features: Feature[]; constitution_version: string }

/** Popup contract (§7): source, source_updated_at, retrieved_at, verification status, constitution version. Missing => explicit "UNKNOWN". */
function popup(r: BaseRecord, label: string, verification: string, extra: Record<string, unknown> = {}) {
  return { label, source: `${r.source_name} / ${r.source_layer}`, source_url: r.source_url, source_updated_at: r.source_updated_at ?? 'UNKNOWN',
    retrieved_at: r.retrieved_at ?? 'UNKNOWN', verification_status: verification, constitution_version: CONSTITUTION_VERSION, ...extra };
}
const fc = (features: Feature[]): FC => ({ type: 'FeatureCollection', features, constitution_version: CONSTITUTION_VERSION });

export interface LayerData { leases: LeaseRecord[]; wells: WellRecord[]; parcels: BaseRecord[]; sections: Section[]; candidates: Candidate[] }

export function buildLayers(d: LayerData, cfg: OpenAcreageConfig, now: string): Record<string, FC> {
  const exp = expiringLeases(d.leases, cfg, now);
  const expIds = new Set(exp.map((e) => e.lease));
  const leaseProps = (l: LeaseRecord, extra = {}) => popup(l, `LEASED — ${l.lessor_type}`, 'SOURCE-REPORTED (not title-verified)',
    { lessor_type: l.lessor_type, lease_no: l.lease_no, lessee: l.lessee, status: l.status_normalized, expiration_date: l.expiration_date, ...extra });
  const secById = new Map(d.sections.map((s) => [`${s.source_name}:${s.source_record_id}`, s]));
  return {
    LEASED: fc(d.leases.filter((l) => l.status_normalized === 'ACTIVE' && !expIds.has(l)).map((l) => ({ type: 'Feature', geometry: l.geom, properties: leaseProps(l) }))),
    EXPIRING_EXPIRED: fc(exp.map(({ lease, kind }) => ({ type: 'Feature', geometry: lease.geom, properties: leaseProps(lease, { kind, label: `${kind.replace('_', ' ')} — ${lease.lessor_type}` }) }))),
    NO_LEASE_CANDIDATES: fc(d.candidates.map((c) => {
      const s = secById.get(c.tract_id);
      return { type: 'Feature', geometry: s?.geom ?? null, properties: {
        label: c.status === CANDIDATE_STATUS ? CANDIDATE_STATUS : `HOLD — ${c.reason_codes.join(', ')}`, tract_id: c.tract_id, county: c.county, reason_codes: c.reason_codes,
        source: s ? `${s.source_name} / ${s.source_layer}` : 'UNKNOWN', source_updated_at: s?.source_updated_at ?? 'UNKNOWN', retrieved_at: s?.retrieved_at ?? 'UNKNOWN',
        verification_status: 'UNVERIFIED — courthouse title check required', constitution_version: c.constitution_version } };
    })),
    WELLS: fc(d.wells.map((w) => ({ type: 'Feature', geometry: w.geom, properties: popup(w, `WELL — ${w.status ?? 'status UNKNOWN'}`, 'SOURCE-REPORTED', { api_no: w.api_no, status: w.status }) }))),
    UL_AVAILABLE_PARCELS: fc(d.parcels.map((p) => ({ type: 'Feature', geometry: p.geom, properties: popup(p, 'UL AVAILABLE PARCEL', 'SOURCE-REPORTED (not title-verified)') }))),
  };
}
