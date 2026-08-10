import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Account } from '@/lib/types';

const replace = vi.fn();
vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace, push: vi.fn(), refresh: vi.fn(), back: vi.fn() }),
}));

let authState: { account: Account | null; loading: boolean } = { account: null, loading: true };
vi.mock('@/lib/auth', () => ({ useAuth: () => authState }));

import Home from './page';

const ACCOUNT: Account = {
  id: 'acc-1',
  email: 'jane@clinic.in',
  display_name: 'Dr. Jane Smith',
  created_at: '2026-01-01T00:00:00Z',
};

describe('Home (route gate)', () => {
  beforeEach(() => {
    replace.mockReset();
  });

  it('waits for the session check instead of bouncing to /login prematurely', () => {
    authState = { account: null, loading: true };
    render(<Home />);
    expect(replace).not.toHaveBeenCalled();
    expect(screen.getByText('Loading…')).toBeInTheDocument();
  });

  it('sends an authenticated clinician to the patient list', () => {
    authState = { account: ACCOUNT, loading: false };
    render(<Home />);
    expect(replace).toHaveBeenCalledWith('/patients');
  });

  it('sends an unauthenticated visitor to the login page', () => {
    authState = { account: null, loading: false };
    render(<Home />);
    expect(replace).toHaveBeenCalledWith('/login');
  });
});
