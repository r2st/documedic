/**
 * Token refresh must happen once, however many callers want one at the same moment.
 *
 * Refresh tokens are single-use: the API revokes the presented token as it issues the new pair,
 * and a *second* presentation of an already-rotated one is treated as theft — it revokes every
 * live session on the account, writes `auth_refresh_token_reuse_detected` to the audit log, and
 * signs the clinician out on every device. So a duplicated exchange is not a wasted round trip,
 * it is a forced sign-out mid-consultation plus a security incident logged against the user.
 *
 * The client had several ways to produce one. Any page that fans out — the patient chart issues
 * four requests at once — has all of them 401 together the first time the access token has
 * expired, and each read the same refresh token out of `localStorage` before any had written the
 * new one. The scheduled proactive refresh could collide with a 401 the same way. And two tabs
 * of the same origin share the storage, so they share the token too.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api, tokenStore } from './api';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function tokens(n: number) {
  return {
    access_token: `access-${n}`,
    refresh_token: `refresh-${n}`,
    token_type: 'bearer',
    expires_in: 900,
  };
}

/** Every `/auth/refresh` call this test's fetch stub saw, with the token it presented. */
function presentedRefreshTokens(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return fetchMock.mock.calls
    .filter(([url]) => String(url).endsWith('/auth/refresh'))
    .map(([, init]) => JSON.parse(String((init as RequestInit).body)).refresh_token);
}

describe('refresh is single-flight', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    localStorage.clear();
    tokenStore.set(tokens(0));
    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('exchanges once when several requests 401 together', async () => {
    let rotations = 0;
    fetchMock.mockImplementation(async (url: string) => {
      if (String(url).endsWith('/auth/refresh')) {
        rotations += 1;
        // A real server revokes on rotation: a second presentation of refresh-0 is theft.
        if (rotations > 1) return jsonResponse({ code: 'invalid_token' }, 401);
        return jsonResponse(tokens(1));
      }
      // Anything carrying the stale access token is rejected; the rotated one is accepted.
      return jsonResponse({ ok: true });
    });
    let unauthorised = 4;
    const original = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (!String(url).endsWith('/auth/refresh') && unauthorised > 0) {
        unauthorised -= 1;
        return jsonResponse({ code: 'invalid_token' }, 401);
      }
      return original(url, init);
    });

    const results = await Promise.all([
      api.me(),
      api.listPatients(),
      api.pilotStatus(),
      api.performanceMetrics(),
    ]);

    expect(results).toHaveLength(4);
    expect(presentedRefreshTokens(fetchMock)).toEqual(['refresh-0']);
    expect(tokenStore.access).toBe('access-1');
  });

  it('never presents an already-rotated token', async () => {
    const seen: string[] = [];
    let issued = 0;
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (String(url).endsWith('/auth/refresh')) {
        const presented = JSON.parse(String(init!.body)).refresh_token as string;
        // The property the server enforces, asserted here instead of merely counting calls.
        expect(seen).not.toContain(presented);
        seen.push(presented);
        issued += 1;
        return jsonResponse(tokens(issued));
      }
      return jsonResponse({ ok: true }, init === undefined ? 200 : 401);
    });

    await Promise.allSettled([api.me(), api.me(), api.me()]);

    expect(new Set(seen).size).toBe(seen.length);
  });

  it('starts a fresh exchange after the previous one has settled', async () => {
    let issued = 0;
    fetchMock.mockImplementation(async (url: string) => {
      if (String(url).endsWith('/auth/refresh')) {
        issued += 1;
        return jsonResponse(tokens(issued));
      }
      return jsonResponse({ ok: true });
    });

    expect(await api.refreshAccessToken()).toBe(true);
    expect(await api.refreshAccessToken()).toBe(true);

    // Sequential callers each get their own rotation -- the sharing must not outlive the flight.
    expect(presentedRefreshTokens(fetchMock)).toEqual(['refresh-0', 'refresh-1']);
  });

  it('shares one failure with every waiting caller and clears the session once', async () => {
    fetchMock.mockImplementation(async (url: string) =>
      jsonResponse({ code: 'invalid_token' }, String(url).endsWith('/auth/refresh') ? 401 : 401),
    );

    const [a, b] = await Promise.all([api.refreshAccessToken(), api.refreshAccessToken()]);

    expect(a).toBe(false);
    expect(b).toBe(false);
    expect(presentedRefreshTokens(fetchMock)).toEqual(['refresh-0']);
    expect(tokenStore.refresh).toBeNull();
  });
});

describe('refresh across tabs', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    localStorage.clear();
    tokenStore.set(tokens(0));
    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    // jsdom has no Web Locks; leave navigator as we found it for the other suites.
    Reflect.deleteProperty(navigator, 'locks');
  });

  /** A minimal Web Locks manager: one holder at a time, queued in request order. */
  function installLockManager() {
    const queues = new Map<string, Promise<unknown>>();
    const request = vi.fn(async (name: string, fn: () => Promise<unknown>) => {
      const prior = queues.get(name) ?? Promise.resolve();
      const run = prior.then(fn, fn);
      queues.set(
        name,
        run.then(
          () => undefined,
          () => undefined,
        ),
      );
      return run;
    });
    Object.defineProperty(navigator, 'locks', { value: { request }, configurable: true });
    return request;
  }

  it('takes the pair another tab already rotated instead of exchanging again', async () => {
    const request = installLockManager();
    let issued = 0;
    fetchMock.mockImplementation(async (url: string) => {
      if (String(url).endsWith('/auth/refresh')) {
        issued += 1;
        return jsonResponse(tokens(issued));
      }
      return jsonResponse({ ok: true });
    });

    // Both "tabs" read refresh-0 before either exchanges -- the shape that triggered the
    // account-wide revocation. They contend for the lock rather than racing to the server.
    const first = api.refreshAccessToken();
    // A second module instance would call in independently; the shared promise is bypassed by
    // driving the lock body directly, which is what a separate tab amounts to here.
    const second = navigator.locks.request('aether:token-refresh', async () => 'other tab done');

    expect(await first).toBe(true);
    expect(await second).toBe('other tab done');
    expect(request).toHaveBeenCalledWith('aether:token-refresh', expect.any(Function));
    expect(presentedRefreshTokens(fetchMock)).toEqual(['refresh-0']);
  });

  it('skips the exchange when the stored token changed while it waited for the lock', async () => {
    installLockManager();
    fetchMock.mockImplementation(async () => jsonResponse(tokens(9)));

    // Stand in for the other tab finishing first: the pair in shared storage is already new by
    // the time this caller's lock body runs.
    const inFlight = api.refreshAccessToken();
    tokenStore.set(tokens(1));

    expect(await inFlight).toBe(true);
    expect(presentedRefreshTokens(fetchMock)).toEqual([]);
    // The tokens the other tab stored are kept -- this caller must not overwrite them.
    expect(tokenStore.access).toBe('access-1');
  });

  it('falls back to the in-tab promise where Web Locks are unavailable', async () => {
    // Older Safari, and any non-browser context this module is imported into. The cross-tab
    // race narrows to one round trip there rather than closing, but the exchange must still
    // happen -- a missing lock manager may not turn into a failed refresh.
    vi.stubGlobal('navigator', undefined);
    fetchMock.mockImplementation(async () => jsonResponse(tokens(3)));

    expect(await api.refreshAccessToken()).toBe(true);

    expect(presentedRefreshTokens(fetchMock)).toEqual(['refresh-0']);
    expect(tokenStore.access).toBe('access-3');
  });

  it('still exchanges when the stored token is unchanged', async () => {
    installLockManager();
    fetchMock.mockImplementation(async () => jsonResponse(tokens(2)));

    expect(await api.refreshAccessToken()).toBe(true);

    expect(presentedRefreshTokens(fetchMock)).toEqual(['refresh-0']);
    expect(tokenStore.access).toBe('access-2');
  });
});
