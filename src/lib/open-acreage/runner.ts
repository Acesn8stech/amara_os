import { randomUUID } from 'node:crypto';
import { buildCandidates } from './candidates.ts';
import type { ConnectorResult } from './connectors.ts';
import type { OpenAcreageConfig } from './config.ts';
import { CONSTITUTION_VERSION } from './constitution.ts';
import { diffRecords } from './diff.ts';
import type { Store } from './store.ts';
import { validateBatch } from './validate.ts';
import type { LeaseRecord, PermitRecord, ProductionRecord, RunStatus, Section, SyncRun, WellRecord, BaseRecord } from './types.ts';

/** Run one source: gate each layer, promote, diff. One failed layer never blocks the others (=> PARTIAL). */
export async function runSource(source: 'UL' | 'BLM' | 'GLO' | 'RRC', pull: () => Promise<ConnectorResult>, store: Store, cfg: OpenAcreageConfig, now: string): Promise<SyncRun> {
  const run: SyncRun = { run_id: randomUUID(), source, started_at: now, finished_at: null, status: 'FAILED', records_in: 0, records_valid: 0, records_rejected: 0,
    source_updated_at: null, error_log: null, constitution_version: CONSTITUTION_VERSION, source_state: 'OK', notes: [] };
  if (cfg.killSwitchAll || !cfg.sources[source].enabled) {
    run.finished_at = now; run.source_state = 'DISABLED'; run.status = 'PARTIAL'; run.notes.push('kill switch engaged'); await store.saveRun(run); return run;
  }
  await store.saveRun(run);
  const errors: string[] = []; let bad = 0;
  try {
    const res = await pull();
    run.source_state = res.state; run.notes.push(...res.notes);
    for (const L of res.layers) {
      if (L.skipped_unchanged) { run.source_updated_at = L.source_updated_at ?? run.source_updated_at; continue; }
      run.records_in += L.records.length;
      const prior = await store.getPromoted(source, L.layer);
      const gate = validateBatch(L.records, prior.length ? prior.length : null, cfg);
      run.records_rejected += gate.rejected.length;
      if (gate.rejected.length) await store.saveRejected(gate.rejected, run.run_id);
      if (gate.halted) { bad++; errors.push(`${L.layer}: ${gate.haltReason}`); continue; }
      run.records_valid += gate.valid.length;
      await store.promoteLayer(source, L.layer, L.entity_type, gate.valid, run.run_id);
      await store.saveChanges(diffRecords(prior, gate.valid, run.run_id, now));
      await store.setLayerEdit(L.url, L.source_updated_at);
      run.source_updated_at = L.source_updated_at ?? run.source_updated_at;
      if (L.unmapped.length) run.notes.push(`${L.layer}: UNMAPPED canonical fields (stored NULL): ${L.unmapped.join(', ')}`);
    }
    // Count-drop halt => FAILED (spec §6). Otherwise OK only if the connector was fully OK.
    run.status = bad > 0 ? 'FAILED' : res.state === 'OK' ? 'OK' : res.layers.length || res.state === 'NOT FOUND' ? 'PARTIAL' : 'FAILED';
  } catch (e) { errors.push((e as Error).message); run.status = 'FAILED'; run.source_state = 'UNREACHABLE'; }
  run.error_log = errors.length ? errors.join('\n') : null;
  run.finished_at = new Date().toISOString(); // real completion time (staleness and duration depend on it)
  await store.saveRun(run);
  return run;
}

export async function rebuildCandidates(store: Store, cfg: OpenAcreageConfig, now: string) {
  const [sections, leases, permits, wells, production, parcels, runs] = await Promise.all([
    store.getAll('section_grid'), store.getAll('lease'), store.getAll('permit'), store.getAll('well'), store.getAll('production_monthly'), store.getAll('available_parcel'), store.getRuns()]);
  const c = buildCandidates({ sections: sections as Section[], leases: leases as LeaseRecord[], permits: permits as PermitRecord[], wells: wells as WellRecord[],
    production: production as ProductionRecord[], parcels: parcels as BaseRecord[], runs }, cfg, now);
  await store.replaceCandidates(c);
  return c;
}

export type { RunStatus };
