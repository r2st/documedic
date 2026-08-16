// How a list of safety flags is ordered and named for a clinician.
//
// Both of these are presentation decisions with a clinical edge, which is why they live in one
// tested module rather than inline in the page.

/**
 * The two fields ordering needs, with `severity` widened to `string`.
 *
 * Deliberately not `Pick<SafetyFlag, …>`. `SafetyFlag['severity']` is a closed union of the four
 * values this build knows about, and the API can add a fifth — the whole reason `severityRank`
 * has a fallback. Typing the parameter to the closed union would make it impossible to write the
 * test for that fallback, which is a strong hint the narrower type is claiming a guarantee the
 * wire does not give. `SafetyFlag` satisfies this, so callers pass their flags unchanged.
 */
interface DisplayableFlag {
  severity: string;
  is_hard_block: boolean;
}

/**
 * Display rank for a flag's severity. Lower sorts first.
 *
 * `hard_block` and `critical` are separated deliberately even though both are red: a hard block
 * cannot be acted past without a documented override, and a critical warning can. An unknown
 * severity string sorts with the warnings rather than the informational notes — a value this
 * client does not recognise is not grounds for burying it.
 */
const SEVERITY_RANK: Record<string, number> = {
  hard_block: 0,
  critical: 1,
  warning: 2,
  info: 3,
};

const UNKNOWN_SEVERITY_RANK = SEVERITY_RANK.warning;

/**
 * Order flags most-severe first, preserving the API's order within each severity band.
 *
 * The API returns flags grouped by which check produced them — allergies, then interactions,
 * then contraindications, then duplicates, then hepatotoxic burden, then guideline adherence,
 * then the chart-level notes appended last. That grouping is meaningful on the server and wrong
 * on the screen: it puts a non-overridable contraindication block underneath however many
 * interaction warnings happened to fire first, so the one flag that stops the prescription is
 * the one the clinician has to scroll for.
 *
 * Sorts on `is_hard_block` before severity rather than trusting the two to agree. They do agree
 * everywhere in the current rule set, but a hard block is the thing CLAUDE.md rule #3 says must
 * be seen, and deriving its position from a second field that could drift is a needless way to
 * lose it.
 *
 * The sort is stable (guaranteed by the language since ES2019), so within one severity band the
 * server's check-category grouping survives intact — the ordering only ever lifts a more severe
 * flag past a less severe one, and never reshuffles peers.
 *
 * Returns a new array; the input is not mutated, because it is React state.
 */
export function orderFlagsForDisplay<T extends DisplayableFlag>(flags: readonly T[]): T[] {
  return [...flags].sort((a, b) => {
    if (a.is_hard_block !== b.is_hard_block) return a.is_hard_block ? -1 : 1;
    return severityRank(a.severity) - severityRank(b.severity);
  });
}

function severityRank(severity: string): number {
  return SEVERITY_RANK[severity] ?? UNKNOWN_SEVERITY_RANK;
}

/**
 * Human labels for `check_type`, which the card shows beside the severity badge.
 *
 * Without this every amber card reads "WARNING" and nothing else, so a Child-Pugh assessment of
 * this patient's liver, a duplicate-therapy note and a guideline deviation are three
 * indistinguishable boxes. The label is what makes a chart-level finding legible as one.
 *
 * The three `unevaluated_*` types are worded as what they are — a check that did not run — and
 * not as a finding. That distinction is the whole reason those flags exist: this engine has
 * repeatedly reported a comparison it could not attempt as a comparison that passed, and a
 * label reading "Medication" beside that summary would put the ambiguity straight back.
 */
const CHECK_TYPE_LABELS: Record<string, string> = {
  allergy_conflict: 'Allergy conflict',
  bleeding_burden: 'Bleeding risk',
  contraindication: 'Contraindication',
  drug_interaction: 'Drug interaction',
  duplicate_therapy: 'Duplicate therapy',
  // Both ends of the age axis, and they have to be told apart at a glance: the older-adult
  // criteria say a routine drug deserves a second look, while the paediatric ones say the drug
  // is the wrong drug for this patient's age. One shared "Age-based caution" would have read as
  // the same finding on a chart that can only ever raise one of them.
  geriatric_caution: 'Age-based caution — older adult',
  paediatric_caution: 'Age-based caution — child',
  guideline_deviation: 'Guideline deviation',
  // Three dose labels rather than one, because the clinician's next action differs for each.
  // An out-of-range dose is a number to confirm; a unit mismatch is a *line to re-read against
  // the source document*, which is a different task and often a different person's; and an
  // unevaluated dose is a gap in the chart (no recorded weight) rather than a finding about the
  // prescription at all. One shared "Dose" label would have collapsed the three.
  dose_out_of_range: 'Dose outside usual range',
  dose_unit_mismatch: 'Dose unit may be wrong',
  unevaluated_dose: 'Not checked — dose',
  hepatic_dose: 'Hepatic dosing',
  hepatic_severity: 'Liver function',
  hepatotoxic_burden: 'Hepatotoxic burden',
  implausible_dose: 'Dose not possible for this drug',
  renal_dose: 'Renal dosing',
  stale_medication: 'Medication list age',
  // Deliberately not "Weight" — the finding is about the measurement's age, not the number, and
  // a label naming the value would read as a comment on how much the patient weighs.
  stale_weight: 'Recorded weight is out of date',
  unevaluated_allergy: 'Not checked — allergy',
  unevaluated_condition: 'Not checked — condition',
  unevaluated_medication: 'Not checked — medication',
  unverified_drug_name: 'Not checked — drug not recognised',
};

/**
 * The label for a check type, falling back to the raw type made readable.
 *
 * A check type this build does not know about is one the API has added since — so it is
 * rendered rather than dropped. Showing `hepatic_severity` as "hepatic severity" is worse than
 * a curated label and far better than a safety finding the screen silently omits because the
 * frontend was deployed a week earlier than the backend.
 */
export function checkTypeLabel(checkType: string): string {
  const known = CHECK_TYPE_LABELS[checkType];
  if (known) return known;
  const readable = checkType.replace(/_/g, ' ').trim();
  return readable ? readable.charAt(0).toUpperCase() + readable.slice(1) : 'Safety check';
}
