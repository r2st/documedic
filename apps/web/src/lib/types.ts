// Shared API types (mirror of apps/api Pydantic schemas — Phase 1 subset).

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

export interface Account {
  id: string;
  email: string;
  display_name: string | null;
  created_at: string;
}

export interface PatientSummary {
  id: string;
  full_name: string;
  date_of_birth: string | null;
  sex: string | null;
  phone: string | null;
  consent_given: boolean;
  updated_at: string;
}

export interface Patient extends PatientSummary {
  address_text: string | null;
  notes: string | null;
  consent_given_at: string | null;
  created_at: string;
}

export interface PaginationMeta {
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface Paginated<T> {
  items: T[];
  pagination: PaginationMeta;
}

export interface DocumentResponse {
  id: string;
  patient_id: string;
  file_name: string;
  file_type: string;
  file_size_bytes: number;
  document_type: string | null;
  extraction_status: string;
  ocr_fallback_used: boolean;
  created_at: string;
}

export interface ExtractionField {
  name: string;
  value: unknown;
  confidence: number;
  confidence_band: 'high' | 'medium' | 'low';
  needs_confirmation: boolean;
}

export interface ExtractedEntity {
  entity_type: string;
  fields: ExtractionField[];
  region: unknown;
}

export interface ExtractionResult {
  document_id: string;
  document_type: string | null;
  model: string | null;
  ocr_fallback_used: boolean;
  entities: ExtractedEntity[];
  confirmation_required_count: number;
}

export type RecordSection =
  | 'medications'
  | 'lab_results'
  | 'conditions'
  | 'allergies'
  | 'derived_markers';

export interface LongitudinalRecord {
  patient_id: string;
  medications: Array<Record<string, unknown>>;
  lab_results: Array<Record<string, unknown>>;
  conditions: Array<Record<string, unknown>>;
  allergies: Array<Record<string, unknown>>;
  derived_markers: Array<Record<string, unknown>>;
  /** Per-section paging state. Each section is paged independently — a section's array being
   *  shorter than its `total` means the rest was not fetched, not that it does not exist. */
  pagination: Record<RecordSection, PaginationMeta>;
}

export interface SafetyFlag {
  check_type: string;
  severity: 'info' | 'warning' | 'critical' | 'hard_block';
  is_hard_block: boolean;
  summary: string;
  details: Record<string, unknown>;
}

export interface SafetyCheckResponse {
  patient_id: string;
  proposed_drug_reference_id: string;
  proposed_drug_name: string;
  is_blocked: boolean;
  is_hard_block: boolean;
  checked_against: Record<string, unknown>;
  flags: SafetyFlag[];
  offline_capable: boolean;
}

export interface AuditEntry {
  id: string;
  sequence: number;
  action: string;
  entity_type: string | null;
  payload: Record<string, unknown>;
  record_hash: string;
  prev_hash: string;
  created_at: string;
}

// --- Phase 2/3: reasoning engine ---

export type AutonomyTier = 'informational' | 'suggestive' | 'flag_for_review';
export type ProbabilityBand =
  | 'high'
  | 'moderate'
  | 'low'
  | 'very_low'
  | 'insufficient_data';
export type ReasoningStatus =
  | 'created'
  | 'intake'
  | 'intake_complete'
  | 'reasoning'
  | 'awaiting_review'
  | 'completed'
  | 'failed'
  | 'offline_paused';

export interface ReasoningSession {
  id: string;
  patient_id: string;
  presenting_complaint: string;
  status: ReasoningStatus;
  autonomy_tier: AutonomyTier | null;
  intake_complete: boolean;
  info_gain_score: number | null;
  online: boolean;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}

export interface IntakeQuestion {
  id: string;
  question_text: string;
  question_type: string;
  rationale: string | null;
  sequence_order: number;
  info_gain_score: number | null;
  answered_at: string | null;
}

export interface IntakeState {
  session: ReasoningSession;
  pending_questions: IntakeQuestion[];
  intake_complete: boolean;
}

export interface Citation {
  section_id: string;
  source: string;
  document_title: string;
  heading: string | null;
  snippet: string | null;
  score: number | null;
  corpus_version?: string | null;
  page_range?: string | null;
}

export interface ClinicalSuggestion {
  id: string;
  session_id: string;
  patient_id: string;
  output_type: 'differential' | 'cant_miss' | 'investigation' | 'management' | 'safety' | 'summary';
  autonomy_tier: AutonomyTier;
  confidence_band: ProbabilityBand | null;
  title: string;
  body: string | null;
  evidence: Record<string, unknown>;
  citations: Citation[];
  agent_trace: unknown[];
  verifier_verdict: Record<string, unknown>;
  devils_advocate: Record<string, unknown>;
  is_hard_block: boolean;
  cant_miss_flag: boolean;
  supersedes_id: string | null;
  created_at: string;
}

export interface ReasoningResult {
  session: ReasoningSession;
  suggestions: ClinicalSuggestion[];
  case_state: Record<string, unknown>;
}

export interface ReasoningEvent {
  event: string;
  data: Record<string, unknown>;
}

// --- Phase 4: validation / regulatory / safety ---

export interface PerformanceMetrics {
  pilot_mode: boolean;
  total_sessions: number;
  completed_sessions: number;
  awaiting_review: number;
  autonomy_tier_distribution: Record<string, number>;
  hard_blocks_total: number;
  cant_miss_total: number;
  verifier_disagreement_rate: number;
  degraded_rate: number;
  mean_citation_faithfulness: number | null;
  citation_faithfulness_target: number;
  open_safety_reports: number;
}

export interface ValidationRun {
  id: string;
  vignette_count: number;
  corpus_version: string | null;
  metrics: Record<string, number | Record<string, number> | null>;
  results: Array<Record<string, unknown>>;
  notes: string | null;
  created_at: string;
}

export interface SafetyReport {
  id: string;
  category: string;
  severity: string;
  status: string;
  description: string;
  patient_id: string | null;
  session_id: string | null;
  detail: Record<string, unknown>;
  created_at: string;
}
