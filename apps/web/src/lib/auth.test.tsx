import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      me: vi.fn(),
      logout: vi.fn(),
      refreshAccessToken: vi.fn(),
    },
  };
});

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn() }),
}));

import { api, tokenStore } from './api';
import { AuthProvider, useAuth, useRequireAuth } from './auth';

const ACCOUNT = {
  id: 'acc-1',
  email: 'doc@example.com',
  display_name: null,
  created_at: '2026-01-01T00:00:00Z',
};

function Probe() {
  const { account, loading } = useAuth();
  return <div data-testid="probe">{loading ? 'loading' : (account?.email ?? 'anon')}</div>;
}

describe('AuthProvider', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(api.me).mockReset();
    vi.mocked(api.logout).mockReset();
    vi.mocked(api.refreshAccessToken).mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('resolves to anon when there is no stored token', async () => {
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByTestId('probe')).toHaveTextContent('anon');
    expect(api.me).not.toHaveBeenCalled();
  });

  it('loads the account when a token is present', async () => {
    tokenStore.set({
      access_token: 'a',
      refresh_token: 'r',
      token_type: 'bearer',
      expires_in: 900,
    });
    vi.mocked(api.me).mockResolvedValue(ACCOUNT);

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByTestId('probe')).toHaveTextContent('doc@example.com');
  });

  it('auto-logs-out after the idle-timeout window with no user activity', async () => {
    vi.useFakeTimers();
    tokenStore.set({
      access_token: 'a',
      refresh_token: 'r',
      token_type: 'bearer',
      expires_in: 900,
    });
    vi.mocked(api.me).mockResolvedValue(ACCOUNT);
    vi.mocked(api.logout).mockResolvedValue(undefined);
    vi.mocked(api.refreshAccessToken).mockResolvedValue(true);

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByTestId('probe')).toHaveTextContent('doc@example.com');
    expect(api.logout).not.toHaveBeenCalled();

    await act(async () => {
      // Just past the 15-minute idle window, with no mousemove/keydown/click dispatched.
      await vi.advanceTimersByTimeAsync(15 * 60 * 1000 + 1_000);
    });
    expect(api.logout).toHaveBeenCalledOnce();
  });

  it('proactively refreshes the access token before it expires', async () => {
    vi.useFakeTimers();
    tokenStore.set({
      access_token: 'a',
      refresh_token: 'r',
      token_type: 'bearer',
      // Short-lived on purpose so the scheduled refresh (expiry - 60s skew) fires well before
      // the 15-minute idle window would, keeping this test isolated from idle-logout behavior.
      expires_in: 120,
    });
    vi.mocked(api.me).mockResolvedValue(ACCOUNT);
    vi.mocked(api.refreshAccessToken).mockResolvedValue(true);

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByTestId('probe')).toHaveTextContent('doc@example.com');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(61 * 1000);
    });
    expect(api.refreshAccessToken).toHaveBeenCalledOnce();
  });

  it('signs the account out when the scheduled refresh itself fails', async () => {
    vi.useFakeTimers();
    tokenStore.set({
      access_token: 'a',
      refresh_token: 'r',
      token_type: 'bearer',
      expires_in: 120,
    });
    vi.mocked(api.me).mockResolvedValue(ACCOUNT);
    vi.mocked(api.refreshAccessToken).mockResolvedValue(false);

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(61 * 1000);
    });
    expect(screen.getByTestId('probe')).toHaveTextContent('anon');
  });
});

describe('useRequireAuth', () => {
  function Guarded() {
    const { loading } = useRequireAuth();
    return <div data-testid="guarded">{loading ? 'loading' : 'ready'}</div>;
  }

  beforeEach(() => {
    localStorage.clear();
  });

  it('resolves without redirecting once loading settles for an anonymous user', async () => {
    render(
      <AuthProvider>
        <Guarded />
      </AuthProvider>,
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByTestId('guarded')).toHaveTextContent('ready');
  });
});

describe('AuthProvider session teardown', () => {
  function LogoutProbe() {
    const { account, loading, logout } = useAuth();
    return (
      <div>
        <span data-testid="probe">{loading ? 'loading' : (account?.email ?? 'anon')}</span>
        <button onClick={() => void logout()}>Sign out</button>
      </div>
    );
  }

  beforeEach(() => {
    localStorage.clear();
    vi.mocked(api.me).mockReset();
    vi.mocked(api.logout).mockReset().mockResolvedValue(undefined);
  });

  it('falls back to anonymous when the stored token is rejected by /me', async () => {
    // A token that outlived its session on the server: the guard sees a token and calls /me,
    // which 401s. The provider has to settle to anon rather than stay stuck on `loading`.
    tokenStore.set({
      access_token: 'stale',
      refresh_token: 'r',
      token_type: 'bearer',
      expires_in: 900,
    });
    vi.mocked(api.me).mockRejectedValue(new Error('401'));

    render(
      <AuthProvider>
        <LogoutProbe />
      </AuthProvider>,
    );
    await act(async () => {
      await Promise.resolve();
    });

    expect(api.me).toHaveBeenCalled();
    expect(screen.getByTestId('probe')).toHaveTextContent('anon');
  });

  it('clears the account when the clinician signs out explicitly', async () => {
    tokenStore.set({
      access_token: 'a',
      refresh_token: 'r',
      token_type: 'bearer',
      expires_in: 900,
    });
    vi.mocked(api.me).mockResolvedValue(ACCOUNT);

    render(
      <AuthProvider>
        <LogoutProbe />
      </AuthProvider>,
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByTestId('probe')).toHaveTextContent('doc@example.com');

    await act(async () => {
      screen.getByText('Sign out').click();
    });

    expect(api.logout).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('probe')).toHaveTextContent('anon');
  });
});

describe('AuthProvider timer guards', () => {
  function Probe2() {
    const { account, loading } = useAuth();
    return <div data-testid="probe">{loading ? 'loading' : (account?.email ?? 'anon')}</div>;
  }

  async function mount() {
    render(
      <AuthProvider>
        <Probe2 />
      </AuthProvider>,
    );
    await act(async () => {
      await Promise.resolve();
    });
  }

  beforeEach(() => {
    localStorage.clear();
    vi.mocked(api.me).mockReset().mockResolvedValue(ACCOUNT);
    vi.mocked(api.logout).mockReset().mockResolvedValue(undefined);
    vi.mocked(api.refreshAccessToken).mockReset().mockResolvedValue(true);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('schedules no proactive refresh when the token carries no known expiry', async () => {
    // Tokens written by an older build (or restored by hand) have no expiry key. The
    // scheduler must bail out rather than compute a delay from NaN and fire immediately.
    localStorage.setItem('aether_access', 'a');
    localStorage.setItem('aether_refresh', 'r');
    vi.useFakeTimers();

    await mount();
    expect(screen.getByTestId('probe')).toHaveTextContent('doc@example.com');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60 * 60 * 1000);
    });
    expect(api.refreshAccessToken).not.toHaveBeenCalled();
  });

  it('stops rearming the idle timer once the token is gone', async () => {
    tokenStore.set({
      access_token: 'a',
      refresh_token: 'r',
      token_type: 'bearer',
      expires_in: 900,
    });
    vi.useFakeTimers();
    await mount();

    // Activity while signed in rearms the existing timer rather than stacking a second one.
    await act(async () => {
      window.dispatchEvent(new Event('mousemove'));
      await Promise.resolve();
    });

    // The token disappearing out from under the provider (another tab signed out) must not
    // leave activity handlers arming a logout timer against a session that no longer exists.
    localStorage.clear();
    await act(async () => {
      window.dispatchEvent(new Event('keydown'));
      await Promise.resolve();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(16 * 60 * 1000);
    });

    expect(api.logout).not.toHaveBeenCalled();
  });
});

describe('useAuth outside a provider', () => {
  function Orphan() {
    const { account, loading, refresh, logout } = useAuth();
    return (
      <div>
        <span data-testid="orphan">{loading ? 'loading' : (account?.email ?? 'anon')}</span>
        <button onClick={() => void refresh()}>refresh</button>
        <button onClick={() => void logout()}>logout</button>
      </div>
    );
  }

  it('exposes inert defaults instead of throwing', async () => {
    // A component rendered outside AuthProvider (a stray Storybook/test mount, or a route that
    // escapes the dashboard shell) must degrade to "no session", not crash the tree.
    render(<Orphan />);
    expect(screen.getByTestId('orphan')).toHaveTextContent('loading');

    await act(async () => {
      screen.getByText('refresh').click();
      screen.getByText('logout').click();
    });
    expect(screen.getByTestId('orphan')).toHaveTextContent('loading');
  });
});
