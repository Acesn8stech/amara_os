/* eslint-disable @typescript-eslint/no-explicit-any -- untyped external JSON payloads */
import type { Store } from './store.ts';
import type { BaseRecord, Candidate, ChangeEvent, EntityType, Rejected, SyncRun } from './types.ts';

/**
 * PostgREST-backed Store for schema god_mode_og. STATUS: written against migrations, NOT yet exercised
 * against a live project (no keys in build sandbox). Geometry is sent as GeoJSON; confirm PostGIS accepts it
 * on first live run (UNVERIFIED).
 */
export class SupabaseStore implements Store {
  private base: string; private key: string; private f: typeof fetch;
  constructor(url: string, serviceKey: string, f: typeof fetch = fetch) { this.f = f; this.base = `${url.replace(/\/$/, '')}/rest/v1`; this.key = serviceKey; }

  private async req(method: string, path: string, body?: unknown, extra: Record<string, string> = {}) {
    const res = await this.f(`${this.base}/${path}`, { method, body: body === undefined ? undefined : JSON.stringify(body), headers: {
      apikey: this.key, authorization: `Bearer ${this.key}`, 'content-type': 'application/json',
      'accept-profile': 'god_mode_og', 'content-profile': 'god_mode_og', ...extra } });
    if (!res.ok) throw new Error(`Supabase ${method} ${path} -> ${res.status} ${await res.text()}`);
    return res.status === 204 ? null : res.json();
  }
  private row(r: BaseRecord) { const rest: Record<string, unknown> = { ...r }; delete rest.entity_type; return rest; }
  private fromRow(entity: EntityType, r: Record<string, any>): BaseRecord { return { ...r, entity_type: entity } as BaseRecord; }

  async getPromoted(source: string, layer: string) {
    const out: BaseRecord[] = [];
    for (const e of ['lease', 'available_parcel', 'section_grid', 'well', 'permit', 'production_monthly'] as EntityType[]) {
      const rows = await this.req('GET', `${e}?source_name=eq.${encodeURIComponent(source)}&source_layer=eq.${encodeURIComponent(layer)}&select=*`);
      out.push(...(rows as any[]).map((r) => this.fromRow(e, r)));
    }
    return out;
  }
  async getAll(entity: EntityType) { return ((await this.req('GET', `${entity}?select=*`)) as any[]).map((r) => this.fromRow(entity, r)); }

  async promoteLayer(source: string, layer: string, entity: EntityType, records: BaseRecord[], runId: string) {
    // Upsert (archive-on-update trigger preserves prior versions in *_history).
    for (let i = 0; i < records.length; i += 500) {
      await this.req('POST', `${entity}?on_conflict=source_name,source_record_id`, records.slice(i, i + 500).map((r) => ({ ...this.row(r), sync_run_id: runId })),
        { prefer: 'resolution=merge-duplicates,return=minimal' });
    }
    // Records that disappeared from the source are removed from the live table (the delete trigger archives them).
    const keep = new Set(records.map((r) => r.source_record_id));
    const existing = (await this.req('GET', `${entity}?source_name=eq.${encodeURIComponent(source)}&source_layer=eq.${encodeURIComponent(layer)}&select=source_record_id`)) as Array<{ source_record_id: string }>;
    for (const gone of existing.filter((x) => !keep.has(x.source_record_id)))
      await this.req('DELETE', `${entity}?source_name=eq.${encodeURIComponent(source)}&source_record_id=eq.${encodeURIComponent(gone.source_record_id)}`);
  }
  async saveRejected(rejected: Rejected[], runId: string) {
    const by = new Map<EntityType, Rejected[]>(); rejected.forEach((r) => by.set(r.entity_type, [...(by.get(r.entity_type) ?? []), r]));
    for (const [e, rs] of by) await this.req('POST', `${e}_rejected`, rs.map((r) => ({ source_name: r.record.source_name ?? null, source_url: r.record.source_url ?? null, source_layer: r.record.source_layer ?? null,
      source_record_id: r.record.source_record_id ?? null, retrieved_at: r.record.retrieved_at ?? null, sync_run_id: runId, raw: r.record.raw ?? null, reasons: r.reasons })), { prefer: 'return=minimal' });
  }
  async saveRun(run: SyncRun) {
    const { run_id, ...rest } = run;
    await this.req('POST', 'sync_run?on_conflict=id', { id: run_id, ...rest }, { prefer: 'resolution=merge-duplicates,return=minimal' });
  }
  async getRuns() { return ((await this.req('GET', 'sync_run?select=*&order=started_at.asc')) as any[]).map(({ id, ...r }) => ({ run_id: id, ...r }) as SyncRun); }
  async saveChanges(c: ChangeEvent[]) { for (let i = 0; i < c.length; i += 500) await this.req('POST', 'change_event', c.slice(i, i + 500), { prefer: 'return=minimal' }); }
  async getChanges() { return (await this.req('GET', 'change_event?select=*')) as ChangeEvent[]; }
  async replaceCandidates(c: Candidate[]) {
    // Candidates are recomputed each run; DROP prior rows then insert. (title_check references candidate ids — see Phase 2 handoff.)
    await this.req('DELETE', 'open_acreage_candidate?id=not.is.null');
    if (c.length) await this.req('POST', 'open_acreage_candidate', c, { prefer: 'return=minimal' });
  }
  async getCandidates() { return (await this.req('GET', 'open_acreage_candidate?select=*')) as Candidate[]; }
  async getLayerEdit(u: string) { const r = (await this.req('GET', `layer_state?layer_url=eq.${encodeURIComponent(u)}&select=last_edit_date`)) as any[]; return r[0]?.last_edit_date ?? null; }
  async setLayerEdit(u: string, iso: string | null) { await this.req('POST', 'layer_state?on_conflict=layer_url', { layer_url: u, last_edit_date: iso, updated_at: new Date().toISOString() }, { prefer: 'resolution=merge-duplicates,return=minimal' }); }
}

export function storeFromEnv(env = process.env): SupabaseStore | null {
  return env.SUPABASE_URL && env.SUPABASE_SERVICE_ROLE_KEY ? new SupabaseStore(env.SUPABASE_URL, env.SUPABASE_SERVICE_ROLE_KEY) : null;
}
