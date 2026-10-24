/**
 * TexasFile.com portal driver.
 *
 * IMPORTANT: the selectors below are best-effort placeholders, not verified
 * against a live, authenticated TexasFile session (this build has no portal
 * credentials). Before running this for real:
 *
 *   1. Run once with PORTAL_HEADLESS=false and PORTAL_USERNAME/PASSWORD set.
 *   2. Use Playwright's inspector (`PWDEBUG=1`) or browser devtools against
 *      the actual login page, county search page, and results table to
 *      correct every selector marked TODO below.
 *   3. TexasFile's search UI differs somewhat by county — Howard County's
 *      grantor/grantee/instrument-type search and its Section/Block/Township
 *      survey search may be on separate tabs; adjust `search()` accordingly.
 *
 * Until verified, treat this driver as scaffolding: it encodes the intended
 * flow (login -> survey search -> paginate results -> download each doc) so
 * that fixing it is a selector-mapping exercise, not a rewrite.
 */
import type { Page } from 'playwright';
import { env } from '../../config/env.js';
import type { PortalDriver, PortalSearchHit, PortalSearchQuery } from '../types.js';

const SELECTORS = {
  loginUsername: '#Username', // TODO verify against live login form
  loginPassword: '#Password', // TODO verify
  loginSubmit: 'button[type="submit"]', // TODO verify
  countySelect: 'select#County', // TODO verify
  surveySectionInput: 'input#Section', // TODO verify
  surveyBlockInput: 'input#Block', // TODO verify
  surveyTownshipInput: 'input#Township', // TODO verify (Howard Co. uses "T1S"-style abstracts)
  tractInput: 'input#Tract', // TODO verify — Howard Co. subdivided-tract field may not exist on TexasFile;
                              // if absent, leave tractNumbers unset here and filter client-side with hitTouchesTract().
  searchSubmit: 'button#SearchButton', // TODO verify
  resultsTableRows: 'table#SearchResults tbody tr', // TODO verify
  resultInstrumentNumber: '.instrument-number', // TODO verify, scoped within a row
  resultInstrumentType: '.instrument-type',
  resultRecordingDate: '.recording-date',
  resultGrantor: '.grantor',
  resultGrantee: '.grantee',
  resultLegalDescription: '.legal-description', // TODO verify — may require opening a detail view per hit instead
  resultDownloadLink: 'a.download-doc', // TODO verify
};

function parseDate(text: string | null): string | null {
  if (!text) return null;
  const trimmed = text.trim();
  const parsed = new Date(trimmed);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString().slice(0, 10);
}

export const texasFileDriver: PortalDriver = {
  name: 'texasfile',

  async login(page: Page): Promise<void> {
    const username = env.portal.username();
    const password = env.portal.password();
    if (!username || !password) {
      throw new Error('PORTAL_USERNAME / PORTAL_PASSWORD are not set — cannot log in to TexasFile.');
    }

    await page.goto(`${env.portal.baseUrl()}/login`);
    await page.fill(SELECTORS.loginUsername, username);
    await page.fill(SELECTORS.loginPassword, password);
    await Promise.all([
      page.waitForLoadState('networkidle'),
      page.click(SELECTORS.loginSubmit),
    ]);

    if (page.url().includes('/login')) {
      throw new Error('TexasFile login did not redirect away from /login — check credentials or selectors.');
    }
  },

  async search(page: Page, query: PortalSearchQuery): Promise<PortalSearchHit[]> {
    // Navigate to the county's document search (URL shape is a guess; verify).
    await page.goto(`${env.portal.baseUrl()}/search`);

    await page.selectOption(SELECTORS.countySelect, { label: query.county }).catch(async () => {
      // Fall back to typing the county name if it isn't a plain <select>.
      await page.fill(SELECTORS.countySelect, query.county);
    });

    if (query.surveySection) await page.fill(SELECTORS.surveySectionInput, query.surveySection);
    if (query.surveyBlock) await page.fill(SELECTORS.surveyBlockInput, query.surveyBlock);
    if (query.surveyTownship) await page.fill(SELECTORS.surveyTownshipInput, query.surveyTownship);

    // Only the first tract number goes in a native field, if TexasFile even
    // has one for Howard Co.; multiple tracts (290, 497, ...) are searched
    // broadly on section/block/township and then filtered client-side below
    // with hitTouchesTract(), since a single native field can't hold a list.
    if (query.tractNumbers?.length === 1) {
      await page.fill(SELECTORS.tractInput, query.tractNumbers[0]).catch(() => {
        // No native tract field on this portal — fall back to post-filtering.
      });
    }

    await Promise.all([
      page.waitForLoadState('networkidle'),
      page.click(SELECTORS.searchSubmit),
    ]);

    const hits: PortalSearchHit[] = [];
    const rows = page.locator(SELECTORS.resultsTableRows);
    const count = await rows.count();

    for (let i = 0; i < count; i++) {
      const row = rows.nth(i);
      const instrumentNumber = await row.locator(SELECTORS.resultInstrumentNumber).textContent().catch(() => null);
      const instrumentType = await row.locator(SELECTORS.resultInstrumentType).textContent().catch(() => null);
      const recordingDateText = await row.locator(SELECTORS.resultRecordingDate).textContent().catch(() => null);
      const grantor = await row.locator(SELECTORS.resultGrantor).textContent().catch(() => null);
      const grantee = await row.locator(SELECTORS.resultGrantee).textContent().catch(() => null);
      const legalDescription = await row.locator(SELECTORS.resultLegalDescription).textContent().catch(() => null);

      hits.push({
        instrumentNumber: instrumentNumber?.trim() ?? null,
        instrumentType: instrumentType?.trim() ?? null,
        recordingDate: parseDate(recordingDateText),
        grantor: grantor?.trim() ?? null,
        grantee: grantee?.trim() ?? null,
        legalDescription: legalDescription?.trim() ?? null,
        handle: `row:${i}`,
      });
    }

    // TODO: paginate — TexasFile result sets for a Section/Block/Township
    // often span multiple pages; loop on a "next page" control here.

    return hits;
  },

  async downloadInstrument(page: Page, hit: PortalSearchHit): Promise<Buffer> {
    const rowIndex = Number(hit.handle.replace('row:', ''));
    const row = page.locator(SELECTORS.resultsTableRows).nth(rowIndex);

    const [download] = await Promise.all([
      page.waitForEvent('download'),
      row.locator(SELECTORS.resultDownloadLink).click(),
    ]);

    const stream = await download.createReadStream();
    if (!stream) throw new Error(`No download stream for instrument ${hit.instrumentNumber ?? hit.handle}`);

    const chunks: Buffer[] = [];
    for await (const chunk of stream) {
      chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
    }
    return Buffer.concat(chunks);
  },
};
