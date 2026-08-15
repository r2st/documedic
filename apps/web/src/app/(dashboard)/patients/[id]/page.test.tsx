import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  AuditEntry,
  LongitudinalRecord,
  Paginated,
  PaginationMeta,
  Patient,
  RecordSection,
} from '@/lib/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getPatient: vi.fn(),
      getRecord: vi.fn(),
      auditTrail: vi.fn(),
      verifyAudit: vi.fn(),
      exportPatientRecord: vi.fn(),
    },
  };
});

import { api, ApiError } from '@/lib/api';
import PatientDetailPage from './page';

function patient(overrides: Partial<Patient> = {}): Patient {
  return {
    id: 'pat-1',
    full_name: 'Asha Reddy',
    date_of_birth: '1970-04-02',
    sex: 'female',
    phone: '+91 98765 43210',
    consent_given: true,
    consent_given_at: '2026-01-01T00:00:00Z',
    address_text: null,
    notes: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

const SECTIONS: RecordSection[] = [
  'medications',
  'lab_results',
  'conditions',
  'allergies',
  'derived_markers',
];

/** Paging metadata derived from the rows the fixture was actually given, so a fixture can
 *  never claim a complete record while holding a truncated one (or the reverse). */
function completePaging(
  rows: Omit<LongitudinalRecord, 'pagination'>,
): Record<RecordSection, PaginationMeta> {
  return Object.fromEntries(
    SECTIONS.map((section) => [
      section,
      { total: rows[section].length, limit: 100, offset: 0, has_more: false },
    ]),
  ) as Record<RecordSection, PaginationMeta>;
}

function record(overrides: Partial<LongitudinalRecord> = {}): LongitudinalRecord {
  const { pagination, ...rows } = {
    patient_id: 'pat-1',
    medications: [],
    lab_results: [],
    conditions: [],
    allergies: [],
    derived_markers: [],
    ...overrides,
  };
  return { ...rows, pagination: pagination ?? completePaging(rows) };
}

function auditPage(items: AuditEntry[]): Paginated<AuditEntry> {
  return { items, pagination: { total: items.length, limit: 50, offset: 0, has_more: false } };
}

const AUDIT_ENTRY: AuditEntry = {
  id: 'aud-1',
  sequence: 7,
  action: 'patient.record.viewed',
  entity_type: 'patient',
  payload: {},
  record_hash: 'abc',
  prev_hash: 'def',
  created_at: '2026-01-02T09:30:00Z',
};

function mockAll(
  overrides: {
    record?: LongitudinalRecord;
    audit?: AuditEntry[];
    chainValid?: boolean;
  } = {},
) {
  vi.mocked(api.getPatient).mockResolvedValue(patient());
  vi.mocked(api.getRecord).mockResolvedValue(overrides.record ?? record());
  vi.mocked(api.auditTrail).mockResolvedValue(auditPage(overrides.audit ?? [AUDIT_ENTRY]));
  vi.mocked(api.verifyAudit).mockResolvedValue({
    entries_checked: 1,
    chain_valid: overrides.chainValid ?? true,
  });
}

/** The record card whose heading is `title` — the timeline repeats the same rows above it. */
function section(title: string): HTMLElement {
  const card = screen.getByRole('heading', { name: title }).closest('.rounded-xl');
  if (!card) throw new Error(`No record card found for section "${title}"`);
  return card as HTMLElement;
}

describe('PatientDetailPage', () => {
  beforeEach(() => {
    vi.mocked(api.getPatient).mockReset();
    vi.mocked(api.getRecord).mockReset();
    vi.mocked(api.auditTrail).mockReset();
    vi.mocked(api.verifyAudit).mockReset();
  });

  it('loads patient, record, audit trail, and chain verification for the routed id', async () => {
    mockAll();
    render(<PatientDetailPage params={{ id: 'pat-1' }} />);

    expect(await screen.findByRole('heading', { name: 'Asha Reddy' })).toBeInTheDocument();
    for (const call of [api.getPatient, api.getRecord, api.auditTrail, api.verifyAudit]) {
      expect(call).toHaveBeenCalledWith('pat-1');
    }
  });

  it('renders the raw timeline before any interpreted record sections (anti-automation-bias)', async () => {
    mockAll({
      record: record({
        conditions: [
          {
            id: 'c1',
            condition_name: 'Type 2 Diabetes Mellitus',
            onset_date: '2023-03-15',
            status: 'active',
          },
        ],
      }),
    });
    const { container } = render(<PatientDetailPage params={{ id: 'pat-1' }} />);
    await screen.findByRole('heading', { name: 'Asha Reddy' });

    const text = container.textContent ?? '';
    expect(text.indexOf('Timeline')).toBeGreaterThan(-1);
    expect(text.indexOf('Timeline')).toBeLessThan(text.indexOf('Conditions'));
  });

  it('lists clinical record sections with per-section counts', async () => {
    mockAll({
      record: record({
        allergies: [
          { id: 'a1', allergen_name: 'Penicillin', severity: 'severe', status: 'active' },
        ],
        medications: [{ id: 'm1', generic_name: 'Metformin', dose: '500mg', frequency: 'BD' }],
      }),
    });
    render(<PatientDetailPage params={{ id: 'pat-1' }} />);
    await screen.findByRole('heading', { name: 'Asha Reddy' });

    const allergies = section('Allergies');
    expect(within(allergies).getByText('Penicillin · severe · active')).toBeInTheDocument();
    expect(within(allergies).getByText('1')).toBeInTheDocument();
    expect(
      within(section('Medications')).getByText('Metformin · 500mg · BD'),
    ).toBeInTheDocument();
    expect(within(section('Conditions')).getByText('No conditions')).toBeInTheDocument();
    expect(within(section('Lab results')).getByText('No labs')).toBeInTheDocument();
  });

  it('marks a discontinued medication instead of listing it like the rest', async () => {
    // Until a `stop` line could actually land in the graph, every row here was current and the
    // panel could call itself "Active medications" without checking. Now that a drug can leave
    // the list, an unmarked row would tell a clinician the patient is still on it.
    mockAll({
      record: record({
        medications: [
          { id: 'm1', generic_name: 'Metformin', dose: '500mg', is_current: true },
          { id: 'm2', generic_name: 'Warfarin', dose: '5mg', is_current: false },
        ],
      }),
    });
    render(<PatientDetailPage params={{ id: 'pat-1' }} />);
    await screen.findByRole('heading', { name: 'Asha Reddy' });

    const meds = section('Medications');
    // Kept, not filtered away: "warfarin was stopped" is the fact a longitudinal record exists
    // to carry, and a list that silently drops it reads as a drug never prescribed.
    expect(within(meds).getByText('Warfarin · 5mg')).toBeInTheDocument();
    expect(within(meds).getAllByText('Stopped')).toHaveLength(1);
    expect(within(meds).getByText('Metformin · 500mg').closest('li')).not.toHaveTextContent(
      'Stopped',
    );
  });

  it('says when each medication was documented, and marks the ones that are years old', async () => {
    // Nothing in this system ages a medication out — `is_current` is written at the merge and
    // only a `stop` line clears it — so a 2019 prescription is still on this list. The list
    // showed name, dose and frequency and no date at all, which made that row and this
    // morning's row identical on screen.
    mockAll({
      record: record({
        medications: [
          {
            id: 'm1',
            generic_name: 'Metformin',
            dose: '500mg',
            is_current: true,
            event_date: new Date().toISOString().slice(0, 10),
          },
          {
            id: 'm2',
            generic_name: 'Amoxicillin',
            dose: '500mg',
            is_current: true,
            event_date: '2019-06-01',
          },
          { id: 'm3', generic_name: 'Atenolol', dose: '25mg', is_current: true },
        ],
      }),
    });
    render(<PatientDetailPage params={{ id: 'pat-1' }} />);
    await screen.findByRole('heading', { name: 'Asha Reddy' });

    const meds = section('Medications');
    // The one that matters: charted before the pandemic, still listed as current.
    expect(within(meds).getByText(/Documented 1 Jun 2019 · over \d+ years ago/)).toBeInTheDocument();
    // "No date" and "today" must not look the same either.
    expect(within(meds).getByText('No date on the prescription')).toBeInTheDocument();
    // A fresh row gets the date and no caution — the phrasing carries the warning, not a colour.
    expect(
      within(meds).getByText('Metformin · 500mg').closest('li'),
    ).not.toHaveTextContent('ago');
  });

  it('confirms the audit hash chain when the backend verifies it', async () => {
    mockAll();
    render(<PatientDetailPage params={{ id: 'pat-1' }} />);

    expect(await screen.findByText('Hash chain verified')).toBeInTheDocument();
    expect(screen.getByText(/patient\.record\.viewed/)).toBeInTheDocument();
    expect(screen.getByText('#7')).toBeInTheDocument();
  });

  it('flags a broken audit chain instead of silently rendering it as healthy', async () => {
    mockAll({ chainValid: false });
    render(<PatientDetailPage params={{ id: 'pat-1' }} />);

    expect(await screen.findByText('Chain INVALID')).toBeInTheDocument();
    expect(screen.queryByText('Hash chain verified')).not.toBeInTheDocument();
  });

  it('shows a not-found message for a 404 rather than a blank page', async () => {
    vi.mocked(api.getPatient).mockRejectedValue(new ApiError(404, 'not_found', 'Not found'));
    vi.mocked(api.getRecord).mockRejectedValue(new ApiError(404, 'not_found', 'Not found'));
    vi.mocked(api.auditTrail).mockRejectedValue(new ApiError(404, 'not_found', 'Not found'));
    vi.mocked(api.verifyAudit).mockRejectedValue(new ApiError(404, 'not_found', 'Not found'));
    render(<PatientDetailPage params={{ id: 'missing' }} />);

    expect(await screen.findByText(/Patient not found/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Back to patients/ })).toHaveAttribute(
      'href',
      '/patients',
    );
  });

  it('surfaces the server message for non-404 API failures', async () => {
    const boom = new ApiError(500, 'server_error', 'database unavailable');
    vi.mocked(api.getPatient).mockRejectedValue(boom);
    vi.mocked(api.getRecord).mockRejectedValue(boom);
    vi.mocked(api.auditTrail).mockRejectedValue(boom);
    vi.mocked(api.verifyAudit).mockRejectedValue(boom);
    render(<PatientDetailPage params={{ id: 'pat-1' }} />);

    expect(
      await screen.findByText('Failed to load patient data: database unavailable'),
    ).toBeInTheDocument();
  });

  it('falls back to a generic message when the failure is not an ApiError', async () => {
    const boom = new TypeError('Failed to fetch');
    vi.mocked(api.getPatient).mockRejectedValue(boom);
    vi.mocked(api.getRecord).mockRejectedValue(boom);
    vi.mocked(api.auditTrail).mockRejectedValue(boom);
    vi.mocked(api.verifyAudit).mockRejectedValue(boom);
    render(<PatientDetailPage params={{ id: 'pat-1' }} />);

    expect(await screen.findByText(/An unexpected error occurred/)).toBeInTheDocument();
  });

  it('links to upload, drug safety, and reasoning for the current patient', async () => {
    mockAll();
    render(<PatientDetailPage params={{ id: 'pat-1' }} />);
    await screen.findByRole('heading', { name: 'Asha Reddy' });

    expect(screen.getByRole('link', { name: /Upload document/ })).toHaveAttribute(
      'href',
      '/patients/pat-1/upload',
    );
    expect(screen.getByRole('link', { name: /Drug safety/ })).toHaveAttribute(
      'href',
      '/patients/pat-1/safety',
    );
    expect(screen.getByRole('link', { name: /Start reasoning/ })).toHaveAttribute(
      'href',
      '/patients/pat-1/encounter',
    );
  });
});

describe('PatientDetailPage header fallbacks', () => {
  beforeEach(() => {
    vi.mocked(api.getPatient).mockReset();
    vi.mocked(api.getRecord).mockReset();
    vi.mocked(api.auditTrail).mockReset();
    vi.mocked(api.verifyAudit).mockReset();
  });

  it('labels missing demographics rather than rendering empty separators', async () => {
    mockAll();
    vi.mocked(api.getPatient).mockResolvedValue(
      patient({ full_name: '', sex: null, date_of_birth: null }),
    );
    render(<PatientDetailPage params={{ id: 'pat-1' }} />);

    expect(await screen.findByText(/DOB unknown/)).toBeInTheDocument();
    expect(screen.getByText(/unknown · DOB unknown/)).toBeInTheDocument();
    // No name to derive an initial from -- the avatar shows a placeholder, not a blank circle.
    expect(screen.getByText('?')).toBeInTheDocument();
  });

  it('renders nothing rather than a half-built header when the patient payload is absent', async () => {
    mockAll();
    // A 200 with an empty body would otherwise dereference null in the header.
    vi.mocked(api.getPatient).mockResolvedValue(null as unknown as Patient);
    const { container } = render(<PatientDetailPage params={{ id: 'pat-1' }} />);

    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});

describe('PatientDetailPage recovery', () => {
  beforeEach(() => {
    vi.mocked(api.getPatient).mockReset();
    vi.mocked(api.getRecord).mockReset();
    vi.mocked(api.auditTrail).mockReset();
    vi.mocked(api.verifyAudit).mockReset();
  });

  it('offers a retry that reloads the chart in place', async () => {
    // Four reads make up this chart and any one of them can be the one that failed, so a
    // retry is usually all it needs. Before this the only way out was the browser's reload,
    // which throws the whole page away to re-ask for the same four.
    const boom = new ApiError(503, 'unavailable', 'briefly unavailable');
    vi.mocked(api.getPatient).mockRejectedValueOnce(boom).mockResolvedValue(patient());
    vi.mocked(api.getRecord).mockRejectedValueOnce(boom).mockResolvedValue(record());
    vi.mocked(api.auditTrail)
      .mockRejectedValueOnce(boom)
      .mockResolvedValue(auditPage([AUDIT_ENTRY]));
    vi.mocked(api.verifyAudit)
      .mockRejectedValueOnce(boom)
      .mockResolvedValue({ entries_checked: 1, chain_valid: true });

    render(<PatientDetailPage params={{ id: 'pat-1' }} />);
    await screen.findByText(/Failed to load patient data/);

    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));

    expect(await screen.findByRole('heading', { name: 'Asha Reddy' })).toBeInTheDocument();
    expect(screen.queryByText(/Failed to load patient data/)).not.toBeInTheDocument();
  });

  it('offers the retry on a 404 too, rather than declaring the chart gone', async () => {
    // From here a record that is momentarily unreachable and one that was deleted are the
    // same status code. Withholding the retry would be deciding between them for the
    // clinician on evidence we do not have.
    const missing = new ApiError(404, 'not_found', 'Not found');
    vi.mocked(api.getPatient).mockRejectedValue(missing);
    vi.mocked(api.getRecord).mockRejectedValue(missing);
    vi.mocked(api.auditTrail).mockRejectedValue(missing);
    vi.mocked(api.verifyAudit).mockRejectedValue(missing);

    render(<PatientDetailPage params={{ id: 'missing' }} />);
    await screen.findByText(/Patient not found/);

    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
    // And the way out is still offered beside it.
    expect(screen.getByRole('link', { name: /Back to patients/ })).toBeInTheDocument();
  });

  it('names the chart it is waiting on while the four reads are in flight', async () => {
    mockAll();
    render(<PatientDetailPage params={{ id: 'pat-1' }} />);

    // The skeleton is grey boxes and nothing else; without this a screen reader would sit in
    // silence and then find a chart that had appeared without comment.
    const status = screen.getByRole('status', { name: 'Loading patient record' });
    expect(status).toHaveAttribute('aria-busy', 'true');

    await screen.findByRole('heading', { name: 'Asha Reddy' });
  });
});

describe('PatientDetailPage section error boundaries', () => {
  it('reports a section that failed to render as missing, not as empty', async () => {
    // A record whose medication rows are not a list at all. Before the boundary this threw
    // during render and React unmounted the whole chart, leaving a blank page; the wrong
    // recovery would be an empty medications card, which reads as "no medications".
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    mockAll();
    vi.mocked(api.getRecord).mockResolvedValue({
      ...record(),
      medications: 42 as unknown as LongitudinalRecord['medications'],
    });

    render(<PatientDetailPage params={{ id: 'pat-1' }} />);

    expect(await screen.findAllByText(/could not be displayed/i)).not.toHaveLength(0);
    expect(
      screen.getAllByText(/treat this section as missing, not as empty/i).length,
    ).toBeGreaterThan(0);
    // Scoped to the sections: the patient header and the audit trail are still on screen
    // rather than gone with them.
    expect(screen.getByRole('heading', { name: 'Asha Reddy' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Audit trail' })).toBeInTheDocument();

    consoleError.mockRestore();
  });

  it('re-fetches the chart when a crashed section is retried', async () => {
    // Clearing the boundary's error alone would re-render the same payload and crash straight
    // back, so `onReset` goes and gets the record again. Here the second read returns a
    // well-formed one and the section comes back.
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    mockAll();
    vi.mocked(api.getRecord)
      .mockResolvedValueOnce({
        ...record(),
        // Not iterable and not mappable, so both the timeline and the record grid throw.
        medications: 42 as unknown as LongitudinalRecord['medications'],
      })
      .mockResolvedValue(
        record({
          medications: [{ id: 'm1', generic_name: 'Metformin', dose: '500mg', frequency: 'BD' }],
        }),
      );

    render(<PatientDetailPage params={{ id: 'pat-1' }} />);
    await screen.findAllByText(/could not be displayed/i);

    // This record breaks both boundaries — the timeline and the record grid read the same
    // rows — so each offers its own retry. Pressing either recovers both: the reload puts the
    // page back into its loading state, which unmounts every boundary and remounts it clean.
    expect(screen.getAllByRole('button', { name: 'Try again' })).toHaveLength(2);
    await userEvent.click(screen.getAllByRole('button', { name: 'Try again' })[0]);

    expect(await screen.findByText('Metformin · 500mg · BD')).toBeInTheDocument();
    expect(screen.queryByText(/could not be displayed/i)).not.toBeInTheDocument();

    consoleError.mockRestore();
  });
});

describe('PatientDetailPage retry while in flight', () => {
  it('hands the wait back to the announced chart skeleton', async () => {
    // The error view clears itself the moment the retry starts, so the feedback a clinician
    // gets is the skeleton — which already says "Loading patient record" and aria-busy. That
    // is why the retry button carries no in-flight label of its own: it is not on screen to
    // show one.
    const boom = new ApiError(503, 'unavailable', 'briefly unavailable');
    vi.mocked(api.getPatient).mockReset().mockRejectedValueOnce(boom);
    vi.mocked(api.getRecord).mockReset().mockRejectedValueOnce(boom);
    vi.mocked(api.auditTrail).mockReset().mockRejectedValueOnce(boom);
    vi.mocked(api.verifyAudit).mockReset().mockRejectedValueOnce(boom);

    render(<PatientDetailPage params={{ id: 'pat-1' }} />);
    await screen.findByText(/Failed to load patient data/);

    // The retry hangs, so the in-flight state stays observable.
    let release: (value: Patient) => void = () => {};
    vi.mocked(api.getPatient).mockReturnValue(
      new Promise<Patient>((resolve) => {
        release = resolve;
      }),
    );
    vi.mocked(api.getRecord).mockResolvedValue(record());
    vi.mocked(api.auditTrail).mockResolvedValue(auditPage([AUDIT_ENTRY]));
    vi.mocked(api.verifyAudit).mockResolvedValue({ entries_checked: 1, chain_valid: true });

    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));

    expect(await screen.findByRole('status', { name: 'Loading patient record' })).toHaveAttribute(
      'aria-busy',
      'true',
    );
    expect(screen.queryByText(/Failed to load patient data/)).not.toBeInTheDocument();

    release(patient());
    expect(await screen.findByRole('heading', { name: 'Asha Reddy' })).toBeInTheDocument();
  });
});

describe('record export', () => {
  it('exports the chart the clinician is looking at', async () => {
    vi.mocked(api.exportPatientRecord).mockResolvedValue(undefined);
    render(<PatientDetailPage params={{ id: 'pat-1' }} />);
    await screen.findByText('Asha Reddy');

    await userEvent.click(screen.getByRole('button', { name: /export record \(FHIR\)/i }));

    await waitFor(() => expect(api.exportPatientRecord).toHaveBeenCalledWith('pat-1', 'fhir'));
  });

  it('exports the same chart as a PDF when that is the button pressed', async () => {
    // Two formats, one disclosure. The button a clinician presses has to be the file they get:
    // handing a patient a FHIR bundle because the wrong handler fired is not a near miss.
    vi.mocked(api.exportPatientRecord).mockResolvedValue(undefined);
    render(<PatientDetailPage params={{ id: 'pat-1' }} />);
    await screen.findByText('Asha Reddy');

    await userEvent.click(screen.getByRole('button', { name: /export record \(PDF\)/i }));

    await waitFor(() => expect(api.exportPatientRecord).toHaveBeenCalledWith('pat-1', 'pdf'));
  });

  it('says so when the export failed rather than leaving the button looking done', async () => {
    // An export that saved nothing and one the browser filed somewhere unexpected look
    // identical from the button, and the clinician may be relying on the file existing.
    vi.mocked(api.exportPatientRecord).mockRejectedValue(new ApiError(500, 'export_error', 'boom'));
    render(<PatientDetailPage params={{ id: 'pat-1' }} />);
    await screen.findByText('Asha Reddy');

    await userEvent.click(screen.getByRole('button', { name: /export record \(FHIR\)/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/could not export this record/i);
  });
});
