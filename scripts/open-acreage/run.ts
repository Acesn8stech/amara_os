// Hermes entrypoint:  npm run oa -- <job> [--dry-run]
// Jobs: ul_sync | blm_sync | glo_sync | rrc_sync | diff_engine | candidate_builder | alert_push | all
// Store: Supabase when SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY are set, else key-less local JSON (.data/open-acreage/state.json).
import { readFileSync, readdirSync, statSync, existsSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { loadConfig } from '../../src/lib/open-acreage/config.ts';
import { CANDIDATE_STATUS, CONSTITUTION_VERSION } from '../../src/lib/open-acreage/constitution.ts';
import { BLM_SERVICE, UL_LAYERS, decodeRRC, RRC_PAGE, pullBLM, pullGLO, pullUL, type ConnectorContext, type ConnectorResult, type RrcFile } from '../../src/lib/open-acreage/connectors.ts';
import { rebuildCandidates, runSource } from '../../src/lib/open-acreage/runner.ts';
import { FileStore, type Store } from '../../src/lib/open-acreage/store.ts';
import { storeFromEnv } from '../../src/lib/open-acreage/supabase-store.ts';
import { fetchDistinct, fetchLayerInfo } from '../../src/lib/open-acreage/arcgis.ts';
import { PDQ_LEASE_CYCLE, PDQ_WELL_COMPLETION, api8, combineRRC, pdqLeaseApiMap, pullPdq, unzipLines, zipMemberDate } from '../../src/lib/open-acreage/pdq.ts';
import { pickLatestPending, pullPending } from '../../src/lib/open-acreage/pending.ts';
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
      console.log(JSON.stringify(await runSource('RRC', async () => {
        const parts: ConnectorResult[] = []; const dir = process.env.OA_RRC_DIR;
        // Permits / EBCDIC production: fixed-width decode (layouts must be verified, else the decoder refuses).
        const files = rrcFiles();
        if (files.length) parts.push(decodeRRC(files, { now }, undefined, files.map((f) => f.kind)));
        // Pending drilling permits: newest run in OA_RRC_DIR/pending that has both dp_drilling_permit_* and dp_wellbore_* files.
        const pdir = dir ? join(dir, 'pending') : '';
        if (pdir && existsSync(pdir)) {
          const latest = pickLatestPending(readdirSync(pdir));
          if (latest) parts.push(pullPending({ permitText: readFileSync(join(pdir, latest.permitFile), 'utf8'), wellboreText: readFileSync(join(pdir, latest.wellboreFile), 'utf8'), ...latest }, now));
        }
        // Production Data Query Dump: PDQ_DSV.zip, read by column name; attributed to our wells by 8-digit API.
        const zip = dir ? join(dir, 'PDQ_DSV.zip') : '';
        if (zip && existsSync(zip)) {
          const wanted = new Set<string>(); for (const w of await store.getAll('well')) { const a = api8(w.api_no); if (a) wanted.add(a); }
          const d = new Date(now); d.setUTCMonth(d.getUTCMonth() - (cfg.productionMonths + 2));
          const cutoffYm = `${d.getUTCFullYear()}${String(d.getUTCMonth() + 1).padStart(2, '0')}`;
          parts.push(await pullPdq({ wellCompletion: unzipLines(zip, PDQ_WELL_COMPLETION), leaseCycle: unzipLines(zip, PDQ_LEASE_CYCLE) },
            { wantedApi8: wanted, cutoffYm, now, sourceUrl: `${RRC_PAGE}#production-data-query-dump`, sourceUpdatedAt: (await zipMemberDate(zip, PDQ_LEASE_CYCLE)) ?? statSync(zip).mtime.toISOString() }));
        }
        if (!parts.length) return { source: 'RRC' as const, state: 'NOT FOUND' as const, layers: [], notes: ['no RRC files staged (OA_RRC_DIR: permit.dat and/or PDQ_DSV.zip) — download step not run'] };
        return combineRRC(parts);
      }, store, cfg, now), null, 1));
    }
    // diff_engine runs inside runSource (after each promote); listed as a job for the scheduler's dependency graph.
    else if (j === 'candidates_csv') {
      // Read-only export of the current candidate list for review. These are leads, never "open": each needs a courthouse title check.
      const secs = new Map((await store.getAll('section_grid')).map((s) => [`${s.source_name}:${s.source_record_id}`, s]));
      const q = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""')}"`;
      const rows = (await store.getCandidates()).map((c) => { const s = secs.get(c.tract_id); return [c.status, c.county, s?.label ?? '', c.tract_id, c.reason_codes.join(';'), s?.source_updated_at ?? ''].map(q).join(','); });
      const out = process.env.OA_CSV ?? '.data/open-acreage/candidates.csv';
      writeFileSync(out, ['status,county,section,tract_id,reason_codes,source_updated_at', ...rows].join('\n') + '\n');
      console.log(`wrote ${rows.length} rows to ${out} (constitution ${CONSTITUTION_VERSION}; ${CANDIDATE_STATUS} — not open/leasable)`);
    }
    else if (j === 'rrc_link_report') {
      // Read-only: which UL wells have no RRC lease link in OG_WELL_COMPLETION, broken down so we can see what they are.
      const zip = join(process.env.OA_RRC_DIR ?? '', 'PDQ_DSV.zip'); if (!existsSync(zip)) { console.error('OA_RRC_DIR/PDQ_DSV.zip not found'); process.exitCode = 2; continue; }
      const wells = await store.getAll('well'); const wanted = new Set<string>(); for (const w of wells) { const a = api8(w.api_no); if (a) wanted.add(a); }
      const linked = new Set<string>(); for (const apis of (await pdqLeaseApiMap(unzipLines(zip, PDQ_WELL_COMPLETION), wanted)).values()) for (const a of apis) linked.add(a);
      const tally = (f: (w: (typeof wells)[number]) => string) => { const c: Record<string, [number, number]> = {}; for (const w of wells) { const a = api8(w.api_no); if (!a) continue; const k = f(w); c[k] ??= [0, 0]; c[k][linked.has(a) ? 0 : 1]++; } return Object.fromEntries(Object.entries(c).sort((x, y) => y[1][1] - x[1][1]).slice(0, 15).map(([k, v]) => [k, `linked ${v[0]} / unlinked ${v[1]}`])); };
      console.log(`wells with a usable API: ${[...wanted].length} distinct; linked ${linked.size}; unlinked ${wanted.size - linked.size}`);
      console.log('by source layer:', tally((w) => w.source_layer)); console.log('by status:', tally((w) => String(w.status ?? 'NULL')));
      console.log('by completion year:', tally((w) => String(w.completion_date ?? 'NULL').slice(0, 4)));
      console.log('by spud year:', tally((w) => String(w.spud_date ?? 'NULL').slice(0, 4)));
      console.log('by operator:', tally((w) => String(w.operator ?? 'NULL')));
    }
    else if (j === 'rrc_links') {
      // Read-only: fetches the RRC data-sets page and prints links (+text) relevant to permits, production, wells and layouts.
      const res = await fetch(RRC_PAGE);
      console.log(`HTTP ${res.status} ${res.url}`);
      const html = await res.text();
      // Preferred output: one block per table row, with the row's visible title text, so each dataset stays with its own files/manual.
      const rows = [...html.matchAll(/<tr[\s\S]*?<\/tr>/gi)].map((m) => m[0]);
      let printed = 0;
      for (const row of rows) {
        const links = [...row.matchAll(/<a\s[^>]*href=["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi)].map((m) => ({ text: m[2].replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim(), href: new URL(m[1], res.url).toString() }))
          .filter((l) => /mft\.rrc|\.pdf|\.zip|\.csv/i.test(l.href));
        if (!links.length) continue;
        const title = row.replace(/<a\s[\s\S]*?<\/a>/gi, ' ').replace(/<[^>]+>/g, ' ').replace(/&nbsp;/g, ' ').replace(/\s+/g, ' ').trim();
        console.log(`\nROW: ${title.slice(0, 400)}`);
        for (const l of links) console.log(`  ${l.text || '(no text)'} -> ${l.href}`);
        printed++;
      }
      console.log(`\n${printed} table rows with file links${printed ? '' : ' (none: page has no <tr> rows; falling back to grouped links below)'}`);
      if (printed) continue;
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
