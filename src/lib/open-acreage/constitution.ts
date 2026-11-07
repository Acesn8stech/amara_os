// AMARA Constitution version — displayed on every output (format vYYYY.MM.DD.N). Confirmed by Scott 2026-11-07: v2026.10.30.1.
// Override with OA_CONSTITUTION_VERSION when the Constitution changes. (Builds before 2026-11-07 stamped an invented placeholder, v2026.11.06.1: do not rely on those stamps.)
const fromEnv = process.env.OA_CONSTITUTION_VERSION?.trim() ?? '';
export const CONSTITUTION_VERSION = /^v\d{4}\.\d{2}\.\d{2}\.\d+$/.test(fromEnv) ? fromEnv : 'v2026.10.30.1';

// Constitution Control 9: the pipeline may never assign a status above this.
export const CANDIDATE_STATUS = 'CANDIDATE — TITLE CHECK REQUIRED' as const;
export type CandidateStatus = typeof CANDIDATE_STATUS | 'HOLD' | 'DROP';
export const ALLOWED_CANDIDATE_STATUSES: readonly CandidateStatus[] = [CANDIDATE_STATUS, 'HOLD', 'DROP'];
