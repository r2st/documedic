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
  return { ...actual, api: { ...actual.api, login: vi.fn() } };
});

import { api, ApiError } from '@/lib/api';
import LoginPage from './page';

describe('LoginPage', () => {
  beforeEach(() => {
    replace.mockReset();
    refreshAuth.mockReset().mockResolvedValue(undefined);
    vi.mocked(api.login).mockReset();
  });

  it('renders the credential form', () => {
    render(<LoginPage />);
    expect(screen.getByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    expect(screen.getByLabelText('Email')).toBeRequired();
    expect(screen.getByLabelText('Password')).toHaveAttribute('type', 'password');
    expect(screen.getByRole('link', { name: 'Sign up' })).toHaveAttribute('href', '/signup');
  });

  it('submits the entered credentials, refreshes the session, and lands on /patients', async () => {
    vi.mocked(api.login).mockResolvedValue({
      access_token: 'a',
      refresh_token: 'r',
      token_type: 'bearer',
      expires_in: 900,
    });
    const user = userEvent.setup();
    render(<LoginPage />);

    await user.type(screen.getByLabelText('Email'), 'doc@example.com');
    await user.type(screen.getByLabelText('Password'), 'hunter2hunter2');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    await waitFor(() =>
      expect(api.login).toHaveBeenCalledWith('doc@example.com', 'hunter2hunter2'),
    );
    expect(refreshAuth).toHaveBeenCalled();
    expect(replace).toHaveBeenCalledWith('/patients');
  });

  it('surfaces the server message when the API rejects the login', async () => {
    vi.mocked(api.login).mockRejectedValue(
      new ApiError(401, 'invalid_credentials', 'Invalid email or password'),
    );
    const user = userEvent.setup();
    render(<LoginPage />);

    await user.type(screen.getByLabelText('Email'), 'doc@example.com');
    await user.type(screen.getByLabelText('Password'), 'wrongpassword');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    expect(await screen.findByText('Invalid email or password')).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });

  it('falls back to a generic message for non-API failures (e.g. network down)', async () => {
    vi.mocked(api.login).mockRejectedValue(new TypeError('Failed to fetch'));
    const user = userEvent.setup();
    render(<LoginPage />);

    await user.type(screen.getByLabelText('Email'), 'doc@example.com');
    await user.type(screen.getByLabelText('Password'), 'hunter2hunter2');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    expect(
      await screen.findByText(/Could not reach the server, so your sign-in may not have completed/),
    ).toBeInTheDocument();
  });

  it('disables the submit button while the request is in flight', async () => {
    let resolveLogin: (v: unknown) => void = () => {};
    vi.mocked(api.login).mockImplementation(
      () =>
        new Promise((res) => {
          resolveLogin = res;
        }) as never,
    );
    const user = userEvent.setup();
    render(<LoginPage />);

    await user.type(screen.getByLabelText('Email'), 'doc@example.com');
    await user.type(screen.getByLabelText('Password'), 'hunter2hunter2');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    const busyButton = await screen.findByRole('button', { name: /Signing in/ });
    expect(busyButton).toBeDisabled();

    resolveLogin({ access_token: 'a', refresh_token: 'r', token_type: 'bearer', expires_in: 900 });
    await waitFor(() => expect(replace).toHaveBeenCalled());
  });
});
