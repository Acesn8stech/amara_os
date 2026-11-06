/* eslint-disable @typescript-eslint/no-explicit-any -- untyped external JSON payloads */
import { fetchAllFeatures, fetchDistinct, fetchLayerInfo, type FetchOpts, type LayerInfo } from './arcgis.ts';
import { dateField, normalizeStatus, num, pick, str, toGeometry } from './normalize.ts';
import { getJson, withRetry } from './http.ts';
import { decodeFile, type RecordLayout } from './fixedwidth.ts';
import { RRC_LAYOUTS } from './rrc-layouts.ts';
import type { BaseRecord, EntityType, LeaseRecord, SourceState } from './types.ts';

export interface ConnectorContext extends FetchOpts {
  now: string;
  /** lastEditDate seen at the previous good pull, keyed by layer url. */
  priorEdit: Record<string, string | null>;
}

export interface LayerPull {
  layer: string;
  url: string;
  entity_type: EntityType;
  skipped_unchanged: boolean;
  source_updated_at: string | null;
  records: BaseRecord[];
  unmapped: string[]; // canonical fields no source field matched => NULL (Celilo)
  fields_discovered: string[];
}

export interface ConnectorResult {
  source: 'UL' | 'BLM' | 'GLO' | 'RRC';
  state: SourceState;
  layers: LayerPull[];
  notes: string[];
}

// ------------------------------------------------------------------ UL
const UL_BASE = 'https://services3.arcgis.com/8jYUORGmDUL39WkJ/arcgis/rest/services';
export const UL_LAYERS: Array<{ layer: string; path: string; entity: EntityType }> = [
  { layer: 'UL_Section', path: `${UL_BASE}/UL_Section/FeatureServer/46`, entity: 'section_grid' },
  { layer: 'OG_Lease', path: `${UL_BASE}/OG_Lease/FeatureServer/0`, entity: 'lease' },
  { layer: 'AvailableParcels', path: `${UL_BASE}/AvailableParcels/FeatureServer/0`, entity: 'available_parcel' },
  { layer: 'WellSHL', path: `${UL_BASE}/WellSHL/FeatureServer/0`, entity: 'well' },
  { layer: 'Wellbore', path: `${UL_BASE}/Wellbore/FeatureServer/0`, entity: 'well' },
];

// Alias lists are lookup candidates only. UL/BLM field names were NOT verified in the build sandbox:
// whatever does not match stays NULL and is reported in `unmapped` — never guessed.
const A = {
  id: ['LEASE_ID', 'LEASE_NO', 'LEASENUMBER', 'LEASE_NUMBER', 'API', 'API_NO', 'PARCEL_ID', 'SECTION_ID', 'GlobalID', 'GLOBALID'],
  leaseNo: ['LEASE_NO', 'LEASE_NUMBER', 'LEASENUMBER', 'LEASE_ID', 'LEASE', 'CASE_SERIAL_NUMBER', 'CSE_NR', 'LEASE_NUM', 'UTLeaseNbr'],
  lessee: ['LESSEE', 'LESSEE_NAME', 'OPERATOR', 'CUSTOMER_NAME', 'CUST_NAME', 'MapLesseeName', 'OriginalLessee', 'CurrentOwner'],
  status: ['STATUS', 'LEASE_STATUS', 'LSE_STATUS', 'CSE_DISP', 'CASE_DISPOSITION', 'DISPOSITION', 'WELL_STATUS', 'LeaseStatus', 'WellboreStatusCode', 'ZoneStatus'],
  eff: ['EFFECTIVE_DATE', 'EFF_DATE', 'LEASE_DATE', 'LEASE_EFF_DATE', 'CSE_EFF_DATE', 'EFFECTIVE_DT', 'START_DATE', 'DateBeginning'],
  exp: ['EXPIRATION_DATE', 'EXP_DATE', 'EXPIRY_DATE', 'LEASE_EXP_DATE', 'CSE_EXP_DATE', 'PRIMARY_TERM_END', 'EXPIRES'],
  acres: ['ACRES', 'ACREAGE', 'GIS_ACRES', 'LEASE_ACRES', 'CSE_ACRES', 'TOTAL_ACRES', 'MineralAcres', 'CalcAcres'],
  county: ['COUNTY', 'COUNTY_NAME', 'CNTY', 'CNTY_NAME'],
  state: ['STATE', 'STATE_ABBR', 'ST', 'ADMIN_ST', 'ADM_ST', 'STATE_CODE', 'STATE_NAME'],
  api: ['API', 'API_NO', 'API_NUMBER', 'API14', 'API10'],
  operator: ['OPERATOR', 'OPERATOR_NAME', 'OPER_NAME', 'CompanyName'],
  spud: ['SPUD_DATE', 'SPUD', 'SpudDate'],
  completion: ['COMPLETION_DATE', 'COMP_DATE', 'CompletionDate'],
  plug: ['PLUG_DATE', 'PLUGGED_DATE'],
  leaseName: ['LEASE_NAME', 'LEASE'],
  field: ['FIELD', 'FIELD_NAME', 'FieldName'],
  label: ['SECTION', 'SECTION_NAME', 'LABEL', 'BLOCK_SECTION', 'NAME'],
};

function recordId(props: Record<string, unknown>, info: LayerInfo): string | null {
  // The service's own OID is the one identifier we know exists structurally (from layer metadata).
  if (info.objectIdField && props[info.objectIdField] !== undefined && props[info.objectIdField] !== null) return String(props[info.objectIdField]);
  return null;
}

function mapFeature(entity: EntityType, source: 'UL' | 'BLM', lessor: LeaseRecord['lessor_type'] | null, layer: string,
  f: { properties: Record<string, unknown>; geometry: unknown }, info: LayerInfo, ctx: ConnectorContext): BaseRecord {
  const p = f.properties;
  const base = {
    source_name: source, source_url: info.url, source_layer: layer, source_record_id: recordId(p, info),
    source_updated_at: info.lastEditDate, retrieved_at: ctx.now, raw: p, geom: toGeometry(f.geometry),
  };
  switch (entity) {
    case 'lease': {
      const status_raw = str(p, A.status);
      return { ...base, entity_type: 'lease', lessor_type: lessor!, lease_no: str(p, A.leaseNo), lessee: str(p, A.lessee),
        status_raw, status_normalized: normalizeStatus(status_raw), effective_date: dateField(p, A.eff),
        expiration_date: dateField(p, A.exp), acres: num(p, A.acres), county: str(p, A.county), state: str(p, A.state) } as LeaseRecord;
    }
    case 'well': {
      const lat = num(p, ['Latitude']); const lon = num(p, ['Longitude']);
      if (!base.geom && lat !== null && lon !== null) base.geom = { type: 'Point', coordinates: [lon, lat] }; // source-stated coordinates, not inferred
      return { ...base, entity_type: 'well', api_no: str(p, A.api), operator: str(p, A.operator), status: str(p, A.status),
        spud_date: dateField(p, A.spud), completion_date: dateField(p, A.completion), plug_date: dateField(p, A.plug),
        lease_name: str(p, A.leaseName), field: str(p, A.field), county: str(p, A.county), state: str(p, A.state) };
    }
    case 'available_parcel':
      return { ...base, entity_type: 'available_parcel', county: str(p, A.county), state: str(p, A.state), acres: num(p, A.acres) };
    default: {
      const blk = str(p, ['blocknum']); const sec = str(p, ['sectionnum']);
      const label = blk && sec ? `Block ${blk} Section ${sec}` : str(p, A.label);
      return { ...base, entity_type: 'section_grid', county: str(p, A.county), state: str(p, A.state), label };
    }
  }
}

const CANON: Record<string, Array<[string, string[]]>> = {
  lease: [['lease_no', A.leaseNo], ['lessee', A.lessee], ['status', A.status], ['effective_date', A.eff], ['expiration_date', A.exp], ['acres', A.acres], ['county', A.county]],
  well: [['api_no', A.api], ['operator', A.operator], ['status', A.status], ['county', A.county]],
  available_parcel: [['county', A.county], ['acres', A.acres]],
  section_grid: [['county', A.county], ['label', [...A.label, 'sectionnum']]],
};

function unmappedFor(entity: EntityType, info: LayerInfo): string[] {
  const names = info.fields.map((f) => f.name);
  const probe = Object.fromEntries(names.map((n) => [n, 'x']));
  return (CANON[entity] ?? []).filter(([, aliases]) => !pick(probe, aliases)).map(([c]) => c);
}

export async function pullUL(ctx: ConnectorContext, only?: string[]): Promise<ConnectorResult> {
  const layers: LayerPull[] = []; const notes = ['Terms of use: NOT FOUND on UL GIS page — do not redistribute data publicly.'];
  let failures = 0;
  for (const L of UL_LAYERS.filter((l) => !only || only.includes(l.layer))) {
    try {
      const info = await fetchLayerInfo(L.path, ctx);
      const prior = ctx.priorEdit[L.path];
      if (info.lastEditDate && prior === info.lastEditDate) {
        layers.push({ layer: L.layer, url: L.path, entity_type: L.entity, skipped_unchanged: true, source_updated_at: info.lastEditDate, records: [], unmapped: [], fields_discovered: info.fields.map((f) => f.name) });
        continue;
      }
      if (!info.lastEditDate) notes.push(`${L.layer}: service does not state editingInfo.lastEditDate — source_updated_at = NULL, full pull every run`);
      const feats = await fetchAllFeatures(info, ctx);
      layers.push({ layer: L.layer, url: L.path, entity_type: L.entity, skipped_unchanged: false, source_updated_at: info.lastEditDate,
        records: feats.map((f) => mapFeature(L.entity, 'UL', 'UNIVERSITY', L.layer, f, info, ctx)),
        unmapped: unmappedFor(L.entity, info), fields_discovered: info.fields.map((f) => f.name) });
    } catch (e) { failures++; notes.push(`${L.layer}: ${(e as Error).message}`); }
  }
  const state: SourceState = failures === 0 ? 'OK' : layers.length ? 'UNVERIFIED' : 'UNREACHABLE';
  return { source: 'UL', state, layers, notes };
}

// ------------------------------------------------------------------ BLM
export const BLM_SERVICE = 'https://gis.blm.gov/nlsdb/rest/services/HUB/BLM_Natl_MLRS_Oil_and_Gas_Leases/FeatureServer';

export async function pullBLM(ctx: ConnectorContext): Promise<ConnectorResult> {
  const notes: string[] = []; const layers: LayerPull[] = [];
  let svc: any;
  try { svc = await withRetry(() => getJson(ctx.fetch, `${BLM_SERVICE}?f=json`, ctx.timeoutMs), ctx.retries, 1000, ctx.sleep); }
  catch (e) { return { source: 'BLM', state: 'UNREACHABLE', layers, notes: [`service listing failed: ${(e as Error).message}`] }; }

  let all: Array<{ id: number; name: string }> = (svc.layers ?? []).map((l: any) => ({ id: l.id, name: String(l.name) }));
  if (!all.length) {
    // Root listing had no `layers` array. Record what it did return, then try the standard /layers endpoint.
    notes.push(`BLM service root returned no layers array; keys=[${Object.keys(svc ?? {}).join(',')}]${svc?.error ? ` error=${JSON.stringify(svc.error)}` : ''}${svc?.serviceDescription ? ` desc=${String(svc.serviceDescription).slice(0, 120)}` : ''}`);
    try {
      const j = await withRetry(() => getJson(ctx.fetch, `${BLM_SERVICE}/layers?f=json`, ctx.timeoutMs), ctx.retries, 1000, ctx.sleep);
      all = [...(j.layers ?? []), ...(j.tables ?? [])].map((l: any) => ({ id: l.id, name: String(l.name) }));
      notes.push(`BLM /layers endpoint returned ${all.length} layer(s)`);
    } catch (e) { notes.push(`BLM /layers fallback failed: ${(e as Error).message}`); }
  }
  // A single-layer service named for oil & gas leases needs no name match; otherwise require /lease/ in the layer name.
  const leaseLayers = all.length === 1 ? all : all.filter((l) => /lease/i.test(l.name));
  notes.push(`BLM layers discovered: ${all.map((l) => `${l.id}:${l.name}`).join(', ') || 'none'}`);
  if (!leaseLayers.length) return { source: 'BLM', state: 'UNVERIFIED', layers, notes: [...notes, all.length ? 'No layer name matched /lease/ — cannot choose a layer without guessing; Scott to pick layer id.' : 'Service exposes no layers — cannot pull.'] };

  let failures = 0;
  for (const L of leaseLayers) {
    const url = `${BLM_SERVICE}/${L.id}`;
    try {
      const info = await fetchLayerInfo(url, ctx);
      if (info.lastEditDate && ctx.priorEdit[url] === info.lastEditDate) {
        layers.push({ layer: L.name, url, entity_type: 'lease', skipped_unchanged: true, source_updated_at: info.lastEditDate, records: [], unmapped: [], fields_discovered: info.fields.map((f) => f.name) });
        continue;
      }
      const probe = Object.fromEntries(info.fields.map((f) => [f.name, 'x']));
      const stateField = pick(probe, A.state)?.field;
      if (!stateField) { failures++; notes.push(`${L.name}: no state field discovered — NM/TX filter cannot be applied without guessing (UNVERIFIED)`); continue; }
      const distinct = await fetchDistinct(info, stateField, ctx);
      const wanted = distinct.filter((v) => /^(nm|tx|new mexico|texas)$/i.test(v.trim()));
      if (!wanted.length) { failures++; notes.push(`${L.name}: state field ${stateField} has no NM/TX values (saw ${distinct.slice(0, 10).join('|')})`); continue; }
      const where = `${stateField} IN (${wanted.map((v) => `'${v.replace(/'/g, "''")}'`).join(',')})`;
      notes.push(`${L.name}: state filter discovered: ${where}`);
      const feats = await fetchAllFeatures(info, ctx, where);
      layers.push({ layer: L.name, url, entity_type: 'lease', skipped_unchanged: false, source_updated_at: info.lastEditDate,
        records: feats.map((f) => mapFeature('lease', 'BLM', 'FEDERAL', L.name, f, info, ctx)),
        unmapped: unmappedFor('lease', info), fields_discovered: info.fields.map((f) => f.name) });
    } catch (e) { failures++; notes.push(`${L.name}: ${(e as Error).message}`); }
  }
  return { source: 'BLM', state: failures === 0 ? 'OK' : layers.length ? 'UNVERIFIED' : 'UNREACHABLE', layers, notes };
}

// ------------------------------------------------------------------ GLO
/** Stub: endpoint NOT confirmed. Do not substitute another source. Do NOT ingest the IFS-licensed OTLS land grid. */
export async function pullGLO(): Promise<ConnectorResult> {
  return { source: 'GLO', state: 'NOT FOUND', layers: [], notes: [
    'GLO GIS page (https://www.glo.texas.gov/land/gis-maps-and-data) lists Oil & Gas Leases Active/Inactive and Lease Sale Nominated Tracts, but no REST service or direct download URL could be confirmed from the build sandbox. Connector stubbed.',
    'OTLS land grid intentionally NOT ingested (IFS-licensed; GLO not authorized to distribute).',
  ] };
}

// ------------------------------------------------------------------ RRC
export const RRC_PAGE = 'https://www.rrc.texas.gov/resource-center/research/data-sets-available-for-download/';

export interface RrcFile { kind: 'permit' | 'production_oil' | 'production_gas'; url: string; bytes: Uint8Array; source_updated_at: string | null }

/** Decode already-downloaded RRC files with verified layouts. Downloads are done by the Hermes job (file names/links unconfirmed). */
export function decodeRRC(files: RrcFile[], ctx: Pick<ConnectorContext, 'now'>, layouts: Record<string, RecordLayout> = RRC_LAYOUTS): ConnectorResult {
  const notes: string[] = []; const layers: LayerPull[] = []; let failed = 0;
  for (const f of files) {
    const layout = layouts[f.kind];
    try {
      const { rows, trailingBytes } = decodeFile(f.bytes, layout);
      if (trailingBytes) notes.push(`${f.kind}: ${trailingBytes} trailing bytes not a full record`);
      const entity: EntityType = f.kind === 'permit' ? 'permit' : 'production_monthly';
      const records = rows.map((r): BaseRecord => {
        const base = { source_name: 'RRC', source_url: f.url, source_layer: f.kind, source_updated_at: f.source_updated_at, retrieved_at: ctx.now, raw: r };
        if (entity === 'permit') {
          const lat = typeof r.lat === 'number' ? r.lat : null; const lon = typeof r.lon === 'number' ? r.lon : null;
          return { ...base, entity_type: 'permit', source_record_id: r.permit_no != null ? String(r.permit_no) : null, permit_no: r.permit_no != null ? String(r.permit_no) : null,
            issued_date: parseYmd(r.issued_date), lease_name: (r.lease_name as string) ?? null, county: (r.county as string) ?? null,
            geom: lat != null && lon != null ? { type: 'Point', coordinates: [lon, lat] } : null };
        }
        const id = r.well_or_lease_id != null ? String(r.well_or_lease_id) : null; const month = parseYm(r.month);
        return { ...base, entity_type: 'production_monthly', source_record_id: id && month ? `${f.kind}:${id}:${month}` : null, well_or_lease_id: id, month,
          oil_bbl: typeof r.oil_bbl === 'number' ? r.oil_bbl : null, gas_mcf: typeof r.gas_mcf === 'number' ? r.gas_mcf : null, geom: null };
      });
      layers.push({ layer: f.kind, url: f.url, entity_type: entity, skipped_unchanged: false, source_updated_at: f.source_updated_at, records, unmapped: [], fields_discovered: layout.fields.map((x) => x.name) });
    } catch (e) { failed++; notes.push(`${f.kind}: ${(e as Error).message}`); }
  }
  return { source: 'RRC', state: failed === 0 && layers.length ? 'OK' : 'UNVERIFIED', layers, notes };
}
const parseYmd = (v: unknown) => { const s = v == null ? '' : String(v); return /^\d{8}$/.test(s) ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}` : null; };
const parseYm = (v: unknown) => { const s = v == null ? '' : String(v); return /^\d{6}$/.test(s) ? `${s.slice(0, 4)}-${s.slice(4, 6)}-01` : null; };
