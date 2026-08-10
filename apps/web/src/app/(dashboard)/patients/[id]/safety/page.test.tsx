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
    expect(
      screen.getByText('Documented allergy to Ibuprofen (NSAID class).'),
    ).toBeInTheDocument();
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

  it('omits the eGFR note when renal function is unknown', async () => {
    vi.mocked(api.checkDrugSafety).mockResolvedValue(
      result({
        checked_against: { current_medications: 0, allergies: 0, conditions: 0, egfr_available: false },
      }),
    );
    await runCheck();

    expect(await screen.findByText(/0 current/)).toBeInTheDocument();
    expect(screen.queryByText(/eGFR available/)).not.toBeInTheDocument();
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
    expect(await screen.findByText('Check failed')).toBeInTheDocument();
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
