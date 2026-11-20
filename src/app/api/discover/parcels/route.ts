import { NextRequest, NextResponse } from 'next/server';
import { demoParcelsInBbox } from '@/lib/discover/demo-data';
import type { Parcel, ParcelsResponse } from '@/lib/discover/types';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

/**
 * GET /api/discover/parcels?bbox=west,south,east,north
 *
 * With PARCEL_QUERY_URL set (an ArcGIS REST ".../FeatureServer/0/query" or
 * ".../MapServer/0/query" URL) this proxies live county parcel data.
 * Otherwise — or if the upstream fails — it serves the built-in demo parcels.
 *
 * PARCEL_FIELDS (optional JSON) maps our fields to the layer's attribute names:
 *   {"apn":"APN","owner":"OWNER_NAME","siteAddress":"SITE_ADDR","city":"CITY",
 *    "county":"COUNTY","zoning":"ZONING","acres":"ACRES"}
 */

const MAX_SPAN_DEG = 0.25; // refuse to pull a whole county in one request
const MAX_FEATURES = 1000;

const DEFAULT_FIELDS = {
  apn: 'APN',
  owner: 'OWNER',
  siteAddress: 'SITE_ADDRESS',
  city: 'CITY',
  county: 'COUNTY',
  zoning: 'ZONING',
  acres: 'ACRES',
};

type FieldMap = typeof DEFAULT_FIELDS;

function loadFieldMap(): FieldMap {
  const raw = process.env.PARCEL_FIELDS;
  if (!raw) return DEFAULT_FIELDS;
  try {
    return { ...DEFAULT_FIELDS, ...(JSON.parse(raw) as Partial<FieldMap>) };
  } catch {
    return DEFAULT_FIELDS;
  }
}

function ringsOf(geometry: GeoJSON.Geometry | null): number[][][] | null {
  if (!geometry) return null;
  if (geometry.type === 'Polygon') return geometry.coordinates as number[][][];
  if (geometry.type === 'MultiPolygon') {
    // Keep the outer ring of each polygon; enough for display.
    return (geometry.coordinates as number[][][][]).map((poly) => poly[0]);
  }
  return null;
}

function centroid(rings: number[][][]): [number, number] {
  let sx = 0;
  let sy = 0;
  let n = 0;
  for (const [x, y] of rings[0]) {
    sx += x;
    sy += y;
    n++;
  }
  return n ? [sy / n, sx / n] : [0, 0];
}

function str(v: unknown): string {
  return v === null || v === undefined ? '' : String(v).trim();
}

function toParcel(f: GeoJSON.Feature, fields: FieldMap, i: number): Parcel | null {
  const rings = ringsOf(f.geometry);
  if (!rings || rings.length === 0 || rings[0].length < 4) return null;
  const p = (f.properties ?? {}) as Record<string, unknown>;
  const apn = str(p[fields.apn]);
  const acresRaw = Number(p[fields.acres]);
  return {
    id: apn || `live-${i}`,
    apn: apn || 'N/A',
    owner: str(p[fields.owner]) || 'Unknown',
    siteAddress: str(p[fields.siteAddress]) || 'No site address',
    city: str(p[fields.city]),
    county: str(p[fields.county]),
    zoning: str(p[fields.zoning]) || 'N/A',
    acres: Number.isFinite(acresRaw) ? Math.round(acresRaw * 100) / 100 : 0,
    rings,
    center: centroid(rings),
  };
}

function parseBbox(value: string | null): [number, number, number, number] | null {
  if (!value) return null;
  const parts = value.split(',').map(Number);
  if (parts.length !== 4 || parts.some((n) => !Number.isFinite(n))) return null;
  const [west, south, east, north] = parts;
  if (west >= east || south >= north) return null;
  if (south < -90 || north > 90 || west < -180 || east > 180) return null;
  return [west, south, east, north];
}

export async function GET(request: NextRequest) {
  const bbox = parseBbox(request.nextUrl.searchParams.get('bbox'));
  if (!bbox) {
    return NextResponse.json(
      { error: 'bbox must be "west,south,east,north" in degrees' },
      { status: 400 },
    );
  }
  const [west, south, east, north] = bbox;

  const upstream = process.env.PARCEL_QUERY_URL;
  if (!upstream) {
    const body: ParcelsResponse = {
      source: 'demo',
      parcels: demoParcelsInBbox(south, west, north, east),
      note: 'Showing demo parcels. Set PARCEL_QUERY_URL to load real county parcel data.',
    };
    return NextResponse.json(body);
  }

  if (east - west > MAX_SPAN_DEG || north - south > MAX_SPAN_DEG) {
    const body: ParcelsResponse = {
      source: 'live',
      parcels: [],
      note: 'Zoom in to load parcels.',
    };
    return NextResponse.json(body);
  }

  try {
    const url = new URL(upstream);
    url.search = new URLSearchParams({
      f: 'geojson',
      where: '1=1',
      outFields: '*',
      returnGeometry: 'true',
      geometry: `${west},${south},${east},${north}`,
      geometryType: 'esriGeometryEnvelope',
      inSR: '4326',
      outSR: '4326',
      spatialRel: 'esriSpatialRelIntersects',
      resultRecordCount: String(MAX_FEATURES),
    }).toString();

    const res = await fetch(url, { signal: AbortSignal.timeout(15_000), cache: 'no-store' });
    if (!res.ok) throw new Error(`Parcel service responded ${res.status}`);
    const json = (await res.json()) as GeoJSON.FeatureCollection & { error?: { message?: string } };
    if (json.error) throw new Error(json.error.message ?? 'Parcel service error');

    const fields = loadFieldMap();
    const parcels = (json.features ?? [])
      .map((f, i) => toParcel(f, fields, i))
      .filter((p): p is Parcel => p !== null);

    const body: ParcelsResponse = {
      source: 'live',
      parcels,
      note: parcels.length >= MAX_FEATURES ? 'Result limit reached — zoom in for the rest.' : undefined,
    };
    return NextResponse.json(body);
  } catch (error) {
    const body: ParcelsResponse = {
      source: 'demo',
      parcels: demoParcelsInBbox(south, west, north, east),
      note: `Live parcel service failed (${
        error instanceof Error ? error.message : 'unknown error'
      }); showing demo parcels.`,
    };
    return NextResponse.json(body);
  }
}
