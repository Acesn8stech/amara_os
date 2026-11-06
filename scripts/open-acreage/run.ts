// Hermes entrypoint:  npm run oa -- <job> [--dry-run]
// Jobs: ul_sync | blm_sync | glo_sync | rrc_sync | diff_engine | candidate_builder | alert_push | all
// Store: Supabase when SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY are set, else key-less local JSON (.data/open-acreage/state.json).
import { readFileSync, statSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { loadConfig } from '../../src/lib/open-acreage/config.ts';
import { CONSTITUTION_VERSION } from '../../src/lib/open-acreage/constitution.ts';
import { BLM_SERVICE, UL_LAYERS, decodeRRC, RRC_PAGE, pullBLM, pullGLO, pullUL, type ConnectorContext, type RrcFile } from '../../src/lib/open-acreage/connectors.ts';
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
    else if (j === 'rrc_links') {
      // Read-only: fetches the RRC data-sets page and prints links (+text) relevant to permits, production, wells and layouts.
      const res = await fetch(RRC_PAGE);
      console.log(`HTTP ${res.status} ${res.url}`);
      const html = await res.text();
      const seen = new Set<string>();
      // Group each link under the nearest preceding section id / heading so a dataset's files and manual stay together.
      const ids = [...html.matchAll(/\sid=["']([^"']+)["']/gi)].map((m) => ({ at: m.index ?? 0, v: m[1] }));
      const heads = [...html.matchAll(/<h[1-6][^>]*>([\s\S]*?)<\/h[1-6]>/gi)].map((m) => ({ at: m.index ?? 0, v: m[1].replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim() }));
      const before = (arr: Array<{ at: number; v: string }>, at: number) => { let v = ''; for (const x of arr) { if (x.at < at) v = x.v; else break; } return v; };
      let lastGroup = '';
      for (const m of html.matchAll(/<a\s[^>]*href=["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi)) {
        const text = m[2].replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim(); const href = new URL(m[1], res.url).toString();
        if (!/permit|production|wellbore|well|digital map|ebcdic|manual|layout|dictionary|record|ascii|pdq|\.(zip|txt|pdf|dat|csv|ebc)\b/i.test(`${text} ${href}`) || seen.has(href)) continue;
        seen.add(href);
        const group = `${before(heads, m.index ?? 0) || '?'} [#${before(ids, m.index ?? 0) || '?'}]`;
        if (group !== lastGroup) { console.log(`\n## ${group}`); lastGroup = group; }
        console.log(`  ${text || '(no text)'} -> ${href}`);
      }
      console.log(`${seen.size} links`);
    }
    else if (j === 'services') {
      // Read-only: lists folders/services at a path on gis.blm.gov (default: the services root) to find the real lease service.
      const path = (posArgs[0] ?? 'nlsdb/rest/services').replace(/^\/+|\/+$/g, '');
      const res = await fetch(`https://gis.blm.gov/${path}?f=json`);
      const jj = await res.json().catch(() => null) as { folders?: string[]; services?: Array<{ name: string; type: string }>; layers?: Array<{ id: number; name: string }>; tables?: Array<{ id: number; name: string }>; capabilities?: string; maxRecordCount?: number; serviceDescription?: string; error?: unknown } | null;
      console.log(`HTTP ${res.status} requested gis.blm.gov/${path} | final url: ${res.url} | redirected: ${res.redirected} | content-type: ${res.headers.get('content-type')}`);
      if (!jj) console.log('non-JSON response'); else {
        if (jj.error) console.log('error:', JSON.stringify(jj.error));
        console.log('folders:', (jj.folders ?? []).join(', ') || '(none)');
        for (const sv of jj.services ?? []) console.log(`service: ${sv.name} [${sv.type}]`);
        if (jj.layers || jj.tables) {
          console.log(`capabilities=${jj.capabilities ?? 'n/a'} maxRecordCount=${jj.maxRecordCount ?? 'n/a'}`);
          if (jj.serviceDescription) console.log('description:', jj.serviceDescription.slice(0, 300));
          for (const l of jj.layers ?? []) console.log(`layer ${l.id}: ${l.name}`);
          for (const l of jj.tables ?? []) console.log(`table ${l.id}: ${l.name}`);
        }
      }
    }
    else if (j === 'values') {
      // Read-only: oa values <UL layer name> <field> — distinct values, to build status vocab from facts.
      const [layerName, field] = posArgs; const L = UL_LAYERS.find((x) => x.layer === layerName);
      if (!L || !field) { console.error('usage: npm run oa -- values <OG_Lease|WellSHL|Wellbore|AvailableParcels|UL_Section> <field>'); process.exitCode = 2; continue; }
      const info = await fetchLayerInfo(L.path, await ctx([], 'UL'));
      console.log((await fetchDistinct(info, field, await ctx([], 'UL'))).sort().join('\n'));
    }
    else if (j === 'discover' && posArgs[0] === 'blm') {
      // Read-only: BLM service layers, fields and one sample row.
      const svc = process.env.OA_BLM_SERVICE ?? BLM_SERVICE; const c = await ctx([], 'BLM');
      const root = await (await fetch(`${svc}?f=json`)).json() as { layers?: Array<{ id: number; name: string }>; capabilities?: string };
      console.log(`service ${svc}\ncapabilities=${root.capabilities ?? 'n/a'} layers=${(root.layers ?? []).map((l) => `${l.id}:${l.name}`).join(', ') || 'NONE'}`);
      for (const l of root.layers ?? []) {
        const info = await fetchLayerInfo(`${svc}/${l.id}`, c);
        const q = new URLSearchParams({ where: '1=1', outFields: '*', f: 'json', returnGeometry: 'false', resultRecordCount: '1' });
        const sample = await (await fetch(`${svc}/${l.id}/query?${q}`)).json().catch(() => null) as { features?: Array<{ attributes?: unknown }> } | null;
        console.log(`\n== layer ${l.id} ${l.name} lastEditDate=${info.lastEditDate} maxRecordCount=${info.maxRecordCount}`);
        console.log(info.fields.map((f) => `${f.name}:${f.type.replace('esriFieldType', '')}`).join('\n'));
        console.log('sample:', JSON.stringify(sample?.features?.[0]?.attributes ?? null));
      }
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
    else if (j === 'candidate_builder') {
      const c = await rebuildCandidates(store, cfg, now); console.log(`candidates: ${c.length} (${c.filter((x) => x.status === 'HOLD').length} HOLD)`);
      // Coverage: a target county with no section grid cannot be evaluated at all (NOT FOUND, not "zero").
      const secs = await store.getAll('section_grid'); const norm = (x: unknown) => String(x ?? '').toLowerCase().replace(/\s+county$/, '').trim();
      for (const t of cfg.targetCounties) { const n = secs.filter((s) => norm(s.county) === norm(t)).length; console.log(`  ${t}: ${n} UL sections, ${c.filter((x) => norm(x.county) === norm(t)).length} candidate tracts${n === 0 ? ' — NO SECTION COVERAGE (NOT FOUND)' : ''}`); }
    }
    else if (j === 'alert_push') { const d = await buildDigest(store, cfg, now); console.log(d); console.log(dry ? 'dry-run: not sent' : JSON.stringify(await pushSlack(d))); }
    else { console.error(`unknown job ${j}`); process.exitCode = 2; }
  }
}
main().catch((e) => { console.error(e); process.exitCode = 1; });
