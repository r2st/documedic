import { render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { AuditEntry, LongitudinalRecord, Paginated, Patient } from '@/lib/types';

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

function record(overrides: Partial<LongitudinalRecord> = {}): LongitudinalRecord {
  return {
    patient_id: 'pat-1',
    medications: [],
    lab_results: [],
    conditions: [],
    allergies: [],
    derived_markers: [],
    ...overrides,
  };
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

function mockAll(overrides: {
  record?: LongitudinalRecord;
  audit?: AuditEntry[];
  chainValid?: boolean;
} = {}) {
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
          { id: 'c1', condition_name: 'Type 2 Diabetes Mellitus', onset_date: '2023-03-15', status: 'active' },
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
        allergies: [{ id: 'a1', allergen_name: 'Penicillin', severity: 'severe', status: 'active' }],
        medications: [{ id: 'm1', generic_name: 'Metformin', dose: '500mg', frequency: 'BD' }],
      }),
    });
    render(<PatientDetailPage params={{ id: 'pat-1' }} />);
    await screen.findByRole('heading', { name: 'Asha Reddy' });

    const allergies = section('Allergies');
    expect(within(allergies).getByText('Penicillin · severe · active')).toBeInTheDocument();
    expect(within(allergies).getByText('1')).toBeInTheDocument();
    expect(
      within(section('Active medications')).getByText('Metformin · 500mg · BD'),
    ).toBeInTheDocument();
    expect(within(section('Conditions')).getByText('No conditions')).toBeInTheDocument();
    expect(within(section('Lab results')).getByText('No labs')).toBeInTheDocument();
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
