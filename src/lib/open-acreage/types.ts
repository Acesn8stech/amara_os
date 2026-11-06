import type { CandidateStatus } from './constitution.ts';

export type Geometry =
  | { type: 'Point'; coordinates: number[] }
  | { type: 'LineString'; coordinates: number[][] }
  | { type: 'Polygon'; coordinates: number[][][] }
  | { type: 'MultiPolygon'; coordinates: number[][][][] };

export type EntityType = 'lease' | 'available_parcel' | 'section_grid' | 'well' | 'permit' | 'production_monthly';
export type SourceName = 'UL' | 'BLM' | 'GLO' | 'RRC';
export type LessorType = 'UNIVERSITY' | 'STATE' | 'FEDERAL';
export type StatusNormalized = 'ACTIVE' | 'INACTIVE' | 'EXPIRED' | 'UNKNOWN';

/** Provenance every record must carry (Cite or omit). */
export interface Provenance {
  source_name: SourceName | string;
  source_url: string | null;
  source_layer: string;
  source_record_id: string | null;
  source_updated_at: string | null; // ISO; NULL when unknown — never guessed
  retrieved_at: string | null; // ISO
}

export interface BaseRecord extends Provenance {
  entity_type: EntityType;
  raw: Record<string, unknown>;
  geom: Geometry | null;
  sync_run_id?: string;
  [field: string]: unknown;
}

export interface LeaseRecord extends BaseRecord {
  entity_type: 'lease';
  lessor_type: LessorType;
  lease_no: string | null;
  lessee: string | null;
  status_raw: string | null;
  status_normalized: StatusNormalized;
  effective_date: string | null; // YYYY-MM-DD
  expiration_date: string | null;
  acres: number | null;
  county: string | null;
  state: string | null;
}

export interface WellRecord extends BaseRecord {
  entity_type: 'well';
  api_no: string | null;
  status: string | null;
  county: string | null;
}
export interface PermitRecord extends BaseRecord {
  entity_type: 'permit';
  permit_no: string | null;
  issued_date: string | null;
  county: string | null;
}
export interface ProductionRecord extends BaseRecord {
  entity_type: 'production_monthly';
  well_or_lease_id: string | null;
  month: string | null; // YYYY-MM-01
  oil_bbl: number | null;
  gas_mcf: number | null;
}

export type RunStatus = 'OK' | 'PARTIAL' | 'FAILED';
export type SourceState = 'OK' | 'UNVERIFIED' | 'NOT FOUND' | 'UNREACHABLE' | 'DISABLED';

export interface SyncRun {
  run_id: string;
  source: string;
  started_at: string;
  finished_at: string | null;
  status: RunStatus;
  records_in: number;
  records_valid: number;
  records_rejected: number;
  source_updated_at: string | null;
  error_log: string | null;
  constitution_version: string;
  source_state: SourceState;
  notes: string[];
}

export interface Rejected {
  entity_type: EntityType;
  record: Partial<BaseRecord>;
  reasons: string[];
}

export interface ChangeEvent {
  entity_type: EntityType;
  entity_id: string;
  change_type: 'NEW' | 'REMOVED' | 'STATUS_CHANGE' | 'EXPIRATION_CHANGE' | 'GEOMETRY_CHANGE';
  old_value: unknown;
  new_value: unknown;
  detected_at: string;
  sync_run_id: string;
}

export interface Candidate {
  tract_id: string;
  county: string | null;
  reason_codes: string[];
  evidence_refs: Array<{ entity_type: EntityType; source_name: string; source_record_id: string }>;
  status: CandidateStatus;
  constitution_version: string;
  created_at: string;
}

export interface Section extends BaseRecord {
  entity_type: 'section_grid';
  county: string | null;
  label: string | null;
}
