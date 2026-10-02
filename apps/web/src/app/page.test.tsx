import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Account } from '@/lib/types';

const replace = vi.fn();
vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace, push: vi.fn(), refresh: vi.fn(), back: vi.fn() }),
}));

let authState: { account: Account | null; loading: boolean; refresh: () => Promise<void> } = {
  account: null,
  loading: true,
  refresh: vi.fn(),
};
vi.mock('@/lib/auth', () => ({ useAuth: () => authState }));
vi.mock('@/lib/api', () => ({ api: { login: vi.fn(), signup: vi.fn() } }));
vi.mock('@/lib/errors', () => ({ requestErrorMessage: (_e: unknown, ctx: string) => `Error in ${ctx}` }));

import Home from './page';

const ACCOUNT: Account = {
  id: 'acc-1',
  email: 'jane@clinic.in',
  display_name: 'Dr. Jane Smith',
  created_at: '2026-01-01T00:00:00Z',
};

describe('Home (split landing)', () => {
  beforeEach(() => {
    replace.mockReset();
  });

  it('shows a loading state while the session check runs', () => {
    authState = { account: null, loading: true, refresh: vi.fn() };
    render(<Home />);
    expect(replace).not.toHaveBeenCalled();
    expect(screen.getByText('Loading…')).toBeInTheDocument();
  });

  it('sends an authenticated clinician to the patient list', () => {
    authState = { account: ACCOUNT, loading: false, refresh: vi.fn() };
    render(<Home />);
    expect(replace).toHaveBeenCalledWith('/patients');
  });

  it('shows the sign-in form for unauthenticated visitors', () => {
    authState = { account: null, loading: false, refresh: vi.fn() };
    render(<Home />);
    expect(replace).not.toHaveBeenCalled();
    const signInButtons = screen.getAllByRole('button', { name: 'Sign in' });
    expect(signInButtons.length).toBeGreaterThanOrEqual(1);
    expect(screen.getByRole('button', { name: 'Create account' })).toBeInTheDocument();
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
  });

  it('shows the headline and subtitle', () => {
    authState = { account: null, loading: false, refresh: vi.fn() };
    render(<Home />);
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(
      'Clinical decisions, supported.',
    );
  });
});
