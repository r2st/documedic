// Canonical enums. MUST stay in sync with apps/api/app/schemas/common.py.

export type AutonomyTier = 'informational' | 'suggestive' | 'flag_for_review';

export type ProbabilityBand = 'high' | 'moderate' | 'low' | 'very_low' | 'insufficient_data';

export type ExtractionConfidence = 'high' | 'medium' | 'low';

export type SafetySeverity = 'info' | 'warning' | 'critical' | 'hard_block';

// Lifecycle of a booked slot. Only 'scheduled' occupies time in the diary — cancelling is what
// frees a slot and the only thing that does. 'no_show' is deliberately distinct from
// 'cancelled': a patient who does not attend a follow-up they were booked for is a clinical
// event, not an administrative one.
export type AppointmentStatus = 'scheduled' | 'completed' | 'cancelled' | 'no_show';

// How a booked visit will be conducted. Not administrative: on an audio-only call the
// prescriber will not have seen the patient, which restricts what may be newly prescribed at
// the encounter that follows (see app.core.telehealth).
export type AppointmentModality = 'in_person' | 'video' | 'audio';

// Lifecycle of a clinical encounter. 'amended' is not a fourth kind of edit: it is what a
// *signed* encounter becomes once a signed amendment supersedes it. Neither row's content
// changes — the correction is a new encounter carrying amendsEncounterId and a reason.
export type EncounterStatus = 'draft' | 'in_progress' | 'signed' | 'amended';

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
  | 'hepatotoxic_burden'
  | 'bleeding_burden'
  | 'unevaluated_condition'
  // The two ends of the age axis. Nothing else in the engine can see how old the patient is: an
  // interaction rule is a pair of drugs and a contraindication rule is a drug and a condition.
  | 'geriatric_caution'
  | 'paediatric_caution'
  // Not a conflict either: the chart's medication list is old. Every rule still ran against it
  // as active therapy — see app.core.safety.check_stale_medications — so an absence of other
  // flags reflects how current the list is, not only what is on it.
  | 'stale_medication'
  // The last two are not about the patient at all. They are about a recommendation the system
  // generated: it named a drug the vocabulary does not know, or a dose that cannot be a dose of
  // the drug it named. See app.core.dose_text — the option text is model-written, and both
  // failures are fluent, plausible and otherwise silent all the way to this screen.
  | 'unverified_drug_name'
  | 'implausible_dose'
  // The dose axis. Three types rather than one because the clinician's next action differs for
  // each: an out-of-range dose is a number to confirm, a unit mismatch is a *line to re-read
  // against the source document*, and an unevaluated dose is a gap in the chart — a child on a
  // weight-dosed drug with no weight recorded — rather than a finding about the prescription.
  // See app.core.dose_range.
  | 'dose_out_of_range'
  | 'dose_unit_mismatch'
  | 'unevaluated_dose'
  // The weight those ceilings were computed from, judged as a measurement with an age rather
  // than as a property of the patient. A weight taken two years ago is a weight a growing child
  // has grown out of, and every mg/kg ceiling divided by it cleared silently.
  // See app.core.safety.check_weight_staleness.
  | 'stale_weight'
  // Three statements about the *format* of the consultation rather than about the drug or the
  // patient — the same drug prescribed in clinic raises none of them. A remote consultation
  // cannot administer an injection, cannot have a baseline blood level in hand at the moment
  // the prescription is written, and on an audio-only call has not seen the patient at all.
  // Raised only when the drug is being *started*: continuing established therapy remotely is
  // the ordinary use of a teleconsultation. See app.core.telehealth.
  | 'telehealth_in_person_required'
  | 'telehealth_baseline_monitoring_required'
  | 'telehealth_audio_only_initiation';

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
