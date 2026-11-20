import { NextRequest, NextResponse } from 'next/server';
import { searchDemoParcels } from '@/lib/discover/demo-data';
import type { GeocodeResponse, GeocodeResult } from '@/lib/discover/types';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

interface NominatimHit {
  display_name: string;
  lat: string;
  lon: string;
  boundingbox?: [string, string, string, string]; // south, north, west, east
}

/** GET /api/discover/geocode?q=... — place/address lookup plus demo APN/owner matches. */
export async function GET(request: NextRequest) {
  const q = (request.nextUrl.searchParams.get('q') ?? '').trim();
  if (q.length < 2 || q.length > 200) {
    return NextResponse.json({ error: 'q must be 2–200 characters' }, { status: 400 });
  }

  const parcelMatches = process.env.PARCEL_QUERY_URL ? [] : searchDemoParcels(q);
  let results: GeocodeResult[] = [];

  try {
    const url = new URL('https://nominatim.openstreetmap.org/search');
    url.search = new URLSearchParams({
      q,
      format: 'jsonv2',
      limit: '5',
      countrycodes: 'us',
    }).toString();
    const res = await fetch(url, {
      headers: { 'User-Agent': 'amara-os-discover/0.1' },
      signal: AbortSignal.timeout(8_000),
      cache: 'no-store',
    });
    if (res.ok) {
      const hits = (await res.json()) as NominatimHit[];
      results = hits.map((h) => {
        const bb = h.boundingbox?.map(Number);
        return {
          label: h.display_name,
          lat: Number(h.lat),
          lng: Number(h.lon),
          bbox: bb && bb.length === 4 ? [bb[0], bb[2], bb[1], bb[3]] : undefined,
        };
      });
    }
  } catch {
    // Geocoder unreachable: still return any parcel matches.
  }

  const body: GeocodeResponse = { results, parcelMatches };
  return NextResponse.json(body);
}
