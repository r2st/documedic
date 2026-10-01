'use client';

import { useEffect } from 'react';

/**
 * The last boundary there is: the root layout itself failing.
 *
 * `app/error.tsx` sits *inside* the root layout, so it cannot render when the root layout is what
 * threw — and the root layout is where `AuthProvider` and the demo/offline banners mount, which
 * is real code that can fail (a corrupt stored session, a banner reading a malformed config). Next
 * replaces the entire document with this file in that case, which is why it renders its own
 * `<html>` and `<body>`: there is no layout left to provide them.
 *
 * Deliberately plain. This renders when the application shell is gone, so it depends on no
 * provider, no shared component, and no stylesheet that the failed layout was responsible for
 * loading — inline styles only. A fallback that needs the thing that just broke is not a fallback.
 *
 * Same clinical contract as everywhere else in this app: name the failure, say the record is
 * untouched, and never let a blank screen pass for an absence of findings.
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Local only — see `app/error.tsx` for why nothing from this app's stacks leaves the device.
    console.error('Application shell failed:', error.digest ?? '(no digest)', error);
  }, [error]);

  return (
    <html lang="en">
      <body style={{ margin: 0, fontFamily: 'system-ui, sans-serif', background: '#0A0A0B' }}>
        <main
          style={{
            minHeight: '100vh',
            display: 'grid',
            placeItems: 'center',
            padding: '1rem',
          }}
        >
          <div
            role="alert"
            style={{
              maxWidth: '32rem',
              width: '100%',
              border: '1px solid rgba(127, 29, 29, 0.5)',
              background: 'rgba(69, 10, 10, 0.5)',
              borderRadius: '0.75rem',
              padding: '1.5rem',
              color: '#fca5a5',
              fontSize: '0.875rem',
              lineHeight: 1.5,
            }}
          >
            <h1 style={{ fontSize: '1rem', fontWeight: 600, margin: 0 }}>
              DoAide Clinician could not start
            </h1>
            <p style={{ marginTop: '0.5rem' }}>
              The application failed to load. Nothing on this page is a clinical finding, and no
              part of the record has been shown — do not treat this as an absence of findings for
              any patient.
            </p>
            <p style={{ marginTop: '0.5rem' }}>
              The patient record is unaffected and nothing was changed.
            </p>
            <button
              type="button"
              onClick={reset}
              style={{
                marginTop: '1rem',
                padding: '0.5rem 1rem',
                borderRadius: '0.5rem',
                border: '1px solid #F0B429',
                background: '#F0B429',
                color: '#0A0A0B',
                fontSize: '0.875rem',
                cursor: 'pointer',
              }}
            >
              Reload the application
            </button>
            {error.digest && (
              <p style={{ marginTop: '1rem', fontSize: '0.75rem', fontFamily: 'monospace' }}>
                Reference: {error.digest}
              </p>
            )}
          </div>
        </main>
      </body>
    </html>
  );
}
