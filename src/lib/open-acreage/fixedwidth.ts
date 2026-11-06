import { decodeCp037 } from './cp037.ts';

export type FieldType = 'char' | 'zoned' | 'packed' | 'int';
export interface FieldSpec { name: string; start: number; length: number; type: FieldType; scale?: number }
export interface RecordLayout {
  id: string;
  recordLength: number;
  encoding: 'ebcdic' | 'ascii';
  /** Offsets MUST come from the RRC manual. false => decoder refuses to run (Celilo: no guessed offsets). */
  verified: boolean;
  fields: FieldSpec[];
  /** Fields (names) to keep rows only when equal to given value, e.g. record-type discriminator. */
  recordTypeFilter?: { field: string; equals: string };
}

function text(bytes: Uint8Array, enc: RecordLayout['encoding']): string {
  return enc === 'ebcdic' ? decodeCp037(bytes) : new TextDecoder('latin1').decode(bytes);
}

export function decodePacked(b: Uint8Array, scale = 0): number | null {
  if (!b.length) return null;
  let digits = '';
  for (let i = 0; i < b.length; i++) {
    const hi = b[i] >> 4; const lo = b[i] & 0xf;
    if (hi > 9) return null;
    digits += hi;
    if (i < b.length - 1) { if (lo > 9) return null; digits += lo; }
  }
  const sign = b[b.length - 1] & 0xf;
  if (![0xa, 0xb, 0xc, 0xd, 0xe, 0xf].includes(sign)) return null;
  const n = Number(digits) / 10 ** scale;
  return sign === 0xd || sign === 0xb ? -n : n;
}

export function decodeZoned(b: Uint8Array, enc: RecordLayout['encoding'], scale = 0): number | null {
  if (enc === 'ascii') {
    const s = new TextDecoder('latin1').decode(b).trim();
    return /^-?\d+$/.test(s) ? Number(s) / 10 ** scale : null;
  }
  let digits = '';
  for (let i = 0; i < b.length; i++) {
    const d = b[i] & 0xf; if (d > 9) return null;
    if (i < b.length - 1 && (b[i] >> 4) !== 0xf) return null;
    digits += d;
  }
  const zone = b[b.length - 1] >> 4;
  if (![0xc, 0xd, 0xf].includes(zone)) return null;
  const n = Number(digits) / 10 ** scale;
  return zone === 0xd ? -n : n;
}

export function decodeRecord(rec: Uint8Array, layout: RecordLayout): Record<string, string | number | null> {
  const out: Record<string, string | number | null> = {};
  for (const f of layout.fields) {
    const slice = rec.subarray(f.start, f.start + f.length);
    if (slice.length < f.length) { out[f.name] = null; continue; }
    if (f.type === 'char') { const s = text(slice, layout.encoding).trim(); out[f.name] = s === '' ? null : s; }
    else if (f.type === 'packed') out[f.name] = decodePacked(slice, f.scale ?? 0);
    else if (f.type === 'zoned') out[f.name] = decodeZoned(slice, layout.encoding, f.scale ?? 0);
    else { let v = 0; for (const x of slice) v = v * 256 + x; out[f.name] = v; }
  }
  return out;
}

export interface DecodeResult { rows: Array<Record<string, string | number | null>>; trailingBytes: number }

export function decodeFile(buf: Uint8Array, layout: RecordLayout, opts: { allowUnverified?: boolean } = {}): DecodeResult {
  if (!layout.verified && !opts.allowUnverified) {
    throw new Error(`Layout ${layout.id} is UNVERIFIED (offsets not confirmed against RRC manual) — refusing to decode`);
  }
  if (layout.recordLength <= 0) throw new Error(`Layout ${layout.id} has no recordLength`);
  const rows: DecodeResult['rows'] = [];
  const n = Math.floor(buf.length / layout.recordLength);
  for (let i = 0; i < n; i++) {
    const row = decodeRecord(buf.subarray(i * layout.recordLength, (i + 1) * layout.recordLength), layout);
    const f = layout.recordTypeFilter;
    if (f && String(row[f.field]) !== f.equals) continue;
    rows.push(row);
  }
  return { rows, trailingBytes: buf.length - n * layout.recordLength };
}
