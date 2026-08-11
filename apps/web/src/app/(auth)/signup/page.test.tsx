import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const replace = vi.fn();
vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace, push: vi.fn(), refresh: vi.fn(), back: vi.fn() }),
}));

const refreshAuth = vi.fn();
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ refresh: refreshAuth }) }));

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return { ...actual, api: { ...actual.api, signup: vi.fn() } };
});

import { api, ApiError } from '@/lib/api';
import SignupPage from './page';

const TOKENS = {
  access_token: 'a',
  refresh_token: 'r',
  token_type: 'bearer',
  expires_in: 900,
};

describe('SignupPage', () => {
  beforeEach(() => {
    replace.mockReset();
    refreshAuth.mockReset().mockResolvedValue(undefined);
    vi.mocked(api.signup).mockReset();
  });

  it('enforces the backend password floor in the browser too', () => {
    render(<SignupPage />);
    expect(screen.getByLabelText(/Password/)).toHaveAttribute('minlength', '8');
    expect(screen.getByLabelText(/Display name/)).not.toBeRequired();
  });

  it('registers the clinician and redirects to the patient list', async () => {
    vi.mocked(api.signup).mockResolvedValue(TOKENS);
    const user = userEvent.setup();
    render(<SignupPage />);

    await user.type(screen.getByLabelText(/Display name/), 'Dr. Jane Smith');
    await user.type(screen.getByLabelText('Email'), 'jane@clinic.in');
    await user.type(screen.getByLabelText(/Password/), 'longenoughpw');
    await user.click(screen.getByRole('button', { name: 'Create account' }));

    await waitFor(() =>
      expect(api.signup).toHaveBeenCalledWith('jane@clinic.in', 'longenoughpw', 'Dr. Jane Smith'),
    );
    expect(replace).toHaveBeenCalledWith('/patients');
  });

  it('omits the optional display name rather than sending an empty string', async () => {
    vi.mocked(api.signup).mockResolvedValue(TOKENS);
    const user = userEvent.setup();
    render(<SignupPage />);

    await user.type(screen.getByLabelText('Email'), 'jane@clinic.in');
    await user.type(screen.getByLabelText(/Password/), 'longenoughpw');
    await user.click(screen.getByRole('button', { name: 'Create account' }));

    await waitFor(() =>
      expect(api.signup).toHaveBeenCalledWith('jane@clinic.in', 'longenoughpw', undefined),
    );
  });

  it('shows the server rejection reason (e.g. duplicate email)', async () => {
    vi.mocked(api.signup).mockRejectedValue(
      new ApiError(409, 'email_taken', 'An account with that email already exists'),
    );
    const user = userEvent.setup();
    render(<SignupPage />);

    await user.type(screen.getByLabelText('Email'), 'jane@clinic.in');
    await user.type(screen.getByLabelText(/Password/), 'longenoughpw');
    await user.click(screen.getByRole('button', { name: 'Create account' }));

    expect(
      await screen.findByText('An account with that email already exists'),
    ).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });

  it('falls back to a generic message for non-API failures', async () => {
    vi.mocked(api.signup).mockRejectedValue(new TypeError('Failed to fetch'));
    const user = userEvent.setup();
    render(<SignupPage />);

    await user.type(screen.getByLabelText('Email'), 'jane@clinic.in');
    await user.type(screen.getByLabelText(/Password/), 'longenoughpw');
    await user.click(screen.getByRole('button', { name: 'Create account' }));

    expect(
      await screen.findByText(/Could not reach the server, so your sign-up may not have completed/),
    ).toBeInTheDocument();
  });
});
