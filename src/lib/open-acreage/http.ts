/* eslint-disable @typescript-eslint/no-explicit-any -- untyped external JSON payloads */
export type Fetcher = (url: string, init?: RequestInit) => Promise<Response>;

export async function withRetry<T>(fn: () => Promise<T>, retries: number, baseMs = 1000, sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))): Promise<T> {
  let last: unknown;
  for (let i = 0; i <= retries; i++) {
    try { return await fn(); } catch (e) {
      last = e;
      // Policy denials (403/407) are not transient; do not hammer.
      if (e instanceof HttpError && (e.status === 403 || e.status === 407)) throw e;
      if (i < retries) await sleep(baseMs * 2 ** i);
    }
  }
  throw last;
}

export class HttpError extends Error {
  status: number; url: string;
  constructor(status: number, url: string) { super(`HTTP ${status} for ${url}`); this.status = status; this.url = url; }
}

export async function getJson(f: Fetcher, url: string, timeoutMs: number): Promise<any> {
  const res = await f(url, { signal: AbortSignal.timeout(timeoutMs), headers: { accept: 'application/json' } });
  if (!res.ok) throw new HttpError(res.status, url);
  const j = await res.json();
  if (j && typeof j === 'object' && 'error' in j) throw new Error(`ArcGIS error from ${url}: ${JSON.stringify((j as any).error)}`);
  return j;
}
