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

describe('PatientsPage edge cases', () => {
  beforeEach(() => {
    vi.mocked(api.listPatients).mockReset().mockResolvedValue(page([summary()]));
    vi.mocked(api.createPatient).mockReset();
  });

  it('shows a placeholder initial for a patient with no usable name', async () => {
    vi.mocked(api.listPatients).mockResolvedValue(page([summary({ full_name: '' })]));
    render(<PatientsPage />);

    const item = await screen.findByRole('listitem');
    expect(within(item).getByText('?')).toBeInTheDocument();
  });

  it('reports a generic message when creation fails outside the API contract', async () => {
    // A network drop rejects with a TypeError, not an ApiError -- the form still has to say
    // something actionable instead of rendering `undefined`.
    vi.mocked(api.createPatient).mockRejectedValue(new TypeError('Failed to fetch'));
    const user = userEvent.setup();
    render(<PatientsPage />);

    await user.click(await screen.findByRole('button', { name: /New patient/i }));
    await user.type(screen.getByPlaceholderText(/Full name/i), 'Ravi Kumar');
    await user.click(screen.getByRole('checkbox'));
    await user.click(screen.getByRole('button', { name: /Create patient/i }));

    expect(
      await screen.findByText(/Could not reach the server, so the new patient record/),
    ).toBeInTheDocument();
    expect(screen.getByText('Register new patient')).toBeInTheDocument();
  });
});

describe('CreatePatientForm optional demographics', () => {
  beforeEach(() => {
    vi.mocked(api.listPatients).mockReset().mockResolvedValue(page([summary()]));
    vi.mocked(api.createPatient).mockReset().mockResolvedValue({} as Patient);
  });

  it('sends the date of birth and phone number when they are supplied', async () => {
    const user = userEvent.setup();
    render(<PatientsPage />);

    await user.click(await screen.findByRole('button', { name: /New patient/i }));
    await user.type(screen.getByPlaceholderText(/Full name/i), 'Ravi Kumar');
    await user.type(screen.getByPlaceholderText(/Phone number/i), '+91 90000 11111');
    const dob = screen.getByLabelText('Date of birth');
    await user.type(dob, '1985-03-14');
    await user.click(screen.getByRole('checkbox'));
    await user.click(screen.getByRole('button', { name: /Create patient/i }));

    await waitFor(() =>
      expect(api.createPatient).toHaveBeenCalledWith({
        full_name: 'Ravi Kumar',
        sex: 'unknown',
        date_of_birth: '1985-03-14',
        phone: '+91 90000 11111',
        consent_given: true,
      }),
    );
  });
});

describe('PatientsPage when the roster cannot be loaded', () => {
  beforeEach(() => {
    vi.mocked(api.createPatient).mockReset();
  });

  it('never tells the clinician they have no patients when the read failed', async () => {
    // The bug this replaced was silent and specific: a rejected `listPatients` left the list
    // empty with loading finished, so the page rendered "No patients yet — create your first
    // patient to get started." A clinician whose panel had failed to load was told in as many
    // words that their panel was empty, and the obvious next action was to re-register
    // someone who was already in the system.
    vi.mocked(api.listPatients).mockReset().mockRejectedValue(new ApiError(503, 'unavailable', 'The service is briefly unavailable.'));

    render(<PatientsPage />);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The service is briefly unavailable.',
    );
    expect(screen.queryByText('No patients yet')).not.toBeInTheDocument();
    expect(screen.queryByText(/patient.? registered/)).not.toBeInTheDocument();
  });

  it('offers a retry that re-reads the roster', async () => {
    vi.mocked(api.listPatients)
      .mockReset()
      .mockRejectedValueOnce(new ApiError(503, 'unavailable', 'Briefly unavailable.'))
      .mockResolvedValue(page([summary()]));

    render(<PatientsPage />);
    await screen.findByRole('alert');

    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));

    expect(await screen.findByText('Asha Reddy')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByText('1 patient registered')).toBeInTheDocument();
  });

  it('retries the search term that was in force, not the unfiltered list', async () => {
    vi.mocked(api.listPatients)
      .mockReset()
      .mockResolvedValueOnce(page([summary()]))
      .mockRejectedValueOnce(new ApiError(503, 'unavailable', 'Briefly unavailable.'))
      .mockResolvedValue(page([]));

    const user = userEvent.setup();
    render(<PatientsPage />);
    await screen.findByText('Asha Reddy');

    await user.type(screen.getByLabelText(/Search patients/i), 'Reddy');
    await user.click(screen.getByRole('button', { name: 'Search' }));
    await screen.findByRole('alert');

    await user.click(screen.getByRole('button', { name: 'Try again' }));

    await waitFor(() => expect(api.listPatients).toHaveBeenLastCalledWith('Reddy'));
  });

  it('keeps the last list the server actually returned behind the banner', async () => {
    // More use than a blank page, and it is still the last thing the API said. The count
    // above it is suppressed, so nothing on screen presents the stale list as current.
    vi.mocked(api.listPatients)
      .mockReset()
      .mockResolvedValueOnce(page([summary()]))
      .mockRejectedValue(new ApiError(503, 'unavailable', 'Briefly unavailable.'));

    const user = userEvent.setup();
    render(<PatientsPage />);
    await screen.findByText('Asha Reddy');

    await user.click(screen.getByRole('button', { name: 'Search' }));

    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.getByText('Asha Reddy')).toBeInTheDocument();
  });
});
