import { CANDIDATE_STATUS, CONSTITUTION_VERSION } from './constitution.ts';
import { expiringLeases } from './candidates.ts';
import type { OpenAcreageConfig } from './config.ts';
import type { Candidate, ChangeEvent, LeaseRecord, SyncRun } from './types.ts';

export type Intent =
  | { kind: 'whats_new'; county: string }
  | { kind: 'expiring_leases'; county: string }
  | { kind: 'candidate_count'; county: string }
  | { kind: 'unknown' };

export function parseIntent(utterance: string): Intent {
  const u = utterance.toLowerCase();
  const county = /(?:in|for)\s+([a-z][a-z .'-]*?)\s+county/i.exec(utterance)?.[1]?.trim() ?? null;
  if (!county) return { kind: 'unknown' };
  const name = county.replace(/\b\w/g, (c) => c.toUpperCase());
  if (/what'?s new|what is new|new in/.test(u)) return { kind: 'whats_new', county: name };
  if (/expir/.test(u)) return { kind: 'expiring_leases', county: name };
  if (/(open acreage|candidate)/.test(u) && /how many|count|number/.test(u)) return { kind: 'candidate_count', county: name };
  return { kind: 'unknown' };
}

export interface VoiceData { leases: LeaseRecord[]; candidates: Candidate[]; changes: ChangeEvent[]; runs: SyncRun[]; countyOfEntity: (entityId: string) => string | null }

const norm = (c: string | null | undefined) => (c ?? '').toLowerCase().replace(/\s+county$/, '').trim();

function freshness(runs: SyncRun[], sources: string[]): string {
  const parts = sources.map((s) => {
    const ok = runs.filter((r) => r.source === s && r.status === 'OK' && r.finished_at).sort((a, b) => b.finished_at!.localeCompare(a.finished_at!))[0];
    return ok ? `${s}: last good sync ${ok.finished_at}, source updated ${ok.source_updated_at ?? 'UNKNOWN'}` : `${s}: no good sync on record (UNVERIFIED)`;
  });
  return parts.join('; ');
}

/** Every answer reads source + freshness and the constitution version. Candidates are never called "open". */
export function answer(intent: Intent, d: VoiceData, cfg: OpenAcreageConfig, now: string): string {
  const tail = (srcs: string[]) => ` Sources — ${freshness(d.runs, srcs)}. Constitution ${CONSTITUTION_VERSION}.`;
  if (intent.kind === 'unknown') return `I didn't catch a county in that. Try "What's new in Reeves County?" Constitution ${CONSTITUTION_VERSION}.`;
  const c = norm(intent.county);
  if (intent.kind === 'candidate_count') {
    const list = d.candidates.filter((x) => norm(x.county) === c);
    const n = list.filter((x) => x.status === CANDIDATE_STATUS).length; const h = list.filter((x) => x.status === 'HOLD').length;
    if (!cfg.targetCounties.map(norm).includes(c)) return `${intent.county} County is not in the configured target list, so no candidates were evaluated — NOT FOUND.${tail(['UL', 'BLM', 'RRC'])}`;
    return `${n} ${CANDIDATE_STATUS.toLowerCase()} tract${n === 1 ? '' : 's'} in ${intent.county} County${h ? `, plus ${h} on HOLD` : ''}. These are not confirmed open; each needs a courthouse title check.${tail(['UL', 'BLM', 'RRC'])}`;
  }
  if (intent.kind === 'expiring_leases') {
    const rows = expiringLeases(d.leases.filter((l) => norm(l.county) === c), cfg, now);
    if (!rows.length) return `No expiring or expired leases on record in ${intent.county} County in the connected sources (not a statement about unrecorded leases).${tail(['UL', 'BLM'])}`;
    const e = rows.filter((r) => r.kind === 'EXPIRING_SOON').length;
    return `${rows.length} lease${rows.length === 1 ? '' : 's'} in ${intent.county} County: ${e} expiring within ${cfg.expiringDays} days and ${rows.length - e} expired or inactive.${tail(['UL', 'BLM'])}`;
  }
  const since = Date.parse(now) - 7 * 86_400_000;
  const ch = d.changes.filter((x) => Date.parse(x.detected_at) >= since && norm(d.countyOfEntity(x.entity_id)) === c);
  const by = (t: string) => ch.filter((x) => x.change_type === t).length;
  return ch.length
    ? `${ch.length} change${ch.length === 1 ? '' : 's'} in ${intent.county} County this week: ${by('NEW')} new, ${by('REMOVED')} removed, ${by('STATUS_CHANGE')} status, ${by('EXPIRATION_CHANGE')} expiration, ${by('GEOMETRY_CHANGE')} geometry.${tail(['UL', 'BLM', 'RRC'])}`
    : `No changes detected in ${intent.county} County in the last 7 days.${tail(['UL', 'BLM', 'RRC'])}`;
}
