import type { Parcel } from './types';

/**
 * Clearly-fake parcels laid out on a grid near Las Vegas so the Discover map
 * works with no data provider configured. Owners/APNs are placeholders, not
 * real records.
 */
const ORIGIN = { lat: 36.1699, lng: -115.1398 };
const ZONES = ['R-1', 'R-2', 'C-1', 'C-2', 'M-1', 'RE', 'PF'];
const OWNERS = [
  'DEMO HOLDINGS LLC',
  'SAMPLE FAMILY TRUST',
  'EXAMPLE DESERT LAND CO',
  'PLACEHOLDER VENTURES LP',
  'TEST PARCEL OWNER INC',
  'SAGEBRUSH DEMO PARTNERS',
];
const STREETS = ['Mesa Rd', 'Desert Ln', 'Cactus Ave', 'Canyon Blvd', 'Sunset Way', 'Ridge Dr'];

// Degrees per acre-ish cell: ~0.004° lat ≈ 440 m.
const CELL_LAT = 0.0042;
const CELL_LNG = 0.0052;

function mulberry32(seed: number) {
  return () => {
    seed |= 0;
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function build(): Parcel[] {
  const rand = mulberry32(42);
  const out: Parcel[] = [];
  const cols = 8;
  const rows = 6;
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const south = ORIGIN.lat + (r - rows / 2) * CELL_LAT;
      const west = ORIGIN.lng + (c - cols / 2) * CELL_LNG;
      // Shrink each cell a little (random) so there are visible gaps/streets.
      const insetLat = CELL_LAT * (0.08 + rand() * 0.12);
      const insetLng = CELL_LNG * (0.08 + rand() * 0.12);
      const s = south + insetLat;
      const n = south + CELL_LAT - insetLat;
      const w = west + insetLng;
      const e = west + CELL_LNG - insetLng;
      // 111_320 m/deg lat; lng scaled by cos(lat).
      const hM = (n - s) * 111_320;
      const wM = (e - w) * 111_320 * Math.cos((ORIGIN.lat * Math.PI) / 180);
      const acres = (hM * wM) / 4046.8564224;
      const idx = r * cols + c;
      out.push({
        id: `demo-${idx}`,
        apn: `DEMO-${String(100 + r).padStart(3, '0')}-${String(10 + c).padStart(2, '0')}-${String(
          Math.floor(rand() * 900) + 100,
        )}`,
        owner: OWNERS[Math.floor(rand() * OWNERS.length)],
        siteAddress: `${Math.floor(rand() * 9000) + 100} ${STREETS[Math.floor(rand() * STREETS.length)]}`,
        city: 'Las Vegas',
        county: 'Clark',
        zoning: ZONES[Math.floor(rand() * ZONES.length)],
        acres: Math.round(acres * 100) / 100,
        rings: [
          [
            [w, s],
            [e, s],
            [e, n],
            [w, n],
            [w, s],
          ],
        ],
        center: [(s + n) / 2, (w + e) / 2],
      });
    }
  }
  return out;
}

export const DEMO_PARCELS: Parcel[] = build();
export const DEMO_CENTER: [number, number] = [ORIGIN.lat, ORIGIN.lng];

export function demoParcelsInBbox(
  south: number,
  west: number,
  north: number,
  east: number,
): Parcel[] {
  return DEMO_PARCELS.filter(
    (p) => p.center[0] >= south && p.center[0] <= north && p.center[1] >= west && p.center[1] <= east,
  );
}

export function searchDemoParcels(query: string): Parcel[] {
  const q = query.trim().toLowerCase();
  if (q.length < 2) return [];
  return DEMO_PARCELS.filter(
    (p) =>
      p.apn.toLowerCase().includes(q) ||
      p.owner.toLowerCase().includes(q) ||
      p.siteAddress.toLowerCase().includes(q),
  ).slice(0, 10);
}
