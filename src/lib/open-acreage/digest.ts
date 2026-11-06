import { CANDIDATE_STATUS, CONSTITUTION_VERSION } from './constitution.ts';
import { expiringLeases, staleSources } from './candidates.ts';
import type { OpenAcreageConfig } from './config.ts';
import type { Store } from './store.ts';
import type { LeaseRecord, SyncRun } from './types.ts';

function latestBySource(runs: SyncRun[]): SyncRun[] {
  const m = new Map<string, SyncRun>();
  for (const r of runs) { const c = m.get(r.source); if (!c || r.started_at >= c.started_at) m.set(r.source, r); }
  return [...m.values()];
}

export async function buildDigest(store: Store, cfg: OpenAcreageConfig, now: string): Promise<string> {
  const day = Date.parse(now) - 86_400_000;
  const [changes, candidates, runs, leases, sections] = await Promise.all([store.getChanges(), store.getCandidates(), store.getRuns(), store.getAll('lease'), store.getAll('section_grid')]);
  const normC = (x: unknown) => String(x ?? '').toLowerCase().replace(/\s+county$/, '').trim();
  const uncovered = cfg.targetCounties.filter((t) => !sections.some((s) => normC(s.county) === normC(t)));
  const newCands = candidates.filter((c) => c.status === CANDIDATE_STATUS && Date.parse(c.created_at) >= day);
  const exp = expiringLeases(leases as LeaseRecord[], cfg, now);
  const latest = latestBySource(runs); // an old failed attempt must not outlive a later good run
  const notOk = latest.filter((r) => r.status !== 'OK');
  const stale = staleSources(runs, cfg, now);
  const recent = changes.filter((c) => Date.parse(c.detected_at) >= day);
  // "Expired today" = leases whose status CHANGED to expired/inactive in the last 24h, not the standing total.
  const newlyExpired = recent.filter((c) => c.entity_type === 'lease' && c.change_type === 'STATUS_CHANGE' && (c.new_value === 'EXPIRED' || c.new_value === 'INACTIVE')).length;
  const standing = exp.filter((e) => e.kind !== 'EXPIRING_SOON').length;
  const lines = [
    `*AMARA Open Acreage — daily digest* (Constitution ${CONSTITUTION_VERSION})`,
    `• Candidates (${CANDIDATE_STATUS}): ${newCands.length} new in 24h; ${candidates.filter((c) => c.status === CANDIDATE_STATUS).length} current, ${candidates.filter((c) => c.status === 'HOLD').length} on HOLD`,
    `• Leases newly expired/inactive (24h): ${newlyExpired} (standing expired/inactive on record: ${standing}); expiring within ${cfg.expiringDays}d: ${exp.filter((e) => e.kind === 'EXPIRING_SOON').length}`,
    `• Changes detected (24h): ${recent.length} (${['NEW', 'REMOVED', 'STATUS_CHANGE', 'EXPIRATION_CHANGE', 'GEOMETRY_CHANGE'].map((t) => `${t}:${recent.filter((c) => c.change_type === t).length}`).join(' ')})`,
    `• Sources failed / not OK (latest run): ${notOk.length ? notOk.map((r) => `${r.source}=${r.status}/${r.source_state}`).join(', ') : 'none'}`,
    ...(uncovered.length ? [`• Target counties with NO section-grid coverage (cannot be evaluated): ${uncovered.join(', ')}`] : []),
    `• Sources stale (>2x cadence): ${stale.length ? stale.join(', ') : 'none'}`,
    `Source freshness: ${latest.filter((r) => r.source_updated_at).map((r) => `${r.source} source_updated_at=${r.source_updated_at}`).join('; ') || 'UNKNOWN (no source states a last-updated time)'}`,
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
