/**
 * Accessibility contracts that span pages.
 *
 * The per-page suites assert what each screen does; these assert the properties a clinician
 * using a screen reader or a keyboard depends on, stated once so a new form cannot quietly
 * skip them:
 *
 *   * every interactive control has an accessible name — not a placeholder, which is an
 *     example rather than a label and vanishes as soon as typing starts
 *   * every failure message is announced, because it appears far from where focus is
 *   * results that arrive without moving focus live in a polite live region
 *   * decorative iconography is hidden from the accessibility tree rather than read aloud
 *
 * `getByLabelText`/`getByRole(..., { name })` are the assertions that matter here: they
 * resolve through the same accessible-name computation a screen reader uses, so they fail
 * exactly when the name is missing or wrong.
 */

import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Citation, IntakeQuestion, Paginated, PatientSummary, SafetyCheckResponse } from '@/lib/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      listPatients: vi.fn(),
      createPatient: vi.fn(),
      checkDrugSafety: vi.fn(),
      searchGuidelines: vi.fn(),
      corpusInfo: vi.fn(),
      submitIntakeAnswers: vi.fn(),
      uploadDocument: vi.fn(),
      getExtraction: vi.fn(),
    },
  };
});

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => '/patients',
  useParams: () => ({ id: 'pat-1' }),
}));

import { api, ApiError } from '@/lib/api';
import PatientsPage from '@/app/(dashboard)/patients/page';
import SafetyPage from '@/app/(dashboard)/patients/[id]/safety/page';
import GuidelinesPage from '@/app/(dashboard)/guidelines/page';
import UploadPage from '@/app/(dashboard)/patients/[id]/upload/page';
import { IntakeFlow } from '@/components/reasoning/IntakeFlow';
import { Button, ConfidenceBadge, ErrorBanner } from '@/components/ui';

const EMPTY_ROSTER: Paginated<PatientSummary> = {
  items: [],
  pagination: { total: 0, limit: 50, offset: 0, has_more: false },
};

function question(overrides: Partial<IntakeQuestion> = {}): IntakeQuestion {
  return {
    id: 'q-1',
    question_text: 'Any chest pain on exertion?',
    question_type: 'red_flag',
    rationale: 'Discriminates cardiac from musculoskeletal causes.',
    ...overrides,
  } as IntakeQuestion;
}

beforeEach(() => {
  vi.mocked(api.listPatients).mockReset().mockResolvedValue(EMPTY_ROSTER);
  vi.mocked(api.createPatient).mockReset();
  vi.mocked(api.checkDrugSafety).mockReset();
  vi.mocked(api.searchGuidelines).mockReset().mockResolvedValue([]);
  vi.mocked(api.corpusInfo).mockReset().mockResolvedValue({
    corpus_version: 'v1',
    chunk_count: 12,
    retrieval_threshold: 0.5,
    citation_faithfulness_target: 0.95,
  });
  vi.mocked(api.submitIntakeAnswers).mockReset();
  vi.mocked(api.uploadDocument).mockReset();
  vi.mocked(api.getExtraction).mockReset();
});

// --------------------------------------------------------------- accessible names

describe('every text input is reachable by its accessible name', () => {
  it('labels the patient search field', async () => {
    render(<PatientsPage />);
    await waitFor(() => expect(api.listPatients).toHaveBeenCalled());

    expect(screen.getByLabelText(/search patients by name or phone/i)).toBeInTheDocument();
  });

  it('labels every field of the new-patient form', async () => {
    render(<PatientsPage />);
    await waitFor(() => expect(api.listPatients).toHaveBeenCalled());
    await userEvent.click(screen.getByRole('button', { name: /new patient/i }));

    for (const label of [/full name/i, /^sex$/i, /date of birth/i, /^phone$/i, /consent obtained/i]) {
      expect(screen.getByLabelText(label)).toBeInTheDocument();
    }
  });

  it('labels the drug-safety field, which otherwise has only a placeholder', () => {
    render(<SafetyPage params={{ id: 'pat-1' }} />);

    expect(screen.getByLabelText(/proposed drug/i)).toBeInTheDocument();
  });

  it('labels the guideline search field', async () => {
    render(<GuidelinesPage />);
    await waitFor(() => expect(api.corpusInfo).toHaveBeenCalled());

    expect(screen.getByLabelText(/search the clinical guideline corpus/i)).toBeInTheDocument();
  });

  it('labels the document file input and describes what it accepts', () => {
    render(<UploadPage params={{ id: 'pat-1' }} />);

    const input = screen.getByLabelText(/drag and drop a file here/i);
    expect(input).toHaveAttribute('type', 'file');
    expect(input).toHaveAccessibleDescription(/PDF, JPEG, or PNG up to 10 MB/i);
  });

  it('names each intake answer field after the question it answers', () => {
    render(
      <IntakeFlow sessionId="s-1" questions={[question()]} onComplete={vi.fn()} />,
    );

    const field = screen.getByLabelText('Any chest pain on exertion?');
    expect(field).toHaveAccessibleDescription(/discriminates cardiac from musculoskeletal/i);
  });

  it('distinguishes the per-entity include checkboxes, whose visible labels are identical', async () => {
    vi.mocked(api.uploadDocument).mockResolvedValue({ id: 'doc-1' } as never);
    vi.mocked(api.getExtraction).mockResolvedValue({
      document_id: 'doc-1',
      document_type: 'prescription',
      model: null,
      ocr_fallback_used: false,
      confirmation_required_count: 0,
      entities: [
        { entity_type: 'medication', region: null, fields: [] },
        { entity_type: 'lab_result', region: null, fields: [] },
      ],
    } as never);
    render(<UploadPage params={{ id: 'pat-1' }} />);

    await userEvent.upload(
      screen.getByLabelText(/drag and drop a file here/i),
      new File(['%PDF-1.4'], 'rx.pdf', { type: 'application/pdf' }),
    );

    // Every row's visible text is the same word, "Include" — the accessible names must not be.
    const boxes = await screen.findAllByRole('checkbox');
    const names = boxes.map((b) => b.getAttribute('aria-label'));
    expect(names).toEqual([
      'Include medication 1 in the record',
      'Include lab_result 2 in the record',
    ]);
  });
});

// --------------------------------------------------------------- announcements

describe('failures and results are announced', () => {
  it('renders failure messages in an alert region', () => {
    render(<ErrorBanner message="Could not resolve the proposed drug" />);

    expect(screen.getByRole('alert')).toHaveTextContent('Could not resolve the proposed drug');
  });

  it('reports a create-patient failure through that region', async () => {
    vi.mocked(api.createPatient).mockRejectedValue(new ApiError(422, 'validation_error', 'Consent is required'));
    render(<PatientsPage />);
    await waitFor(() => expect(api.listPatients).toHaveBeenCalled());

    await userEvent.click(screen.getByRole('button', { name: /new patient/i }));
    await userEvent.type(screen.getByLabelText(/full name/i), 'Asha Reddy');
    await userEvent.click(screen.getByLabelText(/consent obtained/i));
    await userEvent.click(screen.getByRole('button', { name: /create patient/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Consent is required');
  });

  it('delivers the safety verdict into a polite live region', async () => {
    const verdict: SafetyCheckResponse = {
      patient_id: 'pat-1',
      proposed_drug_reference_id: 'IBU-400',
      proposed_drug_name: 'Ibuprofen',
      is_blocked: true,
      is_hard_block: true,
      offline_capable: true,
      checked_against: { current_medications: 1, allergies: 1, conditions: 0, egfr_available: false },
      flags: [
        {
          check_type: 'allergy_conflict',
          severity: 'hard_block',
          is_hard_block: true,
          summary: 'Documented allergy to Ibuprofen conflicts with Ibuprofen.',
          details: {},
        },
      ],
    };
    vi.mocked(api.checkDrugSafety).mockResolvedValue(verdict);
    const { container } = render(<SafetyPage params={{ id: 'pat-1' }} />);

    await userEvent.type(screen.getByLabelText(/proposed drug/i), 'Brufen');
    await userEvent.click(screen.getByRole('button', { name: /^check$/i }));

    const verdictText = await screen.findByText('Ibuprofen');
    const live = container.querySelector('[aria-live="polite"]');
    expect(live).not.toBeNull();
    expect(live).toContainElement(verdictText);
  });

  it('delivers guideline results into a polite live region', async () => {
    const citation = {
      source: 'ICMR',
      document_title: 'Dengue STW',
      section_id: '3.1',
      heading: 'Management',
      snippet: 'Fluid resuscitation…',
      page_range: '4-5',
      score: 0.91,
    } as Citation;
    vi.mocked(api.searchGuidelines).mockResolvedValue([citation]);
    const { container } = render(<GuidelinesPage />);
    await waitFor(() => expect(api.corpusInfo).toHaveBeenCalled());

    await userEvent.type(screen.getByLabelText(/search the clinical guideline corpus/i), 'dengue');
    await userEvent.click(screen.getByRole('button', { name: /search/i }));

    const result = await screen.findByText('Dengue STW');
    expect(container.querySelector('[aria-live="polite"]')).toContainElement(result);
  });

  it('marks the roster as busy while it loads', () => {
    vi.mocked(api.listPatients).mockReturnValue(new Promise(() => {}));
    render(<PatientsPage />);

    expect(screen.getByRole('status', { name: /loading patients/i })).toHaveAttribute(
      'aria-busy',
      'true',
    );
  });
});

// --------------------------------------------------------------- disclosure and state

describe('toggles and disabled controls explain themselves', () => {
  it('reports the new-patient form as an expandable disclosure', async () => {
    render(<PatientsPage />);
    await waitFor(() => expect(api.listPatients).toHaveBeenCalled());

    const toggle = screen.getByRole('button', { name: /new patient/i });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveAttribute('aria-controls', 'new-patient-form');

    await userEvent.click(toggle);
    expect(screen.getByRole('button', { name: /cancel/i })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
  });

  it('says why the create button is disabled rather than leaving it silently dimmed', async () => {
    render(<PatientsPage />);
    await waitFor(() => expect(api.listPatients).toHaveBeenCalled());
    await userEvent.click(screen.getByRole('button', { name: /new patient/i }));

    const submit = screen.getByRole('button', { name: /create patient/i });
    expect(submit).toBeDisabled();
    expect(submit).toHaveAccessibleDescription(/confirm patient consent above/i);

    await userEvent.click(screen.getByLabelText(/consent obtained/i));
    expect(submit).toBeEnabled();
    expect(submit).toHaveAccessibleDescription(/consent recorded/i);
  });
});

// --------------------------------------------------------------- decorative content

describe('decorative content stays out of the accessibility tree', () => {
  it('hides icons so they are not read as unlabelled graphics', () => {
    const { container } = render(<ErrorBanner message="Upload failed" />);

    const icon = container.querySelector('svg');
    expect(icon).toHaveAttribute('aria-hidden', 'true');
  });

  it('spells out what a confidence percentage measures', () => {
    render(<ConfidenceBadge band="medium" value={0.72} />);

    expect(screen.getByLabelText('Extraction confidence medium, 72 percent')).toHaveTextContent(
      '72%',
    );
  });
});

// --------------------------------------------------------------- keyboard behaviour

describe('keyboard operation', () => {
  it('defaults shared buttons to type=button so none can submit a form by accident', () => {
    render(<Button>Do the thing</Button>);

    expect(screen.getByRole('button', { name: 'Do the thing' })).toHaveAttribute('type', 'button');
  });

  it('still honours an explicit submit type', () => {
    render(<Button type="submit">Save</Button>);

    expect(screen.getByRole('button', { name: 'Save' })).toHaveAttribute('type', 'submit');
  });

  it('reaches the whole new-patient form by tabbing, in visual order', async () => {
    render(<PatientsPage />);
    await waitFor(() => expect(api.listPatients).toHaveBeenCalled());
    await userEvent.click(screen.getByRole('button', { name: /new patient/i }));

    const order = [
      screen.getByLabelText(/full name/i),
      screen.getByLabelText(/^sex$/i),
      screen.getByLabelText(/date of birth/i),
      screen.getByLabelText(/^phone$/i),
      screen.getByLabelText(/consent obtained/i),
    ];
    order[0].focus();
    for (let i = 1; i < order.length; i += 1) {
      await userEvent.tab();
      expect(order[i]).toHaveFocus();
    }
  });

  it('makes the transparent file input visibly focused, since its own ring cannot be seen', async () => {
    const { container } = render(<UploadPage params={{ id: 'pat-1' }} />);
    const dropZone = container.querySelector('[class*="border-dashed"]') as HTMLElement;

    expect(dropZone.className).not.toContain('ring-2');

    act(() => screen.getByLabelText(/drag and drop a file here/i).focus());

    expect(dropZone.className).toContain('ring-2');
  });
});
