// Hermes entrypoint:  npm run oa -- <job> [--dry-run]
// Jobs: ul_sync | blm_sync | glo_sync | rrc_sync | diff_engine | candidate_builder | alert_push | all
// Store: Supabase when SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY are set, else key-less local JSON (.data/open-acreage/state.json).
import { readFileSync, statSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { loadConfig } from '../../src/lib/open-acreage/config.ts';
import { CONSTITUTION_VERSION } from '../../src/lib/open-acreage/constitution.ts';
import { BLM_SERVICE, UL_LAYERS, decodeRRC, pullBLM, pullGLO, pullUL, type ConnectorContext, type RrcFile } from '../../src/lib/open-acreage/connectors.ts';
import { rebuildCandidates, runSource } from '../../src/lib/open-acreage/runner.ts';
import { FileStore, type Store } from '../../src/lib/open-acreage/store.ts';
import { storeFromEnv } from '../../src/lib/open-acreage/supabase-store.ts';
import { fetchDistinct, fetchLayerInfo } from '../../src/lib/open-acreage/arcgis.ts';
import { buildDigest, pushSlack } from '../../src/lib/open-acreage/digest.ts';

const [job = 'all', ...flags] = process.argv.slice(2);
const posArgs = flags.filter((f) => !f.startsWith('--'));
const dry = flags.includes('--dry-run');
const cfg = loadConfig();
const now = new Date().toISOString();
const store: Store = storeFromEnv() ?? new FileStore(process.env.OA_STATE_FILE ?? '.data/open-acreage/state.json');

async function ctx(urls: string[], source: 'UL' | 'BLM'): Promise<ConnectorContext> {
  const priorEdit: Record<string, string | null> = {};
  for (const u of urls) priorEdit[u] = flags.includes('--force') ? null : await store.getLayerEdit(u); // --force: ignore lastEditDate, re-pull
  const c = cfg.sources[source];
  return { fetch: globalThis.fetch, timeoutMs: c.timeoutMs, retries: c.retries, now, priorEdit };
}

/** RRC downloads: file names/links are UNCONFIRMED, so files are read from OA_RRC_DIR (<kind>.dat) placed by the download step. */
function rrcFiles(): RrcFile[] {
  const dir = process.env.OA_RRC_DIR; if (!dir || !existsSync(dir)) return [];
  return (['permit', 'production_oil', 'production_gas'] as const).filter((k) => existsSync(join(dir, `${k}.dat`)))
    .map((k) => ({ kind: k, url: `${process.env.OA_RRC_SOURCE_URL ?? 'https://www.rrc.texas.gov/resource-center/research/data-sets-available-for-download/'}#${k}`,
      bytes: readFileSync(join(dir, `${k}.dat`)), source_updated_at: statSync(join(dir, `${k}.dat`)).mtime.toISOString() }));
}

async function main() {
  console.log(`AMARA Open Acreage — job=${job} constitution=${CONSTITUTION_VERSION} store=${storeFromEnv() ? 'supabase' : 'local-file'}${dry ? ' DRY-RUN' : ''}`);
  const jobs = job === 'all' ? ['ul_sync', 'blm_sync', 'glo_sync', 'rrc_sync', 'candidate_builder', 'alert_push'] : [job];
  for (const j of jobs) {
    if (j === 'ul_sync') console.log(JSON.stringify(await runSource('UL', async () => pullUL(await ctx(UL_LAYERS.map((l) => l.path), 'UL')), store, cfg, now), null, 1));
    else if (j === 'blm_sync') console.log(JSON.stringify(await runSource('BLM', async () => pullBLM(await ctx([], 'BLM')), store, cfg, now), null, 1), BLM_SERVICE);
    else if (j === 'glo_sync') console.log(JSON.stringify(await runSource('GLO', pullGLO, store, cfg, now), null, 1));
    else if (j === 'rrc_sync') {
      const files = rrcFiles();
      console.log(JSON.stringify(await runSource('RRC', async () => files.length ? decodeRRC(files, { now }) : { source: 'RRC', state: 'NOT FOUND', layers: [], notes: ['no RRC files staged (OA_RRC_DIR empty) — download step not run / links unconfirmed'] }, store, cfg, now), null, 1));
    }
    // diff_engine runs inside runSource (after each promote); listed as a job for the scheduler's dependency graph.
    else if (j === 'services') {
      // Read-only: lists folders/services at a path on gis.blm.gov (default: the services root) to find the real lease service.
      const path = (posArgs[0] ?? 'nlsdb/rest/services').replace(/^\/+|\/+$/g, '');
      const res = await fetch(`https://gis.blm.gov/${path}?f=json`);
      const jj = await res.json().catch(() => null) as { folders?: string[]; services?: Array<{ name: string; type: string }>; error?: unknown } | null;
      console.log(`HTTP ${res.status} gis.blm.gov/${path}`);
      if (!jj) console.log('non-JSON response'); else {
        if (jj.error) console.log('error:', JSON.stringify(jj.error));
        console.log('folders:', (jj.folders ?? []).join(', ') || '(none)');
        for (const sv of jj.services ?? []) console.log(`service: ${sv.name} [${sv.type}]`);
      }
    }
    else if (j === 'values') {
      // Read-only: oa values <UL layer name> <field> — distinct values, to build status vocab from facts.
      const [layerName, field] = posArgs; const L = UL_LAYERS.find((x) => x.layer === layerName);
      if (!L || !field) { console.error('usage: npm run oa -- values <OG_Lease|WellSHL|Wellbore|AvailableParcels|UL_Section> <field>'); process.exitCode = 2; continue; }
      const info = await fetchLayerInfo(L.path, await ctx([], 'UL'));
      console.log((await fetchDistinct(info, field, await ctx([], 'UL'))).sort().join('\n'));
    }
    else if (j === 'discover') {
      // Read-only: prints each UL layer's real field names/types + one sample row so aliases can be set from facts.
      const c = await ctx([], 'UL');
      for (const L of UL_LAYERS) {
        const info = await fetchLayerInfo(L.path, c);
        const q = new URLSearchParams({ where: '1=1', outFields: '*', f: 'json', returnGeometry: 'false', resultRecordCount: '1' });
        const sample = await (await fetch(`${L.path}/query?${q}`)).json().catch(() => null);
        console.log(`\n== ${L.layer} (${L.entity}) lastEditDate=${info.lastEditDate} maxRecordCount=${info.maxRecordCount}`);
        console.log(info.fields.map((f) => `${f.name}:${f.type.replace('esriFieldType', '')}`).join('\n'));
        console.log('sample:', JSON.stringify(sample?.features?.[0]?.attributes ?? null));
      }
    }
    else if (j === 'diff_engine') console.log('diff_engine: executed inline by each *_sync run (change_event rows written on promote)');
    else if (j === 'candidate_builder') { const c = await rebuildCandidates(store, cfg, now); console.log(`candidates: ${c.length} (${c.filter((x) => x.status === 'HOLD').length} HOLD)`); }
    else if (j === 'alert_push') { const d = await buildDigest(store, cfg, now); console.log(d); console.log(dry ? 'dry-run: not sent' : JSON.stringify(await pushSlack(d))); }
    else { console.error(`unknown job ${j}`); process.exitCode = 2; }
  }
}
main().catch((e) => { console.error(e); process.exitCode = 1; });
