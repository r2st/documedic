import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
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
    render(
      <DashboardLayout>
        <p>child</p>
      </DashboardLayout>,
    );

    expect(screen.getByText('Loading Aether Clinician…')).toBeInTheDocument();
    expect(screen.queryByText('child')).not.toBeInTheDocument();
  });

  it('does not render children for an unauthenticated visitor', () => {
    authState = { account: null, loading: false };
    render(
      <DashboardLayout>
        <p>child</p>
      </DashboardLayout>,
    );
    expect(screen.queryByText('child')).not.toBeInTheDocument();
  });

  it('renders the navigation and page content for a signed-in clinician', () => {
    render(
      <DashboardLayout>
        <p>child</p>
      </DashboardLayout>,
    );

    expect(screen.getByText('child')).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: 'Patients' })[0]).toHaveAttribute(
      'href',
      '/patients',
    );
    expect(screen.getAllByRole('link', { name: 'Guidelines' })[0]).toHaveAttribute(
      'href',
      '/guidelines',
    );
    expect(screen.getAllByRole('link', { name: 'Metrics' })[0]).toHaveAttribute('href', '/metrics');
  });

  it('marks the active section, including nested patient routes', () => {
    pathname = '/patients/pat-1/encounter';
    render(
      <DashboardLayout>
        <p>child</p>
      </DashboardLayout>,
    );

    expect(screen.getAllByRole('link', { name: 'Patients' })[0].className).toContain('bg-brand-50');
    expect(screen.getAllByRole('link', { name: 'Metrics' })[0].className).not.toContain(
      'bg-brand-50',
    );
  });

  it('shows the display name, falling back to the email when unset', () => {
    const { unmount } = render(
      <DashboardLayout>
        <p>child</p>
      </DashboardLayout>,
    );
    expect(screen.getByText('Dr. Jane Smith')).toBeInTheDocument();
    unmount();

    authState = { account: { ...ACCOUNT, display_name: null }, loading: false };
    render(
      <DashboardLayout>
        <p>child</p>
      </DashboardLayout>,
    );
    expect(screen.getByText('jane@clinic.in')).toBeInTheDocument();
  });

  it('signs the clinician out from the desktop header', async () => {
    const user = userEvent.setup();
    render(
      <DashboardLayout>
        <p>child</p>
      </DashboardLayout>,
    );

    await user.click(screen.getByRole('button', { name: 'Sign out' }));
    expect(logout).toHaveBeenCalled();
  });

  it('opens and closes the mobile menu, exposing its state to assistive tech', async () => {
    const user = userEvent.setup();
    render(
      <DashboardLayout>
        <p>child</p>
      </DashboardLayout>,
    );

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
    render(
      <DashboardLayout>
        <p>child</p>
      </DashboardLayout>,
    );

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
    render(
      <DashboardLayout>
        <p>child</p>
      </DashboardLayout>,
    );

    await user.click(screen.getByRole('button', { name: 'Open menu' }));
    const [, mobileSignOut] = screen.getAllByRole('button', { name: 'Sign out' });
    await user.click(mobileSignOut);

    expect(logout).toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Open menu' })).toBeInTheDocument(),
    );
  });
});

describe('DashboardLayout identity fallbacks', () => {
  beforeEach(() => {
    pathname = '/patients';
    logout.mockReset();
  });

  it('falls back to the email in the mobile panel when no display name is set', async () => {
    authState = { account: { ...ACCOUNT, display_name: null }, loading: false };
    const user = userEvent.setup();
    render(
      <DashboardLayout>
        <p>child</p>
      </DashboardLayout>,
    );

    await user.click(screen.getByRole('button', { name: 'Open menu' }));

    // Desktop header and mobile panel both identify the clinician; neither may go blank.
    expect(screen.getAllByText('jane@clinic.in')).toHaveLength(2);
    expect(screen.getAllByText('J')).toHaveLength(2);
  });

  it('renders a placeholder initial when the account has nothing to derive one from', async () => {
    authState = { account: { ...ACCOUNT, display_name: null, email: '' }, loading: false };
    const user = userEvent.setup();
    render(
      <DashboardLayout>
        <p>child</p>
      </DashboardLayout>,
    );

    expect(screen.getByText('?')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Open menu' }));
    expect(screen.getAllByText('?')).toHaveLength(2);
  });
});

describe('DashboardLayout error boundary', () => {
  let consoleError: ReturnType<typeof vi.spyOn>;

  function Boom(): JSX.Element {
    throw new Error('a page component threw during render');
  }

  beforeEach(() => {
    pathname = '/patients';
    authState = { account: ACCOUNT, loading: false };
    consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    consoleError.mockRestore();
  });

  it('keeps the header up when a screen crashes, so the clinician is not stranded', () => {
    // React unmounts the whole tree on a render throw. Without the boundary inside <main>,
    // one page failing left a white screen with no way to another chart and no way to sign
    // out — mid-consultation, with the browser's back button as the only escape.
    render(
      <DashboardLayout>
        <Boom />
      </DashboardLayout>,
    );

    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Patients' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeInTheDocument();
  });

  it('clears the caught error when the clinician navigates to another screen', () => {
    // React never resets a boundary by itself. Keyed on the pathname, so without this one
    // crashed page would leave the fallback in place for every page visited after it.
    const { rerender } = render(
      <DashboardLayout>
        <Boom />
      </DashboardLayout>,
    );
    expect(screen.getByRole('alert')).toBeInTheDocument();

    pathname = '/guidelines';
    rerender(
      <DashboardLayout>
        <p>the guidelines screen</p>
      </DashboardLayout>,
    );

    expect(screen.getByText('the guidelines screen')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
