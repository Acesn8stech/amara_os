import type { RecordLayout } from './fixedwidth.ts';

/**
 * RRC record layouts. Offsets are NOT filled in: the manuals linked from
 * https://www.rrc.texas.gov/resource-center/research/data-sets-available-for-download/ were not
 * reachable from the build sandbox, so every layout is UNVERIFIED and the decoder refuses to run.
 * Scott/Cursor: fill `fields` from the RRC manual, set recordLength + encoding, then verified: true.
 * Canonical field names the mappers read:
 *   permit:     permit_no, issued_date (YYYYMMDD), county, lease_name, lat, lon
 *   production: well_or_lease_id, month (YYYYMM), oil_bbl, gas_mcf
 * NOTE: Drilling Permit Master is ASCII; Statewide Production Oil/Gas are EBCDIC (per RRC page).
 */
export const RRC_LAYOUTS: Record<'permit' | 'production_oil' | 'production_gas', RecordLayout> = {
  permit: { id: 'rrc-permit', recordLength: 0, encoding: 'ascii', verified: false, fields: [] },
  production_oil: { id: 'rrc-production-oil', recordLength: 0, encoding: 'ebcdic', verified: false, fields: [] },
  production_gas: { id: 'rrc-production-gas', recordLength: 0, encoding: 'ebcdic', verified: false, fields: [] },
};
