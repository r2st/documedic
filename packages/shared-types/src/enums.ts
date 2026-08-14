// Canonical enums. MUST stay in sync with apps/api/app/schemas/common.py.

export type AutonomyTier = 'informational' | 'suggestive' | 'flag_for_review';

export type ProbabilityBand = 'high' | 'moderate' | 'low' | 'very_low' | 'insufficient_data';

export type ExtractionConfidence = 'high' | 'medium' | 'low';

export type SafetySeverity = 'info' | 'warning' | 'critical' | 'hard_block';

// Mirrors app.core.safety.CheckType. The last two are not conflicts the engine found but
// statements that part of the chart could not be evaluated at all — an unreadable medication
// line, an allergen the vocabulary cannot identify — which must never render as a clean check.
export type SafetyCheckType =
  | 'drug_interaction'
  | 'contraindication'
  | 'allergy_conflict'
  | 'renal_dose'
  | 'hepatic_dose'
  | 'duplicate_therapy'
  | 'guideline_deviation'
  | 'unevaluated_medication'
  | 'unevaluated_allergy'
  | 'hepatic_severity'
  | 'hepatotoxic_burden';

export type ReasoningStatus =
  | 'created'
  | 'intake'
  | 'intake_complete'
  | 'reasoning'
  | 'awaiting_review'
  | 'completed'
  | 'failed'
  | 'offline_paused';

export type ClinicalOutputType =
  'differential' | 'cant_miss' | 'investigation' | 'management' | 'safety' | 'summary';

export type IntakeQuestionType =
  'red_flag' | 'relevant_negative' | 'clarifying' | 'history' | 'exam';

export type SpecialistRole =
  'internal_medicine' | 'cardiology' | 'infectious_disease' | 'primary_care' | 'sentinel';

export type VerifierStatus = 'agree' | 'partial_disagreement' | 'major_disagreement';

export type AvailabilityTier = 'phc' | 'chc' | 'district_hospital' | 'referral';

export type ClinicianDecision = 'acknowledged' | 'accepted' | 'dismissed' | 'overridden';

export type GuidelineSource = 'icmr' | 'who' | 'nice';
