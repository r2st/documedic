/**
 * Ordering and naming of deterministic safety flags.
 *
 * Both are presentation, and both are the kind of presentation that decides whether a clinician
 * sees a hard block or scrolls past it — which is why they are pinned here rather than left to
 * whatever order the API happened to build its list in.
 */

import { describe, expect, it } from 'vitest';

import { checkTypeLabel, orderFlagsForDisplay } from './safety';

type Flag = { check_type: string; severity: string; is_hard_block: boolean };

function flag(check_type: string, severity: string, is_hard_block = false): Flag {
  return { check_type, severity, is_hard_block };
}

const types = (flags: readonly Flag[]) => flags.map((f) => f.check_type);

describe('orderFlagsForDisplay', () => {
  it('lifts a hard block above the warnings the API returned before it', () => {
    // The real shape of the bug: evaluate_drug_safety runs allergies, then interactions, then
    // contraindications — so a non-overridable contraindication arrives third, underneath two
    // warnings that do not stop the prescription.
    const ordered = orderFlagsForDisplay([
      flag('drug_interaction', 'warning'),
      flag('duplicate_therapy', 'warning'),
      flag('contraindication', 'hard_block', true),
    ]);

    expect(types(ordered)).toEqual(['contraindication', 'drug_interaction', 'duplicate_therapy']);
  });

  it('orders the four severity bands most-severe first', () => {
    const ordered = orderFlagsForDisplay([
      flag('hepatic_severity', 'info'),
      flag('renal_dose', 'warning'),
      flag('allergy_conflict', 'hard_block', true),
      flag('drug_interaction', 'critical'),
    ]);

    expect(types(ordered)).toEqual([
      'allergy_conflict',
      'drug_interaction',
      'renal_dose',
      'hepatic_severity',
    ]);
  });

  it('keeps a critical warning below a hard block even though both render red', () => {
    // Both are red cards, and the difference is the whole of Critical Safety Rule #3: one needs
    // a documented override to get past and the other does not.
    const ordered = orderFlagsForDisplay([
      flag('drug_interaction', 'critical'),
      flag('allergy_conflict', 'hard_block', true),
    ]);

    expect(types(ordered)).toEqual(['allergy_conflict', 'drug_interaction']);
  });

  it('sorts on is_hard_block even when the severity string disagrees with it', () => {
    // The two agree everywhere in the current rule set. Deriving a hard block's position from
    // the severity string alone would make a future disagreement bury it silently, so the
    // blocking field is what decides.
    const ordered = orderFlagsForDisplay([
      flag('drug_interaction', 'critical'),
      flag('contraindication', 'warning', true),
    ]);

    expect(types(ordered)).toEqual(['contraindication', 'drug_interaction']);
  });

  it('preserves the API check-category grouping within one severity band', () => {
    // A stable sort only ever lifts a more severe flag past a less severe one. Peers keep the
    // order the server produced, which groups them by which check fired.
    const ordered = orderFlagsForDisplay([
      flag('allergy_conflict', 'warning'),
      flag('drug_interaction', 'warning'),
      flag('contraindication', 'warning'),
      flag('duplicate_therapy', 'warning'),
    ]);

    expect(types(ordered)).toEqual([
      'allergy_conflict',
      'drug_interaction',
      'contraindication',
      'duplicate_therapy',
    ]);
  });

  it('does not bury a severity string it does not recognise', () => {
    // A severity this build has never heard of ranks with the warnings, not with the
    // informational notes. A value the client cannot classify is not grounds for hiding it.
    const ordered = orderFlagsForDisplay([
      flag('hepatic_severity', 'info'),
      flag('something_new', 'catastrophic'),
    ]);

    expect(types(ordered)).toEqual(['something_new', 'hepatic_severity']);
  });

  it('does not mutate the array it was given', () => {
    // It is called on React state during render.
    const input = [
      flag('drug_interaction', 'warning'),
      flag('allergy_conflict', 'hard_block', true),
    ];
    const before = types(input);

    orderFlagsForDisplay(input);

    expect(types(input)).toEqual(before);
  });

  it('handles an empty list', () => {
    expect(orderFlagsForDisplay([])).toEqual([]);
  });
});

describe('checkTypeLabel', () => {
  it('names the chart-level checks so they are not anonymous amber boxes', () => {
    expect(checkTypeLabel('hepatic_severity')).toBe('Liver function');
    expect(checkTypeLabel('hepatotoxic_burden')).toBe('Hepatotoxic burden');
  });

  it('words the unevaluated checks as a check that did not run, not as a finding', () => {
    // The distinction those flags exist for. A label reading "Condition" beside "no
    // contraindication rule was evaluated against it" would put the ambiguity straight back.
    expect(checkTypeLabel('unevaluated_condition')).toBe('Not checked — condition');
    expect(checkTypeLabel('unevaluated_allergy')).toBe('Not checked — allergy');
    expect(checkTypeLabel('unevaluated_medication')).toBe('Not checked — medication');
  });

  it('renders a check type this build has never seen rather than dropping it', () => {
    // The frontend and the API deploy separately. A safety finding omitted because the client
    // is a week older than the server is the failure this whole engine keeps being corrected
    // for, arriving by a different route.
    expect(checkTypeLabel('qt_prolongation_risk')).toBe('Qt prolongation risk');
  });

  it('falls back to a generic label rather than an empty badge', () => {
    expect(checkTypeLabel('')).toBe('Safety check');
    expect(checkTypeLabel('___')).toBe('Safety check');
  });
});
