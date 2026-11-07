// AMARA Constitution version — displayed on every output (format vYYYY.MM.DD.N). Scott's current version is NOT known to this code:
// it comes from OA_CONSTITUTION_VERSION and reads UNCONFIRMED until set (an earlier build stamped a placeholder date; do not trust those stamps).
const fromEnv = process.env.OA_CONSTITUTION_VERSION?.trim() ?? '';
export const CONSTITUTION_VERSION = /^v\d{4}\.\d{2}\.\d{2}\.\d+$/.test(fromEnv) ? fromEnv : 'UNCONFIRMED';

// Constitution Control 9: the pipeline may never assign a status above this.
export const CANDIDATE_STATUS = 'CANDIDATE — TITLE CHECK REQUIRED' as const;
export type CandidateStatus = typeof CANDIDATE_STATUS | 'HOLD' | 'DROP';
export const ALLOWED_CANDIDATE_STATUSES: readonly CandidateStatus[] = [CANDIDATE_STATUS, 'HOLD', 'DROP'];
