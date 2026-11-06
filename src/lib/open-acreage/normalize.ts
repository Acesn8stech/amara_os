import type { Geometry, StatusNormalized } from './types.ts';

/** Case-insensitive lookup over alias list. Returns null when no alias matches (Celilo: no guess). */
export function pick(attrs: Record<string, unknown>, aliases: string[]): { field: string; value: unknown } | null {
  const lower = new Map(Object.keys(attrs).map((k) => [k.toLowerCase(), k]));
  for (const a of aliases) {
    const k = lower.get(a.toLowerCase());
    if (k !== undefined) {
      const v = attrs[k];
      if (v !== null && v !== undefined && !(typeof v === 'string' && v.trim() === '')) return { field: k, value: v };
    }
  }
  return null;
}

export function str(attrs: Record<string, unknown>, aliases: string[]): string | null {
  const p = pick(attrs, aliases);
  return p ? String(p.value).trim() : null;
}

export function num(attrs: Record<string, unknown>, aliases: string[]): number | null {
  const p = pick(attrs, aliases);
  if (!p) return null;
  const n = typeof p.value === 'number' ? p.value : Number(String(p.value).replace(/,/g, ''));
  return Number.isFinite(n) ? n : null;
}

/** Returns YYYY-MM-DD or null. Never invents a date. Unparseable => null. */
export function parseDate(value: unknown): string | null {
  if (value === null || value === undefined || value === '') return null;
  let d: Date | null = null;
  if (typeof value === 'number') {
    if (!Number.isFinite(value) || value <= 0) return null;
    d = new Date(value); // ArcGIS epoch ms
  } else {
    const s = String(value).trim();
    const us = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(s);
    if (us) d = new Date(Date.UTC(+us[3], +us[1] - 1, +us[2]));
    else if (/^\d{4}-\d{2}-\d{2}/.test(s)) d = new Date(s.length === 10 ? `${s}T00:00:00Z` : s);
    else if (/^\d{8}$/.test(s)) d = new Date(Date.UTC(+s.slice(0, 4), +s.slice(4, 6) - 1, +s.slice(6, 8)));
    else return null;
  }
  if (!d || Number.isNaN(d.getTime())) return null;
  const y = d.getUTCFullYear();
  if (y < 1850 || y > 2200) return null;
  return d.toISOString().slice(0, 10);
}

export function dateField(attrs: Record<string, unknown>, aliases: string[]): string | null {
  const p = pick(attrs, aliases);
  return p ? parseDate(p.value) : null;
}

/**
 * Status comes only from what the source states. We never derive ACTIVE/EXPIRED from a date
 * (that would be inference); no status text => UNKNOWN.
 */
/**
 * Exact UL OG_Lease.LeaseStatus vocabulary (distinct values read live 2026-11-06). Only in-force readings map to ACTIVE
 * (the safe direction: it can only exclude a tract, never offer one). Everything else stays UNKNOWN until Scott rules on it:
 *   'Non Producing' — could be an in-force extension or a lapsed lease; left UNKNOWN so it surfaces as HOLD, not hidden.
 *   'Pending', 'Under Review' — not stated as active or inactive.
 */
export const UL_LEASE_STATUS: Record<string, StatusNormalized> = {
  'held by unit': 'ACTIVE',
  'producing': 'ACTIVE',
  'producing (pt)': 'ACTIVE',
  'primary term non-producing': 'ACTIVE', // still inside its primary term => in force
  'shut-in royalty': 'ACTIVE', // lease maintained by shut-in royalty payments
  // BLM Oil_Gas_Leases_Case_Disp.CSE_DISP (one layer per value, read live 2026-11-06). 'Authorized' = lease issued/in force;
  // 'Closed' = source-stated closure (cause/date not given, so INACTIVE not EXPIRED); Pending/Interim not stated as in force.
  'authorized': 'ACTIVE',
  'closed': 'INACTIVE',
  'interim': 'UNKNOWN',
  'non producing': 'UNKNOWN',
  'pending': 'UNKNOWN',
  'under review': 'UNKNOWN',
};

export function normalizeStatus(raw: string | null): StatusNormalized {
  if (!raw) return 'UNKNOWN';
  const exact = UL_LEASE_STATUS[raw.trim().toLowerCase()];
  if (exact) return exact;
  const s = raw.toLowerCase();
  // "Non-Producing" is not stated as active or inactive by the source: do not read it as ACTIVE via the word "producing".
  if (/non[- ]?producing|not producing/.test(s)) return 'UNKNOWN';
  if (/expired|terminated|lapsed/.test(s)) return 'EXPIRED';
  if (/inactive|cancel|relinquish|released|surrender|closed|not active/.test(s)) return 'INACTIVE';
  if (/\bactive\b|producing|held by production|\bhbp\b|in force|\bheld\b|\bleased\b/.test(s)) return 'ACTIVE';
  return 'UNKNOWN';
}

/** ArcGIS f=geojson geometry -> our Geometry. Polygons are promoted to MultiPolygon. */
export function toGeometry(g: unknown): Geometry | null {
  if (!g || typeof g !== 'object') return null;
  const geo = g as { type?: string; coordinates?: unknown };
  if (!geo.type || !geo.coordinates) return null;
  if (geo.type === 'Polygon') return { type: 'MultiPolygon', coordinates: [geo.coordinates as number[][][]] };
  if (geo.type === 'MultiPolygon' || geo.type === 'Point' || geo.type === 'LineString') return geo as Geometry;
  return null;
}
