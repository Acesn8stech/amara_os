/* eslint-disable @typescript-eslint/no-explicit-any -- untyped external JSON payloads */
import { getJson, withRetry, type Fetcher } from './http.ts';

export interface LayerInfo {
  url: string;
  name: string | null;
  fields: Array<{ name: string; type: string }>;
  objectIdField: string | null;
  maxRecordCount: number;
  lastEditDate: string | null; // ISO from editingInfo.lastEditDate; null if service does not state it
  raw: unknown;
}

export interface FetchOpts { fetch: Fetcher; timeoutMs: number; retries: number; sleep?: (ms: number) => Promise<void> }

export async function fetchLayerInfo(layerUrl: string, o: FetchOpts): Promise<LayerInfo> {
  const j = await withRetry(() => getJson(o.fetch, `${layerUrl}?f=json`, o.timeoutMs), o.retries, 1000, o.sleep);
  const led = j?.editingInfo?.lastEditDate;
  const fields = Array.isArray(j.fields) ? j.fields.map((f: any) => ({ name: String(f.name), type: String(f.type) })) : [];
  const oid = fields.find((f: any) => f.type === 'esriFieldTypeOID')?.name ?? j.objectIdField ?? null;
  return {
    url: layerUrl,
    name: j.name ?? null,
    fields,
    objectIdField: oid,
    maxRecordCount: Number.isFinite(j.maxRecordCount) && j.maxRecordCount > 0 ? j.maxRecordCount : 1000,
    lastEditDate: typeof led === 'number' && led > 0 ? new Date(led).toISOString() : null,
    raw: j,
  };
}

export interface RawFeature { properties: Record<string, unknown>; geometry: unknown }

/** Paginates by resultOffset, never exceeding the layer's maxRecordCount. `where` must come from discovered fields. */
export async function fetchAllFeatures(info: LayerInfo, o: FetchOpts, where = '1=1'): Promise<RawFeature[]> {
  const out: RawFeature[] = [];
  const size = info.maxRecordCount;
  for (let offset = 0; ; offset += size) {
    const q = new URLSearchParams({
      where, outFields: '*', f: 'geojson', outSR: '4326', returnGeometry: 'true',
      resultOffset: String(offset), resultRecordCount: String(size),
      ...(info.objectIdField ? { orderByFields: info.objectIdField } : {}),
    });
    const j = await withRetry(() => getJson(o.fetch, `${info.url}/query?${q}`, o.timeoutMs), o.retries, 1000, o.sleep);
    const feats: any[] = Array.isArray(j.features) ? j.features : [];
    for (const f of feats) out.push({ properties: f.properties ?? {}, geometry: f.geometry ?? null });
    const more = j.exceededTransferLimit === true || j.properties?.exceededTransferLimit === true || feats.length >= size;
    if (!feats.length || !more) break;
  }
  return out;
}

/** Distinct values of a discovered field — used to find the service's own state codes instead of assuming them. */
export async function fetchDistinct(info: LayerInfo, field: string, o: FetchOpts): Promise<string[]> {
  const q = new URLSearchParams({ where: '1=1', outFields: field, returnDistinctValues: 'true', returnGeometry: 'false', f: 'json' });
  const j = await withRetry(() => getJson(o.fetch, `${info.url}/query?${q}`, o.timeoutMs), o.retries, 1000, o.sleep);
  return (j.features ?? []).map((f: any) => f.attributes?.[field]).filter((v: unknown) => v !== null && v !== undefined).map(String);
}
