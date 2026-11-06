import type { Geometry } from './types.ts';

type Ring = number[][];
export interface BBox { minLon: number; minLat: number; maxLon: number; maxLat: number }

function walk(geom: Geometry, f: (x: number, y: number) => void) {
  const rec = (c: unknown): void => {
    if (Array.isArray(c) && typeof c[0] === 'number') f(c[0] as number, c[1] as number);
    else if (Array.isArray(c)) c.forEach(rec);
  };
  rec(geom.coordinates);
}

export function bboxOf(geom: Geometry): BBox | null {
  let b: BBox | null = null;
  walk(geom, (x, y) => {
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    b = b ? { minLon: Math.min(b.minLon, x), minLat: Math.min(b.minLat, y), maxLon: Math.max(b.maxLon, x), maxLat: Math.max(b.maxLat, y) }
      : { minLon: x, minLat: y, maxLon: x, maxLat: y };
  });
  return b;
}

export const bboxOverlap = (a: BBox, b: BBox) =>
  a.minLon <= b.maxLon && a.maxLon >= b.minLon && a.minLat <= b.maxLat && a.maxLat >= b.minLat;

export function centroid(geom: Geometry): [number, number] | null {
  const b = bboxOf(geom);
  return b ? [(b.minLon + b.maxLon) / 2, (b.minLat + b.maxLat) / 2] : null;
}

/** Minimal ring validity: closed, >= 4 positions, finite, non-zero area. (PostGIS ST_IsValid is authoritative in DB.) */
export function isValidGeometry(geom: Geometry | null): boolean {
  if (!geom) return false;
  let finite = true;
  walk(geom, (x, y) => { if (!Number.isFinite(x) || !Number.isFinite(y)) finite = false; });
  if (!finite) return false;
  const rings: Ring[] = geom.type === 'Polygon' ? geom.coordinates : geom.type === 'MultiPolygon' ? geom.coordinates.flat() : [];
  if (geom.type === 'Point') return geom.coordinates.length >= 2;
  if (geom.type === 'LineString') return geom.coordinates.length >= 2;
  if (!rings.length) return false;
  return rings.every((r) => {
    if (r.length < 4) return false;
    const [fx, fy] = r[0]; const [lx, ly] = r[r.length - 1];
    if (fx !== lx || fy !== ly) return false;
    let a = 0;
    for (let i = 0; i < r.length - 1; i++) a += r[i][0] * r[i + 1][1] - r[i + 1][0] * r[i][1];
    return Math.abs(a) > 0;
  });
}

function pointInRing(x: number, y: number, r: Ring): boolean {
  let inside = false;
  for (let i = 0, j = r.length - 1; i < r.length; j = i++) {
    const [xi, yi] = r[i]; const [xj, yj] = r[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}
const polys = (g: Geometry): Ring[][] => (g.type === 'Polygon' ? [g.coordinates] : g.type === 'MultiPolygon' ? g.coordinates : []);

function segInt(a: number[], b: number[], c: number[], d: number[]): boolean {
  const o = (p: number[], q: number[], r: number[]) => Math.sign((q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]));
  return o(a, b, c) !== o(a, b, d) && o(c, d, a) !== o(c, d, b);
}

export function pointInGeometry(p: [number, number], g: Geometry): boolean {
  return polys(g).some(([outer, ...holes]) => pointInRing(p[0], p[1], outer) && !holes.some((h) => pointInRing(p[0], p[1], h)));
}

/** Polygon/point intersection (area overlap or boundary crossing). Touching-only edges count as intersect via vertex tests. */
export function intersects(a: Geometry, b: Geometry): boolean {
  const ba = bboxOf(a); const bb = bboxOf(b);
  if (!ba || !bb || !bboxOverlap(ba, bb)) return false;
  if (a.type === 'Point') return b.type === 'Point' ? a.coordinates[0] === b.coordinates[0] && a.coordinates[1] === b.coordinates[1] : pointInGeometry([a.coordinates[0], a.coordinates[1]], b);
  if (b.type === 'Point') return pointInGeometry([b.coordinates[0], b.coordinates[1]], a);
  // Identical or collinear-edged polygons (a whole-section lease over its section) defeat the vertex/edge tests above, so also test
  // representative interior points: missing an overlap would offer a leased tract as a candidate (the unsafe direction).
  const ca = centroid(a); const cb = centroid(b);
  if ((ca && pointInGeometry(ca, b)) || (cb && pointInGeometry(cb, a))) return true;
  const pa = polys(a); const pb = polys(b);
  for (const A of pa) for (const B of pb) {
    if (A[0].some((v) => pointInGeometry([v[0], v[1]], { type: 'Polygon', coordinates: B }))) return true;
    if (B[0].some((v) => pointInGeometry([v[0], v[1]], { type: 'Polygon', coordinates: A }))) return true;
    for (let i = 0; i < A[0].length - 1; i++) for (let j = 0; j < B[0].length - 1; j++)
      if (segInt(A[0][i], A[0][i + 1], B[0][j], B[0][j + 1])) return true;
  }
  return false;
}

export function haversineMiles(a: [number, number], b: [number, number]): number {
  const R = 3958.8; const rad = (d: number) => (d * Math.PI) / 180;
  const dLat = rad(b[1] - a[1]); const dLon = rad(b[0] - a[0]);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(rad(a[1])) * Math.cos(rad(b[1])) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

export function inBBox(geom: Geometry, box: BBox): boolean {
  const b = bboxOf(geom);
  return !!b && b.minLon >= box.minLon && b.maxLon <= box.maxLon && b.minLat >= box.minLat && b.maxLat <= box.maxLat;
}
