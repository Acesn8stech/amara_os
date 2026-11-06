import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { dirname } from 'node:path';
import type { BaseRecord, Candidate, ChangeEvent, EntityType, Rejected, SyncRun } from './types.ts';

export interface Store {
  /** Currently promoted records for a layer. */
  getPromoted(source: string, layer: string): Promise<BaseRecord[]>;
  getAll(entity: EntityType): Promise<BaseRecord[]>;
  /** Replace promoted set for a layer (upsert on source_name+source_record_id); prior versions go to history. */
  promoteLayer(source: string, layer: string, entity: EntityType, records: BaseRecord[], runId: string): Promise<void>;
  saveRejected(rejected: Rejected[], runId: string): Promise<void>;
  saveRun(run: SyncRun): Promise<void>;
  getRuns(): Promise<SyncRun[]>;
  saveChanges(c: ChangeEvent[]): Promise<void>;
  getChanges(): Promise<ChangeEvent[]>;
  replaceCandidates(c: Candidate[]): Promise<void>;
  getCandidates(): Promise<Candidate[]>;
  getLayerEdit(layerUrl: string): Promise<string | null>;
  setLayerEdit(layerUrl: string, iso: string | null): Promise<void>;
}

interface State {
  promoted: Record<string, BaseRecord[]>; history: BaseRecord[]; rejected: Array<Rejected & { run_id: string }>;
  runs: SyncRun[]; changes: ChangeEvent[]; candidates: Candidate[]; layerEdit: Record<string, string | null>;
}
const empty = (): State => ({ promoted: {}, history: [], rejected: [], runs: [], changes: [], candidates: [], layerEdit: {} });

export class MemoryStore implements Store {
  state: State;
  constructor(state?: State) { this.state = state ?? empty(); }
  async getPromoted(source: string, layer: string) { return this.state.promoted[`${source}|${layer}`] ?? []; }
  async getAll(entity: EntityType) { return Object.values(this.state.promoted).flat().filter((r) => r.entity_type === entity); }
  async promoteLayer(source: string, layer: string, _e: EntityType, records: BaseRecord[], runId: string) {
    const k = `${source}|${layer}`; const old = new Map((this.state.promoted[k] ?? []).map((r) => [r.source_record_id, r]));
    for (const r of records) { const p = old.get(r.source_record_id); if (p && JSON.stringify(p.raw) !== JSON.stringify(r.raw)) this.state.history.push(p); }
    this.state.promoted[k] = records.map((r) => ({ ...r, sync_run_id: runId }));
  }
  async saveRejected(rejected: Rejected[], runId: string) { this.state.rejected.push(...rejected.map((r) => ({ ...r, run_id: runId }))); }
  async saveRun(run: SyncRun) { const i = this.state.runs.findIndex((r) => r.run_id === run.run_id); if (i >= 0) this.state.runs[i] = run; else this.state.runs.push(run); }
  async getRuns() { return this.state.runs; }
  async saveChanges(c: ChangeEvent[]) { this.state.changes.push(...c); }
  async getChanges() { return this.state.changes; }
  async replaceCandidates(c: Candidate[]) { this.state.candidates = c; }
  async getCandidates() { return this.state.candidates; }
  async getLayerEdit(u: string) { return this.state.layerEdit[u] ?? null; }
  async setLayerEdit(u: string, iso: string | null) { this.state.layerEdit[u] = iso; }
}

/** Key-less local mode for Hermes/dev: JSON file persisted after each mutation. */
export class FileStore extends MemoryStore {
  private path: string;
  constructor(path: string) {
    super(existsSync(path) ? (JSON.parse(readFileSync(path, 'utf8')) as State) : undefined);
    this.path = path;
  }
  flush() { mkdirSync(dirname(this.path), { recursive: true }); writeFileSync(this.path, JSON.stringify(this.state)); }
  async promoteLayer(...a: Parameters<MemoryStore['promoteLayer']>) { await super.promoteLayer(...a); this.flush(); }
  async saveRun(r: SyncRun) { await super.saveRun(r); this.flush(); }
  async saveChanges(c: ChangeEvent[]) { await super.saveChanges(c); this.flush(); }
  async replaceCandidates(c: Candidate[]) { await super.replaceCandidates(c); this.flush(); }
  async saveRejected(r: Rejected[], id: string) { await super.saveRejected(r, id); this.flush(); }
  async setLayerEdit(u: string, i: string | null) { await super.setLayerEdit(u, i); this.flush(); }
}
