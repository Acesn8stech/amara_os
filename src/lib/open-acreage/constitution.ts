// AMARA Constitution version — displayed on every output (format vYYYY.MM.DD.N).
export const CONSTITUTION_VERSION = 'v2026.11.06.1';

// Constitution Control 9: the pipeline may never assign a status above this.
export const CANDIDATE_STATUS = 'CANDIDATE — TITLE CHECK REQUIRED' as const;
export type CandidateStatus = typeof CANDIDATE_STATUS | 'HOLD' | 'DROP';
export const ALLOWED_CANDIDATE_STATUSES: readonly CandidateStatus[] = [CANDIDATE_STATUS, 'HOLD', 'DROP'];
