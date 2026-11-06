import { inBBox, isValidGeometry } from './geometry.ts';
import type { OpenAcreageConfig } from './config.ts';
import type { BaseRecord, EntityType, Rejected } from './types.ts';

const GEOM_REQUIRED: EntityType[] = ['lease', 'well', 'available_parcel', 'section_grid'];
const ISO = /^\d{4}-\d{2}-\d{2}$/;

export interface GateResult {
  valid: BaseRecord[];
  rejected: Rejected[];
  halted: boolean;
  haltReason: string | null;
}

export function validateRecord(r: BaseRecord, cfg: Pick<OpenAcreageConfig, 'bbox'>): string[] {
  const why: string[] = [];
  if (!r.source_record_id) why.push('missing source_record_id');
  if (!r.source_url) why.push('missing source_url');
  if (!r.retrieved_at) why.push('missing retrieved_at');
  if (!r.source_name) why.push('missing source_name');
  if (!r.source_layer) why.push('missing source_layer');
  if (r.geom) {
    if (!isValidGeometry(r.geom)) why.push('invalid geometry');
    else if (!inBBox(r.geom, cfg.bbox)) why.push('geometry outside TX/NM bounding box');
  } else if (GEOM_REQUIRED.includes(r.entity_type)) why.push('missing geometry');
  for (const k of ['effective_date', 'expiration_date', 'issued_date', 'spud_date', 'completion_date', 'plug_date', 'month']) {
    const v = r[k];
    if (v !== null && v !== undefined && !(typeof v === 'string' && ISO.test(v) && !Number.isNaN(Date.parse(v)))) why.push(`unparseable date: ${k}`);
  }
  const eff = r.effective_date; const exp = r.expiration_date;
  if (typeof eff === 'string' && typeof exp === 'string' && ISO.test(eff) && ISO.test(exp) && exp < eff) why.push('expiration_date before effective_date');
  return why;
}

/**
 * Validation gate (Referee/Mirofish step). Nothing is promoted if the batch halts.
 * priorCount = number of promoted records for this layer from the last good run (null = first run).
 */
export function validateBatch(records: BaseRecord[], priorCount: number | null, cfg: OpenAcreageConfig): GateResult {
  const valid: BaseRecord[] = []; const rejected: Rejected[] = [];
  const seen = new Set<string>();
  for (const r of records) {
    const reasons = validateRecord(r, cfg);
    if (r.source_record_id) { if (seen.has(r.source_record_id)) reasons.push('duplicate source_record_id in batch'); else seen.add(r.source_record_id); }
    if (reasons.length) rejected.push({ entity_type: r.entity_type, record: r, reasons });
    else valid.push(r);
  }
  let halted = false; let haltReason: string | null = null;
  if (priorCount !== null && priorCount > 0) {
    const drop = (priorCount - valid.length) / priorCount;
    if (drop > cfg.maxDropRatio) {
      halted = true;
      haltReason = `record count dropped ${(drop * 100).toFixed(1)}% (${priorCount} -> ${valid.length}) > ${(cfg.maxDropRatio * 100).toFixed(0)}% — possible source outage, promote halted`;
    }
  }
  return { valid, rejected, halted, haltReason };
}
