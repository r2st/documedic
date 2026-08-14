// Canonical enums. MUST stay in sync with apps/api/app/schemas/common.py.

export type AutonomyTier = 'informational' | 'suggestive' | 'flag_for_review';

export type ProbabilityBand = 'high' | 'moderate' | 'low' | 'very_low' | 'insufficient_data';

export type ExtractionConfidence = 'high' | 'medium' | 'low';

export type SafetySeverity = 'info' | 'warning' | 'critical' | 'hard_block';

export type SafetyCheckType =
  'drug_interaction' | 'contraindication' | 'allergy_conflict' | 'renal_dose' | 'hepatic_dose';

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
