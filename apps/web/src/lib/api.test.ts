import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api, tokenStore } from './api';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('tokenStore', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('starts empty', () => {
    expect(tokenStore.access).toBeNull();
    expect(tokenStore.refresh).toBeNull();
    expect(tokenStore.accessExpiresAt).toBeNull();
  });

  it('set() persists access/refresh and computes an expiry timestamp', () => {
    const before = Date.now();
    tokenStore.set({
      access_token: 'a1',
      refresh_token: 'r1',
      token_type: 'bearer',
      expires_in: 900,
    });
    expect(tokenStore.access).toBe('a1');
    expect(tokenStore.refresh).toBe('r1');
    expect(tokenStore.accessExpiresAt).not.toBeNull();
    expect(tokenStore.accessExpiresAt as number).toBeGreaterThanOrEqual(before + 900 * 1000);
  });

  it('clear() removes everything', () => {
    tokenStore.set({
      access_token: 'a1',
      refresh_token: 'r1',
      token_type: 'bearer',
      expires_in: 900,
    });
    tokenStore.clear();
    expect(tokenStore.access).toBeNull();
    expect(tokenStore.refresh).toBeNull();
    expect(tokenStore.accessExpiresAt).toBeNull();
  });
});

describe('api requests', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.stubGlobal('fetch', vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('login stores the returned tokens', async () => {
    const tokens = {
      access_token: 'a1',
      refresh_token: 'r1',
      token_type: 'bearer',
      expires_in: 900,
    };
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse(tokens, 200));

    const result = await api.login('doc@example.com', 'password123');
    expect(result).toEqual(tokens);
    expect(tokenStore.access).toBe('a1');
    expect(tokenStore.refresh).toBe('r1');
  });

  it('throws ApiError with the server-provided code and message on failure', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonResponse({ code: 'invalid_credentials', message: 'Invalid email or password' }, 401),
    );

    await expect(api.login('doc@example.com', 'wrong')).rejects.toMatchObject({
      status: 401,
      code: 'invalid_credentials',
      message: 'Invalid email or password',
    });
  });

  it('a 401 on an authenticated request transparently refreshes and retries once', async () => {
    tokenStore.set({
      access_token: 'stale',
      refresh_token: 'r1',
      token_type: 'bearer',
      expires_in: 900,
    });
    const account = {
      id: 'acc-1',
      email: 'doc@example.com',
      display_name: null,
      created_at: '2026-01-01T00:00:00Z',
    };
    const freshTokens = {
      access_token: 'fresh',
      refresh_token: 'r2',
      token_type: 'bearer',
      expires_in: 900,
    };

    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse({ code: 'invalid_token' }, 401)) // /auth/me with stale token
      .mockResolvedValueOnce(jsonResponse(freshTokens, 200)) // /auth/refresh
      .mockResolvedValueOnce(jsonResponse(account, 200)); // /auth/me retried

    const result = await api.me();
    expect(result).toEqual(account);
    expect(tokenStore.access).toBe('fresh');
    expect(fetch).toHaveBeenCalledTimes(3);
  });

  it('clears tokens when refresh itself fails, and does not retry forever', async () => {
    tokenStore.set({
      access_token: 'stale',
      refresh_token: 'dead',
      token_type: 'bearer',
      expires_in: 900,
    });
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse({ code: 'invalid_token' }, 401))
      .mockResolvedValueOnce(jsonResponse({ code: 'invalid_token' }, 401)); // refresh also fails

    await expect(api.me()).rejects.toBeInstanceOf(ApiError);
    expect(tokenStore.access).toBeNull();
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('logoutAllOtherSessions sends the current refresh token to keep that session alive', async () => {
    tokenStore.set({
      access_token: 'a1',
      refresh_token: 'keep-me',
      token_type: 'bearer',
      expires_in: 900,
    });
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ message: 'Revoked 1 session(s)' }, 200));

    await api.logoutAllOtherSessions();

    const [, init] = vi.mocked(fetch).mock.calls[0];
    const body = JSON.parse(String(init?.body));
    expect(body.keep_current_refresh_token).toBe('keep-me');
  });
});

describe('tokenStore on the server', () => {
  // The app is a Next.js App Router project, so these getters are evaluated during server
  // rendering too, where `localStorage` does not exist. Each guard has to return null rather
  // than throw a ReferenceError that would take the whole render down.
  beforeEach(() => {
    localStorage.clear();
    tokenStore.set({
      access_token: 'a1',
      refresh_token: 'r1',
      token_type: 'bearer',
      expires_in: 900,
    });
    vi.stubGlobal('window', undefined);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('reads as empty with no window, even when localStorage holds tokens', () => {
    expect(tokenStore.access).toBeNull();
    expect(tokenStore.refresh).toBeNull();
    expect(tokenStore.accessExpiresAt).toBeNull();
  });
});
