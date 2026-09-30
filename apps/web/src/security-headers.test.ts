/**
 * The response headers this app serves.
 *
 * These are assertions on a header nobody reads again after the day it is written. A directive
 * quietly dropped or widened breaks no feature and fails no functional test — the app keeps
 * working, which is precisely why the regression would ship. So each directive that is here for
 * a reason is pinned to that reason.
 */

import { describe, expect, it } from 'vitest';

// eslint-disable-next-line @typescript-eslint/no-var-requires
const { apiOrigins, contentSecurityPolicy, securityHeaders } = require('../security-headers');

/** The policy as a directive -> source-list map, which is how a browser reads it. */
function directives(policy: string): Record<string, string[]> {
  return Object.fromEntries(
    policy
      .split(';')
      .map((part) => part.trim())
      .filter(Boolean)
      .map((part) => {
        const [name, ...sources] = part.split(/\s+/);
        return [name, sources];
      }),
  );
}

const prod = () =>
  directives(contentSecurityPolicy({ dev: false, apiUrl: 'https://api.documedic.doaide.com' }));

describe('Content-Security-Policy', () => {
  it('is served at all', () => {
    const csp = securityHeaders().find((h: { key: string }) => h.key === 'Content-Security-Policy');
    expect(csp, 'the page-rendering app was serving no CSP while the JSON API had one').toBeTruthy();
    expect(csp.value).toContain("default-src 'self'");
  });

  it('falls back to everything-from-self', () => {
    expect(prod()['default-src']).toEqual(["'self'"]);
  });

  describe('connect-src — where a decrypted chart is allowed to go', () => {
    it('names the API origin explicitly, not a wildcard', () => {
      expect(prod()['connect-src']).toEqual([
        "'self'",
        'https://api.documedic.doaide.com',
        'wss://api.documedic.doaide.com',
      ]);
    });

    it('follows the deployment instead of being hard-coded', () => {
      // The API is a different port in dev and a different subdomain in production; a policy that
      // did not track NEXT_PUBLIC_API_URL would block every request in one of the two.
      expect(directives(contentSecurityPolicy({ dev: true, apiUrl: 'http://localhost:8000' }))[
        'connect-src'
      ]).toEqual(["'self'", 'http://localhost:8000', 'ws://localhost:8000']);
    });

    it('pairs each API origin with its websocket scheme', () => {
      // connect-src treats ws:// as a distinct source expression from http://, even for the same
      // host, so the SSE/WebSocket channel needs its own entry or the Reasoning Theatre goes dark.
      expect(apiOrigins('https://api.example.org')).toEqual([
        'https://api.example.org',
        'wss://api.example.org',
      ]);
      expect(apiOrigins('http://api.example.org:3003')).toEqual([
        'http://api.example.org:3003',
        'ws://api.example.org:3003',
      ]);
    });

    it('refuses to build a policy from a malformed API URL', () => {
      // Silently producing a policy that blocks the API would take the whole app down at runtime
      // with a console error no clinician will read. This fails the build instead.
      expect(() => contentSecurityPolicy({ dev: false, apiUrl: 'api.documedic.doaide.com' })).toThrow(
        /not a valid absolute URL/,
      );
    });
  });

  describe('the directives that close escape routes', () => {
    it("forbids plugins and embedded documents", () => {
      expect(prod()['object-src']).toEqual(["'none'"]);
      expect(prod()['frame-src']).toEqual(["'none'"]);
    });

    it('forbids a <base> tag re-pointing every relative URL', () => {
      expect(prod()['base-uri']).toEqual(["'none'"]);
    });

    it('forbids a form posting a chart to another origin', () => {
      expect(prod()['form-action']).toEqual(["'self'"]);
    });

    it('refuses framing in both the CSP and the legacy header', () => {
      // Kept as a pair on purpose: the two are honoured by different browser generations.
      expect(prod()['frame-ancestors']).toEqual(["'none'"]);
      expect(
        securityHeaders().find((h: { key: string }) => h.key === 'X-Frame-Options').value,
      ).toBe('DENY');
    });
  });

  describe("'unsafe-eval'", () => {
    it('is never granted in production', () => {
      expect(prod()['script-src']).not.toContain("'unsafe-eval'");
    });

    it('is granted in development, where Fast Refresh needs it', () => {
      expect(
        directives(contentSecurityPolicy({ dev: true, apiUrl: 'http://localhost:8000' }))[
          'script-src'
        ],
      ).toContain("'unsafe-eval'");
    });
  });

  it('allows the Google Fonts hosts globals.css actually imports', () => {
    // globals.css does `@import url('https://fonts.googleapis.com/...')`, so the stylesheet host
    // and the font host both have to be named or the app renders in a fallback face. Exact
    // origins, not a wildcard — the point of naming them is that nothing else is allowed.
    expect(prod()['style-src']).toContain('https://fonts.googleapis.com');
    expect(prod()['font-src']).toContain('https://fonts.gstatic.com');
    expect(prod()['style-src']).not.toContain('*');
  });

  it('allows blob: and data: images for document previews and inline icons', () => {
    expect(prod()['img-src']).toEqual(["'self'", 'data:', 'blob:']);
  });
});

describe('the other security headers', () => {
  it('sets nosniff and a no-referrer policy', () => {
    const byKey = Object.fromEntries(
      securityHeaders().map((h: { key: string; value: string }) => [h.key, h.value]),
    );
    expect(byKey['X-Content-Type-Options']).toBe('nosniff');
    expect(byKey['Referrer-Policy']).toBe('no-referrer');
  });

  it('turns off hardware the app has no use for', () => {
    const byKey = Object.fromEntries(
      securityHeaders().map((h: { key: string; value: string }) => [h.key, h.value]),
    );
    for (const feature of ['camera', 'microphone', 'geolocation', 'payment']) {
      expect(byKey['Permissions-Policy']).toContain(`${feature}=()`);
    }
  });

  it('does not announce the framework', async () => {
    // `x-powered-by: Next.js` tells an attacker which framework advisories to try and buys
    // nothing. Asserted on the config, because the header is removed by Next rather than added.
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const nextConfig = require('../next.config.js');
    expect(nextConfig.poweredByHeader).toBe(false);
  });

  it('serves every one of them on every route', async () => {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const nextConfig = require('../next.config.js');
    const rules = await nextConfig.headers();
    const catchAll = rules.find((r: { source: string }) => r.source === '/:path*');
    expect(catchAll, 'the security headers are no longer on a catch-all route').toBeTruthy();
    expect(catchAll.headers.map((h: { key: string }) => h.key).sort()).toEqual([
      'Content-Security-Policy',
      'Permissions-Policy',
      'Referrer-Policy',
      'X-Content-Type-Options',
      'X-Frame-Options',
    ]);
  });
});
