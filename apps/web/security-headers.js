/**
 * The security response headers this app serves, as data.
 *
 * Its own module rather than inline in `next.config.js` so it can be asserted on directly. A
 * Content-Security-Policy is a header nobody reads again after the day it is written, and a
 * directive quietly dropped or widened is invisible in every functional test — the app keeps
 * working, which is exactly the problem. See `security-headers.test.ts`.
 */

/**
 * Origins the browser is allowed to open a connection to.
 *
 * This is the directive that carries the most weight in this application, and it is not really
 * about XSS. Under the DPDP Act patient data is processed here on a purpose limitation, and the
 * frontend holds a decrypted chart in memory; `connect-src` is the browser-enforced statement of
 * where that chart is permitted to go. With `default-src 'self'` alone, injected script — or a
 * well-meaning dependency that phones home — can `fetch()` any host it likes.
 *
 * Derived from the API URL rather than hard-coded, because it must follow the deployment: the API
 * is a separate subdomain in production (`api.documedic.aiknol.com`) and a different port in dev.
 * The `ws(s)` twin is the same origin over the WebSocket scheme, which `connect-src` treats as a
 * distinct source expression even though it is the same host.
 */
function apiOrigins(apiUrl) {
  const configured = apiUrl ?? process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';
  let parsed;
  try {
    parsed = new URL(configured);
  } catch {
    // A malformed NEXT_PUBLIC_API_URL must not silently produce a policy that blocks the API and
    // takes the whole app down at runtime. Fail loudly at build time instead.
    throw new Error(
      `NEXT_PUBLIC_API_URL is not a valid absolute URL (${configured}); ` +
        'the Content-Security-Policy connect-src is derived from it.',
    );
  }
  const socketScheme = parsed.protocol === 'https:' ? 'wss:' : 'ws:';
  return [parsed.origin, `${socketScheme}//${parsed.host}`];
}

/**
 * The page-level Content-Security-Policy.
 *
 * The API has had one since P1-11b; this app — the one that actually renders HTML, and renders
 * LLM-sourced clinical prose inside it — had none. Only `X-Frame-Options`, `nosniff` and a
 * referrer policy were set, so the surface where an injection would land was the surface with no
 * policy on it.
 *
 * `script-src` keeps `'unsafe-inline'` because the App Router streams its RSC payload through
 * inline `<script>` tags (`self.__next_f.push`). Removing it needs a per-request nonce, which
 * needs middleware, which opts every route out of static prerendering — a real cost to a
 * clinician opening a chart, paid for a directive that is not where this app's risk sits. The
 * directives that *are* load-bearing here — `connect-src`, `object-src`, `base-uri`,
 * `form-action`, `frame-ancestors` — are all exact.
 *
 * `'unsafe-eval'` is dev-only: React Fast Refresh and the webpack dev runtime evaluate generated
 * code. Production never gets it, and `security-headers.test.ts` pins that.
 */
function contentSecurityPolicy({ dev = process.env.NODE_ENV !== 'production', apiUrl } = {}) {
  const connect = ["'self'", 'https://analytics.doaide.com', ...apiOrigins(apiUrl)];
  return [
    "default-src 'self'",
    `script-src 'self' 'unsafe-inline' https://analytics.doaide.com${dev ? " 'unsafe-eval'" : ''}`,
    // Google Fonts is imported from globals.css, so the stylesheet host must be allowed here and
    // the font host in font-src below. Both are exact origins, not a wildcard.
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' data: https://fonts.gstatic.com",
    // `blob:` covers a document preview built from a downloaded file; `data:` covers inline SVG
    // icons. Neither can reach off-origin.
    "img-src 'self' data: blob:",
    `connect-src ${connect.join(' ')}`,
    // Nothing in this app embeds a plugin or another document, and both are routes an injection
    // uses to escape the policy above.
    "object-src 'none'",
    "frame-src 'none'",
    // Without this a single injected <base> tag silently re-points every relative script URL.
    "base-uri 'none'",
    // A form posting a chart to another origin is never legitimate here.
    "form-action 'self'",
    // The same statement X-Frame-Options makes, for browsers that prefer CSP; kept alongside it
    // rather than instead of it, because the two are read by different browser generations.
    "frame-ancestors 'none'",
  ].join('; ');
}

/** Every security header served on every route. */
function securityHeaders(options) {
  return [
    { key: 'X-Content-Type-Options', value: 'nosniff' },
    { key: 'X-Frame-Options', value: 'DENY' },
    { key: 'Referrer-Policy', value: 'no-referrer' },
    { key: 'Content-Security-Policy', value: contentSecurityPolicy(options) },
    // Chart data is read on shared clinic workstations, and the browser should not be offering
    // the page hardware it has no use for.
    {
      key: 'Permissions-Policy',
      value: 'camera=(), microphone=(), geolocation=(), payment=()',
    },
  ];
}

module.exports = { apiOrigins, contentSecurityPolicy, securityHeaders };
