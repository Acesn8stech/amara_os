#!/usr/bin/env node
/**
 * Standalone tract-scoped instrument retrieval — no Postgres project
 * required ("forget workspaces for now"). Logs into the configured portal,
 * searches a survey tract, filters down to the requested subdivided-tract
 * numbers (e.g. 290, 497) using legal-description text since most portals
 * don't expose a native "tract" search field for this, and saves every
 * matching instrument as a PDF into a plain local output folder alongside a
 * manifest.json so the next pipeline stage (indexer) has something to read
 * without touching a database.
 *
 * Usage:
 *   tsx src/standalone/retrieveInstruments.ts \
 *     --county Howard --state TX --section 47 --block 33 --township T1S \
 *     --tracts 290,497 --out ./retrieved/sec47-blk33-t1s
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';
import { env, missingCredentialsFor } from '../config/env.js';
import { getPortalDriver } from '../browser/portals/index.js';
import { hitTouchesTract, type PortalSearchHit } from '../browser/types.js';

interface ManifestEntry {
  fileName: string;
  instrumentNumber: string | null;
  instrumentType: string | null;
  recordingDate: string | null;
  grantor: string | null;
  grantee: string | null;
  legalDescription: string | null;
  matchedTracts: string[];
  downloadStatus: 'downloaded' | 'failed';
  errorMessage?: string;
}

function flag(args: string[], name: string): string | undefined {
  const idx = args.indexOf(`--${name}`);
  return idx >= 0 ? args[idx + 1] : undefined;
}

function slug(hit: PortalSearchHit, index: number): string {
  const parts = [hit.recordingDate ?? 'undated', hit.instrumentNumber ?? `hit-${index}`, hit.instrumentType ?? 'instrument'];
  return parts.join('_').replace(/[^a-zA-Z0-9._-]+/g, '-').slice(0, 150);
}

function matchedTracts(hit: PortalSearchHit, tractNumbers: string[]): string[] {
  if (tractNumbers.length === 0) return [];
  return tractNumbers.filter((tract) => hitTouchesTract(hit, tract));
}

async function main() {
  const args = process.argv.slice(2);

  const county = flag(args, 'county') ?? env.defaultTract.county();
  const state = flag(args, 'state') ?? env.defaultTract.state();
  const section = flag(args, 'section') ?? env.defaultTract.section();
  const block = flag(args, 'block') ?? env.defaultTract.block();
  const township = flag(args, 'township') ?? env.defaultTract.township();
  const tractNumbers = (flag(args, 'tracts') ?? '').split(',').map((t) => t.trim()).filter(Boolean);
  const outDir = path.resolve(flag(args, 'out') ?? './retrieved');

  if (tractNumbers.length === 0) {
    throw new Error('Pass --tracts <comma-separated tract numbers>, e.g. --tracts 290,497');
  }

  const missing = missingCredentialsFor('portal');
  if (missing.length > 0) {
    throw new Error(`Cannot run instrument retrieval — missing ${missing.join(', ')}.`);
  }

  mkdirSync(outDir, { recursive: true });

  const driver = getPortalDriver(env.portal.name());
  const browser = await chromium.launch({ headless: env.portal.headless() });
  const manifest: ManifestEntry[] = [];

  try {
    const page = await browser.newPage();
    await driver.login(page);

    const hits = await driver.search(page, {
      state,
      county,
      surveySection: section,
      surveyBlock: block,
      surveyTownship: township,
      tractNumbers,
    });

    console.log(`Portal "${driver.name}" returned ${hits.length} hit(s) for ${county} County Sec ${section} Blk ${block} ${township}.`);

    const relevant = hits
      .map((hit, index) => ({ hit, index, tracts: matchedTracts(hit, tractNumbers) }))
      .filter(({ tracts }) => tracts.length > 0);

    console.log(`${relevant.length} hit(s) matched tract(s) ${tractNumbers.join(', ')} by legal description.`);

    if (relevant.length === 0 && hits.length > 0) {
      console.warn(
        'No hits matched by legal description — if this portal has no legal-description text on results rows, ' +
          'the tract filter needs a per-hit detail-page lookup instead. See hitTouchesTract() usage here and ' +
          'texasfile.ts\'s resultLegalDescription selector (marked TODO).',
      );
    }

    for (const { hit, index, tracts } of relevant) {
      const fileName = `${slug(hit, index)}.pdf`;
      try {
        const content = await driver.downloadInstrument(page, hit);
        writeFileSync(path.join(outDir, fileName), content);
        manifest.push({
          fileName,
          instrumentNumber: hit.instrumentNumber,
          instrumentType: hit.instrumentType,
          recordingDate: hit.recordingDate,
          grantor: hit.grantor,
          grantee: hit.grantee,
          legalDescription: hit.legalDescription,
          matchedTracts: tracts,
          downloadStatus: 'downloaded',
        });
        console.log(`  downloaded ${fileName}`);
      } catch (err) {
        manifest.push({
          fileName,
          instrumentNumber: hit.instrumentNumber,
          instrumentType: hit.instrumentType,
          recordingDate: hit.recordingDate,
          grantor: hit.grantor,
          grantee: hit.grantee,
          legalDescription: hit.legalDescription,
          matchedTracts: tracts,
          downloadStatus: 'failed',
          errorMessage: err instanceof Error ? err.message : String(err),
        });
        console.error(`  FAILED ${fileName}: ${err instanceof Error ? err.message : err}`);
      }
    }

    writeFileSync(path.join(outDir, 'manifest.json'), JSON.stringify(manifest, null, 2));
    console.log(`\nWrote ${manifest.length} manifest entries to ${path.join(outDir, 'manifest.json')}`);
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : err);
  process.exitCode = 1;
});
