import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Account } from '@/lib/types';

let pathname = '/patients';
vi.mock('next/navigation', () => ({
  usePathname: () => pathname,
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), refresh: vi.fn(), back: vi.fn() }),
}));

const logout = vi.fn();
let authState: { account: Account | null; loading: boolean } = { account: null, loading: true };
vi.mock('@/lib/auth', () => ({
  useAuth: () => ({ ...authState, logout, refresh: vi.fn() }),
  useRequireAuth: () => authState,
}));

import DashboardLayout from './layout';

const ACCOUNT: Account = {
  id: 'acc-1',
  email: 'jane@clinic.in',
  display_name: 'Dr. Jane Smith',
  created_at: '2026-01-01T00:00:00Z',
};

describe('DashboardLayout', () => {
  beforeEach(() => {
    pathname = '/patients';
    logout.mockReset();
    authState = { account: ACCOUNT, loading: false };
  });

  it('holds the shell behind a loading state until the session resolves', () => {
    authState = { account: null, loading: true };
    render(<DashboardLayout><p>child</p></DashboardLayout>);

    expect(screen.getByText('Loading Aether Clinician…')).toBeInTheDocument();
    expect(screen.queryByText('child')).not.toBeInTheDocument();
  });

  it('does not render children for an unauthenticated visitor', () => {
    authState = { account: null, loading: false };
    render(<DashboardLayout><p>child</p></DashboardLayout>);
    expect(screen.queryByText('child')).not.toBeInTheDocument();
  });

  it('renders the navigation and page content for a signed-in clinician', () => {
    render(<DashboardLayout><p>child</p></DashboardLayout>);

    expect(screen.getByText('child')).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: 'Patients' })[0]).toHaveAttribute('href', '/patients');
    expect(screen.getAllByRole('link', { name: 'Guidelines' })[0]).toHaveAttribute('href', '/guidelines');
    expect(screen.getAllByRole('link', { name: 'Metrics' })[0]).toHaveAttribute('href', '/metrics');
  });

  it('marks the active section, including nested patient routes', () => {
    pathname = '/patients/pat-1/encounter';
    render(<DashboardLayout><p>child</p></DashboardLayout>);

    expect(screen.getAllByRole('link', { name: 'Patients' })[0].className).toContain('bg-brand-50');
    expect(screen.getAllByRole('link', { name: 'Metrics' })[0].className).not.toContain('bg-brand-50');
  });

  it('shows the display name, falling back to the email when unset', () => {
    const { unmount } = render(<DashboardLayout><p>child</p></DashboardLayout>);
    expect(screen.getByText('Dr. Jane Smith')).toBeInTheDocument();
    unmount();

    authState = { account: { ...ACCOUNT, display_name: null }, loading: false };
    render(<DashboardLayout><p>child</p></DashboardLayout>);
    expect(screen.getByText('jane@clinic.in')).toBeInTheDocument();
  });

  it('signs the clinician out from the desktop header', async () => {
    const user = userEvent.setup();
    render(<DashboardLayout><p>child</p></DashboardLayout>);

    await user.click(screen.getByRole('button', { name: 'Sign out' }));
    expect(logout).toHaveBeenCalled();
  });

  it('opens and closes the mobile menu, exposing its state to assistive tech', async () => {
    const user = userEvent.setup();
    render(<DashboardLayout><p>child</p></DashboardLayout>);

    const toggle = screen.getByRole('button', { name: 'Open menu' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getAllByRole('link', { name: 'Patients' })).toHaveLength(1);

    await user.click(toggle);
    expect(screen.getByRole('button', { name: 'Close menu' })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
    expect(screen.getAllByRole('link', { name: 'Patients' })).toHaveLength(2);

    await user.click(screen.getByRole('button', { name: 'Close menu' }));
    expect(screen.getAllByRole('link', { name: 'Patients' })).toHaveLength(1);
  });

  it('closes the mobile menu when a destination is chosen', async () => {
    const user = userEvent.setup();
    render(<DashboardLayout><p>child</p></DashboardLayout>);

    await user.click(screen.getByRole('button', { name: 'Open menu' }));
    // jsdom cannot perform the real navigation; swallow it so only the menu state is under test.
    const swallowNavigation = (e: Event) => e.preventDefault();
    document.addEventListener('click', swallowNavigation);
    try {
      await user.click(screen.getAllByRole('link', { name: 'Guidelines' })[1]);
    } finally {
      document.removeEventListener('click', swallowNavigation);
    }

    await waitFor(() =>
      expect(screen.getAllByRole('link', { name: 'Guidelines' })).toHaveLength(1),
    );
  });

  it('signs out and dismisses the menu from the mobile panel', async () => {
    const user = userEvent.setup();
    render(<DashboardLayout><p>child</p></DashboardLayout>);

    await user.click(screen.getByRole('button', { name: 'Open menu' }));
    const [, mobileSignOut] = screen.getAllByRole('button', { name: 'Sign out' });
    await user.click(mobileSignOut);

    expect(logout).toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Open menu' })).toBeInTheDocument(),
    );
  });
});
