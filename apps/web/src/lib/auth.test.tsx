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
