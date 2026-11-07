import { bboxOf, intersects } from './geometry.ts';
import type { Store } from './store.ts';
import type { BaseRecord, LeaseRecord } from './types.ts';

export interface EnrichStats { leasesWithoutCounty: number; filled: number; ambiguous: number; noOverlap: number }

const CELL = 0.25; // degrees; spatial grid so 40k leases x 3k sections is not a full cross product
const cells = (b: { minLon: number; minLat: number; maxLon: number; maxLat: number }) => {
  const out: string[] = [];
  for (let x = Math.floor(b.minLon / CELL); x <= Math.floor(b.maxLon / CELL); x++) for (let y = Math.floor(b.minLat / CELL); y <= Math.floor(b.maxLat / CELL); y++) out.push(`${x}:${y}`);
  return out;
};

/**
 * Scott's call 2026-11-07: BLM leases carry no county, so derive it from the University Lands section grid.
 * A county is set ONLY when every overlapping UL section is in the same county; it is marked county_derived (never source-stated).
 * Several counties => county stays NULL and county_candidates lists them. No overlap (e.g. Midland, Glasscock: no UL sections) => NULL.
 */
export async function enrichLeaseCounties(store: Store, now: string): Promise<EnrichStats> {
  const sections = (await store.getAll('section_grid')).filter((s) => s.geom && s.county);
  const index = new Map<string, BaseRecord[]>();
  for (const s of sections) { const b = bboxOf(s.geom!); if (b) for (const c of cells(b)) index.set(c, [...(index.get(c) ?? []), s]); }
  const stats: EnrichStats = { leasesWithoutCounty: 0, filled: 0, ambiguous: 0, noOverlap: 0 };
  const leases = (await store.getAll('lease')) as LeaseRecord[];
  const layers = new Set(leases.filter((l) => !l.county && l.source_name !== 'UL').map((l) => `${l.source_name}|${l.source_layer}`));
  for (const key of layers) {
    const [source, layer] = key.split('|'); const recs = (await store.getPromoted(source, layer)) as LeaseRecord[]; let changed = false;
    const next = recs.map((l) => {
      if (l.county || !l.geom) return l;
      stats.leasesWithoutCounty++;
      const b = bboxOf(l.geom); const seen = new Set<BaseRecord>(); const counties = new Map<string, string>();
      if (b) for (const c of cells(b)) for (const s of index.get(c) ?? []) { if (seen.has(s)) continue; seen.add(s); if (intersects(s.geom!, l.geom)) counties.set(String(s.county).toLowerCase().trim(), String(s.county)); }
      if (counties.size === 1) { stats.filled++; changed = true; return { ...l, county: [...counties.values()][0], county_derived: true, county_source: 'University Lands section overlap (derived, not source-stated)' } as LeaseRecord; }
      if (counties.size > 1) { stats.ambiguous++; changed = true; return { ...l, county_candidates: [...counties.values()].sort() } as LeaseRecord; }
      stats.noOverlap++; return l;
    });
    if (changed) await store.promoteLayer(source, layer, 'lease', next, `enrich:${now}`);
  }
  return stats;
}
