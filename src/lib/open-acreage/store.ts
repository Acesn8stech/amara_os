import { readFileSync, writeFileSync, writeSync, openSync, closeSync, mkdirSync, existsSync, renameSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
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

function writeJsonArray(path: string, arr: unknown[]) {
  // Chunked: a single JSON.stringify of a big array exceeds V8's max string length (~512 MB).
  mkdirSync(dirname(path), { recursive: true });
  const fd = openSync(`${path}.tmp`, 'w');
  try {
    writeSync(fd, '[');
    for (let i = 0; i < arr.length; i += 500) {
      const chunk = arr.slice(i, i + 500).map((x) => JSON.stringify(x)).join(',');
      writeSync(fd, (i ? ',' : '') + chunk);
    }
    writeSync(fd, ']');
  } finally { closeSync(fd); }
  renameSync(`${path}.tmp`, path);
}
const readJson = <T>(path: string, fallback: T): T => (existsSync(path) ? (JSON.parse(readFileSync(path, 'utf8')) as T) : fallback);

/**
 * Key-less local mode for Hermes/dev. State is sharded under <dir>/state.d/: one file per promoted layer plus small meta/changes/rejected/history
 * files, each rewritten only when it changes. A legacy single-file state.json is migrated automatically (and renamed .migrated).
 */
export class FileStore extends MemoryStore {
  private dir: string;
  private legacy: string | null;
  constructor(path: string) {
    super(FileStore.load(path));
    this.dir = join(dirname(path), 'state.d');
    this.legacy = existsSync(path) && !existsSync(join(this.dir, 'meta.json')) ? path : null;
    if (this.legacy) { this.flushAll(); renameSync(this.legacy, `${this.legacy}.migrated`); this.legacy = null; }
  }
  private static load(path: string): State | undefined {
    const dir = join(dirname(path), 'state.d');
    if (existsSync(join(dir, 'meta.json'))) {
      const meta = readJson<Pick<State, 'runs' | 'candidates' | 'layerEdit'>>(join(dir, 'meta.json'), { runs: [], candidates: [], layerEdit: {} });
      const promoted: State['promoted'] = {};
      const pdir = join(dir, 'promoted');
      if (existsSync(pdir)) for (const f of readdirSync(pdir)) if (f.endsWith('.json')) promoted[decodeURIComponent(f.slice(0, -5))] = readJson<BaseRecord[]>(join(pdir, f), []);
      return { ...meta, promoted, changes: readJson(join(dir, 'changes.json'), []), rejected: readJson(join(dir, 'rejected.json'), []), history: readJson(join(dir, 'history.json'), []) };
    }
    return existsSync(path) ? (JSON.parse(readFileSync(path, 'utf8')) as State) : undefined;
  }
  private flushMeta() { mkdirSync(this.dir, { recursive: true }); writeFileSync(join(this.dir, 'meta.json'), JSON.stringify({ runs: this.state.runs, candidates: this.state.candidates, layerEdit: this.state.layerEdit })); }
  private flushLayer(key: string) { writeJsonArray(join(this.dir, 'promoted', `${encodeURIComponent(key)}.json`), this.state.promoted[key] ?? []); }
  private flushAll() {
    this.flushMeta();
    for (const k of Object.keys(this.state.promoted)) this.flushLayer(k);
    writeJsonArray(join(this.dir, 'changes.json'), this.state.changes); writeJsonArray(join(this.dir, 'rejected.json'), this.state.rejected); writeJsonArray(join(this.dir, 'history.json'), this.state.history);
  }
  async promoteLayer(source: string, layer: string, e: EntityType, records: BaseRecord[], runId: string) {
    const h = this.state.history.length; await super.promoteLayer(source, layer, e, records, runId);
    this.flushLayer(`${source}|${layer}`); if (this.state.history.length !== h) writeJsonArray(join(this.dir, 'history.json'), this.state.history);
  }
  async saveRun(r: SyncRun) { await super.saveRun(r); this.flushMeta(); }
  async saveChanges(c: ChangeEvent[]) { await super.saveChanges(c); writeJsonArray(join(this.dir, 'changes.json'), this.state.changes); }
  async replaceCandidates(c: Candidate[]) { await super.replaceCandidates(c); this.flushMeta(); }
  async saveRejected(r: Rejected[], id: string) { await super.saveRejected(r, id); writeJsonArray(join(this.dir, 'rejected.json'), this.state.rejected); }
  async setLayerEdit(u: string, i: string | null) { await super.setLayerEdit(u, i); this.flushMeta(); }
}
