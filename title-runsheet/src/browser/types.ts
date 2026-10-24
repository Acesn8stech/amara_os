import type { Page } from 'playwright';

export interface PortalSearchQuery {
  state: string;
  county: string;
  surveySection?: string | null;
  surveyBlock?: string | null;
  surveyTownship?: string | null;
  surveyAbstractNo?: string | null;
  /**
   * Specific subdivided-tract numbers to search/filter for within the
   * survey (e.g. ["290", "497"]). When the portal has no native tract
   * field, drivers should still return every hit for the
   * section/block/township and tag `legalDescription` so callers can
   * filter with `hitTouchesTract()` below.
   */
  tractNumbers?: string[];
}

export interface PortalSearchHit {
  instrumentNumber: string | null;
  instrumentType: string | null;
  recordingDate: string | null; // ISO yyyy-mm-dd if parseable, else null
  grantor: string | null;
  grantee: string | null;
  /** Raw legal-description text from the results row/detail view, when the portal exposes one. */
  legalDescription: string | null;
  /** Opaque handle the driver uses internally to locate/download this hit (e.g. a row selector or doc id). */
  handle: string;
}

/**
 * True if a hit's legal description mentions a given tract number. Portals
 * rarely expose a dedicated "tract" search field for subdivided surveys, so
 * this is the fallback filter: search broadly on section/block/township,
 * then narrow to tracts 290/497 (or whichever) with this.
 */
export function hitTouchesTract(hit: PortalSearchHit, tractNumber: string): boolean {
  if (!hit.legalDescription) return false;
  const pattern = new RegExp(`tract\\s+${tractNumber}\\b`, 'i');
  return pattern.test(hit.legalDescription);
}

/**
 * One implementation per site (TexasFile, a county clerk's own search, etc).
 * Agents talk to this interface only, so swapping PORTAL_NAME in .env swaps
 * the whole retrieval flow without touching instrumentRetrievalAgent.ts.
 */
export interface PortalDriver {
  readonly name: string;
  login(page: Page): Promise<void>;
  search(page: Page, query: PortalSearchQuery): Promise<PortalSearchHit[]>;
  downloadInstrument(page: Page, hit: PortalSearchHit): Promise<Buffer>;
}
