import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Paginated, Patient, PatientSummary } from '@/lib/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    api: { ...actual.api, listPatients: vi.fn(), createPatient: vi.fn() },
  };
});

import { api, ApiError } from '@/lib/api';
import PatientsPage from './page';

function summary(overrides: Partial<PatientSummary> = {}): PatientSummary {
  return {
    id: 'pat-1',
    full_name: 'Asha Reddy',
    date_of_birth: '1970-04-02',
    sex: 'female',
    phone: '+91 98765 43210',
    consent_given: true,
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

function page(items: PatientSummary[]): Paginated<PatientSummary> {
  return {
    items,
    pagination: { total: items.length, limit: 50, offset: 0, has_more: false },
  };
}

describe('PatientsPage', () => {
  beforeEach(() => {
    vi.mocked(api.listPatients).mockReset().mockResolvedValue(page([summary()]));
    vi.mocked(api.createPatient).mockReset();
  });

  it('loads the roster on mount and links each patient to their detail page', async () => {
    render(<PatientsPage />);

    expect(await screen.findByText('Asha Reddy')).toBeInTheDocument();
    expect(api.listPatients).toHaveBeenCalledWith(undefined);
    expect(screen.getByRole('link', { name: /Asha Reddy/ })).toHaveAttribute(
      'href',
      '/patients/pat-1',
    );
    expect(screen.getByText('1 patient registered')).toBeInTheDocument();
  });

  it('pluralises the registered-patient count', async () => {
    vi.mocked(api.listPatients).mockResolvedValue(
      page([summary(), summary({ id: 'pat-2', full_name: 'Ravi Kumar' })]),
    );
    render(<PatientsPage />);
    expect(await screen.findByText('2 patients registered')).toBeInTheDocument();
  });

  it('falls back to placeholders when demographics are missing', async () => {
    vi.mocked(api.listPatients).mockResolvedValue(
      page([summary({ sex: null, date_of_birth: null, phone: null })]),
    );
    render(<PatientsPage />);
    expect(await screen.findByText(/unknown · DOB unknown · no phone/)).toBeInTheDocument();
  });

  it('shows an empty state when no patients are registered', async () => {
    vi.mocked(api.listPatients).mockResolvedValue(page([]));
    render(<PatientsPage />);

    expect(await screen.findByText('No patients yet')).toBeInTheDocument();
    expect(screen.queryByRole('list')).not.toBeInTheDocument();
  });

  it('passes the search term through to the API', async () => {
    const user = userEvent.setup();
    render(<PatientsPage />);
    await screen.findByText('Asha Reddy');

    await user.type(screen.getByPlaceholderText('Search by name or phone'), 'Asha');
    await user.click(screen.getByRole('button', { name: 'Search' }));

    await waitFor(() => expect(api.listPatients).toHaveBeenLastCalledWith('Asha'));
  });

  it('gates patient creation behind the DPDP consent checkbox', async () => {
    const user = userEvent.setup();
    render(<PatientsPage />);
    await screen.findByText('Asha Reddy');

    await user.click(screen.getByRole('button', { name: /New patient/ }));
    const createButton = screen.getByRole('button', { name: 'Create patient' });
    expect(createButton).toBeDisabled();

    await user.click(screen.getByRole('checkbox'));
    expect(createButton).toBeEnabled();
  });

  it('creates a patient with the consent flag and reloads the roster', async () => {
    vi.mocked(api.createPatient).mockResolvedValue({} as Patient);
    const user = userEvent.setup();
    render(<PatientsPage />);
    await screen.findByText('Asha Reddy');

    await user.click(screen.getByRole('button', { name: /New patient/ }));
    await user.type(screen.getByPlaceholderText("Enter patient's full name"), 'New Patient');
    await user.selectOptions(screen.getByRole('combobox'), 'male');
    await user.click(screen.getByRole('checkbox'));
    await user.click(screen.getByRole('button', { name: 'Create patient' }));

    await waitFor(() =>
      expect(api.createPatient).toHaveBeenCalledWith({
        full_name: 'New Patient',
        sex: 'male',
        date_of_birth: null,
        phone: null,
        consent_given: true,
      }),
    );
    // Form collapses and the list is refetched.
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Create patient' })).not.toBeInTheDocument(),
    );
    expect(api.listPatients).toHaveBeenCalledTimes(2);
  });

  it('keeps the form open and shows the server message when creation fails', async () => {
    vi.mocked(api.createPatient).mockRejectedValue(
      new ApiError(422, 'invalid_dob', 'Date of birth cannot be in the future'),
    );
    const user = userEvent.setup();
    render(<PatientsPage />);
    await screen.findByText('Asha Reddy');

    await user.click(screen.getByRole('button', { name: /New patient/ }));
    await user.type(screen.getByPlaceholderText("Enter patient's full name"), 'New Patient');
    await user.click(screen.getByRole('checkbox'));
    await user.click(screen.getByRole('button', { name: 'Create patient' }));

    expect(await screen.findByText('Date of birth cannot be in the future')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Create patient' })).toBeInTheDocument();
  });

  it('toggles the create form closed again via the Cancel affordance', async () => {
    const user = userEvent.setup();
    render(<PatientsPage />);
    await screen.findByText('Asha Reddy');

    await user.click(screen.getByRole('button', { name: /New patient/ }));
    expect(screen.getByText('Register new patient')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByText('Register new patient')).not.toBeInTheDocument();
  });

  it('renders an initial from the patient name for the avatar', async () => {
    render(<PatientsPage />);
    const item = await screen.findByRole('listitem');
    expect(within(item).getByText('A')).toBeInTheDocument();
  });
});
