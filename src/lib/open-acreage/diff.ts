import type { BaseRecord, ChangeEvent } from './types.ts';

const key = (r: BaseRecord) => `${r.source_name}\u0000${r.source_record_id}`;
const geomSig = (r: BaseRecord) => (r.geom ? JSON.stringify(r.geom) : null);

/** Diff prior promoted records vs the newly promoted full set for one layer. */
export function diffRecords(prior: BaseRecord[], next: BaseRecord[], runId: string, now: string): ChangeEvent[] {
  const out: ChangeEvent[] = [];
  const pm = new Map(prior.map((r) => [key(r), r])); const nm = new Map(next.map((r) => [key(r), r]));
  const ev = (r: BaseRecord, change_type: ChangeEvent['change_type'], old_value: unknown, new_value: unknown): ChangeEvent =>
    ({ entity_type: r.entity_type, entity_id: `${r.source_name}:${r.source_record_id}`, change_type, old_value, new_value, detected_at: now, sync_run_id: runId });
  for (const [k, n] of nm) {
    const p = pm.get(k);
    if (!p) { out.push(ev(n, 'NEW', null, { source_layer: n.source_layer })); continue; }
    const ps = (p.status_normalized ?? p.status) as unknown; const ns = (n.status_normalized ?? n.status) as unknown;
    if (ps !== ns) out.push(ev(n, 'STATUS_CHANGE', ps ?? null, ns ?? null));
    if (p.expiration_date !== n.expiration_date) out.push(ev(n, 'EXPIRATION_CHANGE', p.expiration_date ?? null, n.expiration_date ?? null));
    if (geomSig(p) !== geomSig(n)) out.push(ev(n, 'GEOMETRY_CHANGE', null, null));
  }
  for (const [k, p] of pm) if (!nm.has(k)) out.push(ev(p, 'REMOVED', { source_layer: p.source_layer }, null));
  return out;
}
