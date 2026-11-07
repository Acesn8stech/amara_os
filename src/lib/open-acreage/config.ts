export interface SourceConfig {
  enabled: boolean; // kill switch
  cadenceHours: number; // stated update cadence; staleness = 2x
  timeoutMs: number;
  retries: number;
}

export interface OpenAcreageConfig {
  /** Scott sets this list. EMPTY => no candidate can be produced (Celilo: no guessing). */
  targetCounties: string[];
  expiringDays: number; // N
  permitMonths: number; // X
  productionMonths: number; // Y
  offsetMiles: number; // Z
  recentExpiredDays: number;
  /** Months (YYYY-MM) of the permit master that Scott has explicitly accepted as unavailable. Empty by default; every candidate then carries PERMIT_MONTH_UNAVAILABLE:<month>. */
  acceptedPermitGapMonths: string[];
  /** A permit point this close to a tract counts as inside it (RRC coordinates are NAD27; UL geometry is WGS84). */
  permitBufferMiles: number;
  /** Well statuses (UL codes) that may stay unlinked to RRC production without holding a tract. PA = plugged & abandoned: Scott to confirm. */
  unlinkedWellOkStatuses: string[];
  /** Scott's call 2026-11-07: tracts on a lease with unknown status (Under Review / no status) are NOT held. Set OA_UNKNOWN_LEASE_HOLDS=1 to hold them again. The reason code is always recorded. */
  holdOnUnknownLeaseStatus: boolean;
  maxDropRatio: number; // >20% drop halts promote
  bbox: { minLon: number; minLat: number; maxLon: number; maxLat: number }; // TX/NM envelope
  killSwitchAll: boolean;
  sources: Record<'UL' | 'BLM' | 'GLO' | 'RRC', SourceConfig>;
}

export const DEFAULT_CONFIG: OpenAcreageConfig = {
  targetCounties: [],
  expiringDays: 180,
  permitMonths: 24,
  productionMonths: 12,
  offsetMiles: 2,
  recentExpiredDays: 365,
  acceptedPermitGapMonths: [],
  permitBufferMiles: 0.15,
  unlinkedWellOkStatuses: [], // Scott 2026-11-07: PA meaning unconfirmed, so every well with no RRC production link holds its tract (OA_UNLINKED_OK_STATUSES=PA to allow PA)
  holdOnUnknownLeaseStatus: false,
  maxDropRatio: 0.2,
  // Generous TX + NM envelope (WGS84): TX/NM span lon -109.05..-93.5, lat 25.8..37.0
  bbox: { minLon: -109.06, minLat: 25.8, maxLon: -93.5, maxLat: 37.01 },
  killSwitchAll: false,
  sources: {
    UL: { enabled: true, cadenceHours: 24 * 31, timeoutMs: 60_000, retries: 3 }, // monthly files
    BLM: { enabled: true, cadenceHours: 24, timeoutMs: 60_000, retries: 3 },
    GLO: { enabled: true, cadenceHours: 24, timeoutMs: 60_000, retries: 1 }, // stubbed: NOT FOUND
    RRC: { enabled: true, cadenceHours: 24 * 31, timeoutMs: 300_000, retries: 3 },
  },
};

export function loadConfig(overrides: Partial<OpenAcreageConfig> = {}, env: Record<string, string | undefined> = process.env): OpenAcreageConfig {
  const counties = env.OA_TARGET_COUNTIES?.split(',').map((s) => s.trim()).filter(Boolean);
  const cfg: OpenAcreageConfig = {
    ...DEFAULT_CONFIG,
    ...overrides,
    sources: { ...DEFAULT_CONFIG.sources, ...(overrides.sources ?? {}) },
    ...(counties?.length ? { targetCounties: counties } : {}),
  };
  const gaps = env.OA_ALLOW_MISSING_PERMIT_MONTHS?.split(',').map((m) => m.trim()).filter((m) => /^\d{4}-\d{2}$/.test(m));
  if (gaps?.length) cfg.acceptedPermitGapMonths = gaps;
  if (env.OA_UNKNOWN_LEASE_HOLDS === '1') cfg.holdOnUnknownLeaseStatus = true;
  const ok = env.OA_UNLINKED_OK_STATUSES?.split(',').map((x) => x.trim().toUpperCase()).filter(Boolean);
  if (ok?.length) cfg.unlinkedWellOkStatuses = ok;
  if (env.OA_KILL_SWITCH === '1') cfg.killSwitchAll = true;
  for (const k of ['UL', 'BLM', 'GLO', 'RRC'] as const) {
    if (env[`OA_KILL_${k}`] === '1') cfg.sources[k] = { ...cfg.sources[k], enabled: false };
  }
  return cfg;
}
