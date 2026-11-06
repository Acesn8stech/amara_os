import { NextRequest, NextResponse } from 'next/server';
import { loadConfig } from '@/lib/open-acreage/config';
import { CONSTITUTION_VERSION } from '@/lib/open-acreage/constitution';
import { storeFromEnv } from '@/lib/open-acreage/supabase-store';
import { answer, parseIntent } from '@/lib/open-acreage/voice';
import type { LeaseRecord } from '@/lib/open-acreage/types';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

/** Voice intents: "What's new in X County?", "Show me expiring leases in X County", "How many open acreage candidates in X County?" */
export async function POST(request: NextRequest) {
  try {
    const body = (await request.json()) as { utterance?: string };
    if (!body.utterance || typeof body.utterance !== 'string') {
      return NextResponse.json({ error: 'Request body must contain an "utterance" string' }, { status: 400 });
    }
    const intent = parseIntent(body.utterance);
    const store = storeFromEnv();
    if (!store) {
      return NextResponse.json({ intent, answer: `UNVERIFIED: open acreage store is not configured. Constitution ${CONSTITUTION_VERSION}.` }, { status: 503 });
    }
    const [leases, candidates, changes, runs, sections] = await Promise.all([
      store.getAll('lease'), store.getCandidates(), store.getChanges(), store.getRuns(), store.getAll('section_grid'),
    ]);
    const all = [...leases, ...sections, ...(await store.getAll('well')), ...(await store.getAll('available_parcel'))];
    const county = new Map(all.map((r) => [`${r.source_name}:${r.source_record_id}`, (r.county as string | null) ?? null]));
    const text = answer(intent, { leases: leases as LeaseRecord[], candidates, changes, runs, countyOfEntity: (id) => county.get(id) ?? null }, loadConfig(), new Date().toISOString());
    return NextResponse.json({ intent, answer: text, constitution_version: CONSTITUTION_VERSION });
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Voice query failed';
    return NextResponse.json({ error: message }, { status: 500 });
  }
}
