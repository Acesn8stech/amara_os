import { CANDIDATE_STATUS, CONSTITUTION_VERSION } from './constitution.ts';
import { expiringLeases, staleSources } from './candidates.ts';
import type { OpenAcreageConfig } from './config.ts';
import type { Store } from './store.ts';
import type { LeaseRecord } from './types.ts';

export async function buildDigest(store: Store, cfg: OpenAcreageConfig, now: string): Promise<string> {
  const day = Date.parse(now) - 86_400_000;
  const [changes, candidates, runs, leases] = await Promise.all([store.getChanges(), store.getCandidates(), store.getRuns(), store.getAll('lease')]);
  const newCands = candidates.filter((c) => c.status === CANDIDATE_STATUS && Date.parse(c.created_at) >= day);
  const exp = expiringLeases(leases as LeaseRecord[], cfg, now);
  const failed = runs.filter((r) => Date.parse(r.started_at) >= day && (r.status === 'FAILED' || r.source_state !== 'OK'));
  const stale = staleSources(runs, cfg, now);
  const recent = changes.filter((c) => Date.parse(c.detected_at) >= day);
  const lines = [
    `*AMARA Open Acreage — daily digest* (Constitution ${CONSTITUTION_VERSION})`,
    `• Candidates (${CANDIDATE_STATUS}): ${newCands.length} current of ${candidates.length} total tracts evaluated`,
    `• Leases expired/inactive: ${exp.filter((e) => e.kind !== 'EXPIRING_SOON').length}; expiring within ${cfg.expiringDays}d: ${exp.filter((e) => e.kind === 'EXPIRING_SOON').length}`,
    `• Changes detected (24h): ${recent.length} (${['NEW', 'REMOVED', 'STATUS_CHANGE', 'EXPIRATION_CHANGE', 'GEOMETRY_CHANGE'].map((t) => `${t}:${recent.filter((c) => c.change_type === t).length}`).join(' ')})`,
    `• Sources failed / not OK: ${failed.length ? failed.map((r) => `${r.source}=${r.status}/${r.source_state}`).join(', ') : 'none'}`,
    `• Sources stale (>2x cadence): ${stale.length ? stale.join(', ') : 'none'}`,
    `Source freshness: ${runs.length ? runs.filter((r) => r.source_updated_at).map((r) => `${r.source} source_updated_at=${r.source_updated_at}`).join('; ') || 'UNKNOWN' : 'no runs'}`,
    `Candidates are NOT open/leasable — title check required.`,
  ];
  return lines.join('\n');
}

/** Posts only when SLACK_WEBHOOK_URL is set (keys added LAST). Otherwise reports not-sent. */
export async function pushSlack(text: string, env = process.env, f: typeof fetch = fetch): Promise<{ sent: boolean; reason?: string }> {
  const url = env.SLACK_WEBHOOK_URL;
  if (!url) return { sent: false, reason: 'SLACK_WEBHOOK_URL not set' };
  const res = await f(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ text }) });
  return res.ok ? { sent: true } : { sent: false, reason: `HTTP ${res.status}` };
}
