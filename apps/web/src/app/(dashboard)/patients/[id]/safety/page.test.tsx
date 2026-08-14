import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { SafetyCheckResponse } from '@/lib/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return { ...actual, api: { ...actual.api, checkDrugSafety: vi.fn() } };
});

import { api, ApiError } from '@/lib/api';
import SafetyPage from './page';

function result(overrides: Partial<SafetyCheckResponse> = {}): SafetyCheckResponse {
  return {
    patient_id: 'pat-1',
    proposed_drug_reference_id: 'rx:ibuprofen',
    proposed_drug_name: 'Ibuprofen',
    is_blocked: false,
    is_hard_block: false,
    checked_against: {
      current_medications: 3,
      allergies: 1,
      conditions: 2,
      egfr_available: true,
    },
    flags: [],
    offline_capable: true,
    ...overrides,
  };
}

async function runCheck(drug = 'Brufen') {
  const user = userEvent.setup();
  render(<SafetyPage params={{ id: 'pat-1' }} />);
  await user.type(screen.getByPlaceholderText(/Proposed drug/), drug);
  await user.click(screen.getByRole('button', { name: 'Check' }));
  return user;
}

describe('SafetyPage', () => {
  beforeEach(() => {
    vi.mocked(api.checkDrugSafety).mockReset().mockResolvedValue(result());
  });

  it('states that hard blocks are non-overridable and checks run offline', () => {
    render(<SafetyPage params={{ id: 'pat-1' }} />);
    expect(screen.getByText(/Hard blocks cannot be overridden/)).toBeInTheDocument();
    expect(screen.getByText(/offline-capable/)).toBeInTheDocument();
  });

  it('sends the typed brand name to the deterministic checker for the routed patient', async () => {
    await runCheck('Brufen');
    await waitFor(() =>
      expect(api.checkDrugSafety).toHaveBeenCalledWith('pat-1', { drug_name: 'Brufen' }),
    );
  });

  it('resolves the brand name to its reference id in the result header', async () => {
    await runCheck();
    expect(await screen.findByRole('heading', { name: 'Ibuprofen' })).toBeInTheDocument();
    expect(screen.getByText('rx:ibuprofen')).toBeInTheDocument();
  });

  it('reports a clean check with the scope it was checked against', async () => {
    await runCheck();
    expect(await screen.findByText(/No interactions, contraindications/)).toBeInTheDocument();
    expect(
      screen.getByText(/3 current\s+medication\(s\), 1 allergy\(ies\), 2 condition\(s\)\./),
    ).toBeInTheDocument();
    expect(screen.getByText(/eGFR available\./)).toBeInTheDocument();
    expect(screen.getByText('No hard block')).toBeInTheDocument();
  });

  it('renders a BLOCKED badge and the hard-block flag when an allergy conflicts', async () => {
    vi.mocked(api.checkDrugSafety).mockResolvedValue(
      result({
        is_blocked: true,
        is_hard_block: true,
        flags: [
          {
            check_type: 'allergy',
            severity: 'hard_block',
            is_hard_block: true,
            summary: 'Documented allergy to Ibuprofen (NSAID class).',
            details: {},
          },
        ],
      }),
    );
    await runCheck();

    expect(await screen.findByText('BLOCKED')).toBeInTheDocument();
    expect(screen.getByText('Documented allergy to Ibuprofen (NSAID class).')).toBeInTheDocument();
    expect(screen.queryByText('No hard block')).not.toBeInTheDocument();
  });

  it('renders every non-blocking flag rather than only the most severe one', async () => {
    vi.mocked(api.checkDrugSafety).mockResolvedValue(
      result({
        flags: [
          {
            check_type: 'interaction',
            severity: 'warning',
            is_hard_block: false,
            summary: 'NSAID with ACE inhibitor may reduce renal perfusion.',
            details: {},
          },
          {
            check_type: 'renal',
            severity: 'critical',
            is_hard_block: false,
            summary: 'eGFR 38 mL/min — dose adjustment considerations apply.',
            details: {},
          },
        ],
      }),
    );
    await runCheck();

    expect(
      await screen.findByText('NSAID with ACE inhibitor may reduce renal perfusion.'),
    ).toBeInTheDocument();
    expect(
      screen.getByText('eGFR 38 mL/min — dose adjustment considerations apply.'),
    ).toBeInTheDocument();
  });

  it('names the missing eGFR when renal function is unknown', async () => {
    // This note used to be omitted entirely when there was no eGFR, which left the screen
    // saying nothing where it needed to say that renal thresholds had not been applied.
    vi.mocked(api.checkDrugSafety).mockResolvedValue(
      result({
        checked_against: {
          current_medications: 0,
          allergies: 0,
          conditions: 0,
          egfr_available: false,
        },
      }),
    );
    await runCheck();

    expect(await screen.findByText(/0 current/)).toBeInTheDocument();
    expect(screen.queryByText(/eGFR available/)).not.toBeInTheDocument();
    expect(screen.getByText(/renal thresholds were not applied/)).toBeInTheDocument();
  });

  it('shows the server message and no stale result when the check fails', async () => {
    vi.mocked(api.checkDrugSafety).mockRejectedValue(
      new ApiError(404, 'drug_not_found', 'Drug not found in vocabulary'),
    );
    await runCheck('Notadrug');

    expect(await screen.findByText('Drug not found in vocabulary')).toBeInTheDocument();
    expect(screen.queryByText('No hard block')).not.toBeInTheDocument();
  });

  it('falls back to a generic message for non-API failures', async () => {
    vi.mocked(api.checkDrugSafety).mockRejectedValue(new TypeError('Failed to fetch'));
    await runCheck();
    expect(
      await screen.findByText(/Could not reach the server, so the drug safety check/),
    ).toBeInTheDocument();
  });

  it('links back to the patient record', () => {
    render(<SafetyPage params={{ id: 'pat-1' }} />);
    expect(screen.getByRole('link', { name: /Back to patient/ })).toHaveAttribute(
      'href',
      '/patients/pat-1',
    );
  });
});

describe('SafetyPage scope summary', () => {
  beforeEach(() => {
    vi.mocked(api.checkDrugSafety).mockReset();
  });

  it('reads the scope as zero rather than blank when the counts are absent', async () => {
    // The clinician needs to know what the deterministic check ran against. A payload missing
    // those counts must degrade to "0", never to an empty gap that reads as "not checked".
    vi.mocked(api.checkDrugSafety).mockResolvedValue(
      result({
        checked_against: {} as SafetyCheckResponse['checked_against'],
      }),
    );
    await runCheck();

    expect(
      await screen.findByText(/0 current\s*medication\(s\), 0 allergy\(ies\), 0 condition\(s\)/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/eGFR available/)).not.toBeInTheDocument();
  });
});

describe('SafetyPage retries', () => {
  beforeEach(() => {
    vi.mocked(api.checkDrugSafety).mockReset();
  });

  it('re-runs the check when a transient failure is retried', async () => {
    // The check writes nothing to the chart, so it is safe to repeat unconditionally — and the
    // answer being retried is whether a drug is contraindicated.
    vi.mocked(api.checkDrugSafety).mockRejectedValueOnce(new TypeError('Failed to fetch'));
    const user = await runCheck('Brufen');

    await screen.findByText(/the drug safety check may not have completed/);

    vi.mocked(api.checkDrugSafety).mockResolvedValue(result());
    await user.click(screen.getByRole('button', { name: 'Check again' }));

    expect(await screen.findByText('Ibuprofen')).toBeInTheDocument();
    expect(api.checkDrugSafety).toHaveBeenCalledTimes(2);
    expect(api.checkDrugSafety).toHaveBeenLastCalledWith('pat-1', { drug_name: 'Brufen' });
  });

  it('retries the drug that failed, not whatever has since been typed into the field', async () => {
    vi.mocked(api.checkDrugSafety).mockRejectedValueOnce(new TypeError('Failed to fetch'));
    const user = await runCheck('Brufen');
    await screen.findByRole('button', { name: 'Check again' });

    // The clinician moves on to the next drug before noticing the banner.
    await user.clear(screen.getByPlaceholderText(/Proposed drug/));
    await user.type(screen.getByPlaceholderText(/Proposed drug/), 'Crocin');

    vi.mocked(api.checkDrugSafety).mockResolvedValue(result());
    await user.click(screen.getByRole('button', { name: 'Check again' }));

    expect(api.checkDrugSafety).toHaveBeenLastCalledWith('pat-1', { drug_name: 'Brufen' });
  });

  it('offers the retry when the server itself returned the error', async () => {
    vi.mocked(api.checkDrugSafety).mockRejectedValue(
      new ApiError(503, 'unavailable', 'The safety service is temporarily unavailable.'),
    );
    await runCheck();

    const banner = await screen.findByRole('alert');
    expect(banner).toHaveTextContent('The safety service is temporarily unavailable.');
    expect(screen.getByRole('button', { name: 'Check again' })).toBeInTheDocument();
  });

  it('clears the failed attempt once the retry succeeds', async () => {
    vi.mocked(api.checkDrugSafety).mockRejectedValueOnce(new TypeError('Failed to fetch'));
    const user = await runCheck();
    await screen.findByRole('button', { name: 'Check again' });

    vi.mocked(api.checkDrugSafety).mockResolvedValue(result());
    await user.click(screen.getByRole('button', { name: 'Check again' }));

    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    expect(screen.getByText('No hard block')).toBeInTheDocument();
  });

  it('drops a stale verdict as soon as a retry starts', async () => {
    // A verdict left on screen under an error banner is the dangerous state here: it reads as
    // the answer for the drug the clinician just asked about.
    vi.mocked(api.checkDrugSafety).mockResolvedValueOnce(result());
    const user = await runCheck();
    await screen.findByText('Ibuprofen');

    vi.mocked(api.checkDrugSafety).mockRejectedValue(new TypeError('Failed to fetch'));
    await user.click(screen.getByRole('button', { name: 'Check' }));

    await screen.findByRole('alert');
    expect(screen.queryByText('Ibuprofen')).not.toBeInTheDocument();
  });
});

describe('SafetyPage coverage footnote', () => {
  beforeEach(() => {
    vi.mocked(api.checkDrugSafety).mockReset().mockResolvedValue(result());
  });

  it('says a missing eGFR was missing instead of showing nothing', async () => {
    // The footnote used to render " eGFR available." when there was one and the empty string
    // when there was not, so "renal thresholds could not be applied to this patient" and
    // "everything was checked" looked identical on screen — an absence read as nothing at all.
    vi.mocked(api.checkDrugSafety).mockResolvedValue(
      result({
        checked_against: {
          current_medications: 3,
          allergies: 1,
          conditions: 2,
          egfr_available: false,
        },
      }),
    );
    await runCheck();

    expect(await screen.findByText(/No eGFR on this chart/)).toBeInTheDocument();
    expect(screen.queryByText(/eGFR available/)).not.toBeInTheDocument();
  });

  it('does not claim a missing eGFR when one was used', async () => {
    await runCheck();

    expect(await screen.findByText(/eGFR available/)).toBeInTheDocument();
    expect(screen.queryByText(/No eGFR on this chart/)).not.toBeInTheDocument();
  });

  it('says a missing liver panel was missing instead of showing nothing', async () => {
    // The eGFR footnote's counterpart, for the same reason: a hepatic threshold with no LFTs to
    // apply it to only reports itself in `flags` for the handful of drugs carrying such a rule.
    // For every other drug the chart's silence about the liver was silence here too.
    await runCheck();

    expect(await screen.findByText(/No liver function tests on this chart/)).toBeInTheDocument();
    expect(screen.queryByText(/Liver panel available/)).not.toBeInTheDocument();
  });

  it('does not claim a missing liver panel when one was used', async () => {
    vi.mocked(api.checkDrugSafety).mockResolvedValue(
      result({
        checked_against: {
          current_medications: 3,
          allergies: 1,
          conditions: 2,
          egfr_available: true,
          hepatic_markers_available: true,
        },
      }),
    );
    await runCheck();

    expect(await screen.findByText(/Liver panel available/)).toBeInTheDocument();
    expect(screen.queryByText(/No liver function tests on this chart/)).not.toBeInTheDocument();
  });

  it('reports allergies the vocabulary could not name rather than counting them as checked', async () => {
    // The higher-stakes half of the same gap: `allergies` counts every documented allergy,
    // including the ones carrying no reference id and no drug class, which were cross-checked
    // against nothing but an exact generic-name match. The headline count on its own overstates
    // how much of the one non-overridable rule in the engine actually ran.
    vi.mocked(api.checkDrugSafety).mockResolvedValue(
      result({
        checked_against: {
          current_medications: 1,
          allergies: 2,
          conditions: 0,
          egfr_available: true,
          unresolved_allergies: 1,
        },
      }),
    );
    await runCheck();

    expect(
      await screen.findByText(/1 of those allergy\(ies\) could not be matched to a known drug/),
    ).toBeInTheDocument();
  });

  it('stays quiet when every documented allergy was identified', async () => {
    await runCheck();
    await screen.findByText('Ibuprofen');

    expect(screen.queryByText(/of those allergy\(ies\)/)).not.toBeInTheDocument();
  });

  it('reports medications the resolver could not read rather than shrinking the count', async () => {
    // `current_medications` counts only what resolved, so a chart losing a line to the resolver
    // showed a smaller number with nothing saying why. The API reports the gap separately; not
    // rendering it put the screen back where the API was before it did.
    vi.mocked(api.checkDrugSafety).mockResolvedValue(
      result({
        checked_against: {
          current_medications: 2,
          allergies: 0,
          conditions: 1,
          egfr_available: true,
          unresolved_medications: 2,
        },
      }),
    );
    await runCheck();

    expect(
      await screen.findByText(/2 further medication\(s\).*could not be matched to a known drug/),
    ).toBeInTheDocument();
  });

  it('stays quiet when every medication on the chart was read', async () => {
    await runCheck();
    await screen.findByText('Ibuprofen');

    expect(screen.queryByText(/could not be matched to a known drug/)).not.toBeInTheDocument();
  });

  it('treats a missing or malformed tally as zero rather than rendering it', async () => {
    vi.mocked(api.checkDrugSafety).mockResolvedValue(
      result({ checked_against: { egfr_available: true, unresolved_medications: null } }),
    );
    await runCheck();

    expect(
      await screen.findByText(/Checked against 0 current medication\(s\)/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/undefined|null|NaN/)).not.toBeInTheDocument();
    expect(screen.queryByText(/could not be matched to a known drug/)).not.toBeInTheDocument();
  });
});

describe('SafetyPage loading state', () => {
  beforeEach(() => {
    vi.mocked(api.checkDrugSafety).mockReset();
  });

  it('names the wait while the check is in flight', async () => {
    let release: ((value: SafetyCheckResponse) => void) | undefined;
    vi.mocked(api.checkDrugSafety).mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      }),
    );
    await runCheck();

    // A set of grey boxes says "loading" to the eye and nothing at all to a screen reader,
    // so the block around them has to name what is being waited on.
    const block = await screen.findByRole('status', {
      name: "Checking this drug against the patient's record",
    });
    expect(block).toHaveAttribute('aria-busy', 'true');

    release?.(result());
    await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument());
    expect(screen.getByText('Ibuprofen')).toBeInTheDocument();
  });

  it('does not leave the placeholder up when the check fails', async () => {
    vi.mocked(api.checkDrugSafety).mockRejectedValue(new TypeError('Failed to fetch'));
    await runCheck();

    await screen.findByRole('alert');
    expect(screen.queryByRole('status', { name: /Checking this drug/ })).not.toBeInTheDocument();
  });

  it('shows no placeholder before the clinician has asked for anything', () => {
    render(<SafetyPage params={{ id: 'pat-1' }} />);
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('puts the hard block first even though the API returned it last', async () => {
    // evaluate_drug_safety runs allergies, then interactions, then contraindications, then the
    // chart-level notes — so the one flag that stops the prescription arrives underneath the
    // ones that do not. Rendering that order verbatim makes it the flag a clinician scrolls
    // for.
    vi.mocked(api.checkDrugSafety).mockResolvedValue(
      result({
        is_blocked: true,
        is_hard_block: true,
        flags: [
          {
            check_type: 'drug_interaction',
            severity: 'warning',
            is_hard_block: false,
            summary: 'Interaction warning that does not stop the prescription.',
            details: {},
          },
          {
            check_type: 'hepatic_severity',
            severity: 'info',
            is_hard_block: false,
            summary: 'Child-Pugh class A on this chart.',
            details: {},
          },
          {
            check_type: 'contraindication',
            severity: 'hard_block',
            is_hard_block: true,
            summary: 'Absolute contraindication in this condition.',
            details: {},
          },
        ],
      }),
    );
    await runCheck();

    await screen.findByText('Absolute contraindication in this condition.');
    const rendered = screen.getAllByRole('listitem').map((el) => el.textContent ?? '');
    expect(rendered[0]).toContain('Absolute contraindication in this condition.');
    expect(rendered[1]).toContain('Interaction warning that does not stop the prescription.');
    expect(rendered[2]).toContain('Child-Pugh class A on this chart.');
  });

  it("names the liver assessment so it is not an anonymous box among the drug's own flags", async () => {
    // The Child-Pugh window is a statement about the chart, not about the proposed drug. Until
    // the check type was rendered it was indistinguishable from a drug-interaction warning.
    vi.mocked(api.checkDrugSafety).mockResolvedValue(
      result({
        flags: [
          {
            check_type: 'hepatic_severity',
            severity: 'warning',
            is_hard_block: false,
            summary: 'Child-Pugh class C — 10 to 12 points on this chart.',
            details: { child_pugh_class: 'C' },
          },
        ],
      }),
    );
    await runCheck();

    expect(await screen.findByText('Liver function')).toBeInTheDocument();
    expect(
      screen.getByText('Child-Pugh class C — 10 to 12 points on this chart.'),
    ).toBeInTheDocument();
  });

  it('says a condition could not be checked rather than showing it as a finding', async () => {
    // A problem list the tokeniser cannot read was compared to no contraindication rule at all.
    // The label has to say that, not name it as though something was found.
    vi.mocked(api.checkDrugSafety).mockResolvedValue(
      result({
        flags: [
          {
            check_type: 'unevaluated_condition',
            severity: 'warning',
            is_hard_block: false,
            summary: '1 condition could not be read as text and carries no ICD-10 code.',
            details: { evaluated: false },
          },
        ],
      }),
    );
    await runCheck();

    expect(await screen.findByText('Not checked — condition')).toBeInTheDocument();
  });

  it('announces the number of findings on the list', async () => {
    vi.mocked(api.checkDrugSafety).mockResolvedValue(
      result({
        flags: [
          {
            check_type: 'drug_interaction',
            severity: 'warning',
            is_hard_block: false,
            summary: 'One',
            details: {},
          },
          {
            check_type: 'renal_dose',
            severity: 'warning',
            is_hard_block: false,
            summary: 'Two',
            details: {},
          },
        ],
      }),
    );
    await runCheck();

    expect(await screen.findByRole('list', { name: '2 safety finding(s)' })).toBeInTheDocument();
  });

  it('nests the findings as direct children of the list', async () => {
    // `getAllByRole('listitem')` finds them at any depth, so it passes even when the list
    // semantics are broken by a wrapper div between the list and its items. This asserts the
    // parent relationship the ARIA role actually requires.
    vi.mocked(api.checkDrugSafety).mockResolvedValue(
      result({
        flags: [
          {
            check_type: 'drug_interaction',
            severity: 'warning',
            is_hard_block: false,
            summary: 'One',
            details: {},
          },
          {
            check_type: 'renal_dose',
            severity: 'warning',
            is_hard_block: false,
            summary: 'Two',
            details: {},
          },
        ],
      }),
    );
    await runCheck();

    const list = await screen.findByRole('list', { name: '2 safety finding(s)' });
    const items = screen.getAllByRole('listitem');
    expect(items).toHaveLength(2);
    expect(items.every((item) => item.parentElement === list)).toBe(true);
  });
});
