import { NextResponse } from 'next/server';
import { loadConfig } from '@/lib/open-acreage/config';
import { CONSTITUTION_VERSION } from '@/lib/open-acreage/constitution';
import { buildLayers } from '@/lib/open-acreage/layers';
import { storeFromEnv } from '@/lib/open-acreage/supabase-store';
import type { LeaseRecord, Section, WellRecord, BaseRecord } from '@/lib/open-acreage/types';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

/** God Mode map layers (GeoJSON). Layers: LEASED, EXPIRING_EXPIRED, NO_LEASE_CANDIDATES, WELLS, UL_AVAILABLE_PARCELS. */
export async function GET() {
  const store = storeFromEnv();
  if (!store) {
    return NextResponse.json(
      { error: 'UNVERIFIED: Supabase store not configured (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY)', constitution_version: CONSTITUTION_VERSION },
      { status: 503 },
    );
  }
  try {
    const [leases, wells, parcels, sections, candidates] = await Promise.all([
      store.getAll('lease'), store.getAll('well'), store.getAll('available_parcel'), store.getAll('section_grid'), store.getCandidates(),
    ]);
    const layers = buildLayers(
      { leases: leases as LeaseRecord[], wells: wells as WellRecord[], parcels: parcels as BaseRecord[], sections: sections as Section[], candidates },
      loadConfig(), new Date().toISOString(),
    );
    return NextResponse.json({ constitution_version: CONSTITUTION_VERSION, layers });
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Failed to load layers';
    return NextResponse.json({ error: message, constitution_version: CONSTITUTION_VERSION }, { status: 500 });
  }
}
