'use client';

import { useEffect } from 'react';
import { Button } from '@aether/ui';

/**
 * The route-segment backstop, for the failures the in-tree `ErrorBoundary` cannot reach.
 *
 * `components/ErrorBoundary` is mounted inside `<main>` in each group layout, so it catches a
 * *page* that fails to render and keeps the header around it. What it cannot catch is the layout
 * it is mounted in: React unwinds past a boundary to find one that is above the throw, and there
 * was nothing above these layouts at all. So every failure in the shell itself — the nav reading
 * a malformed account payload, the auth context throwing while it resolves a session, anything in
 * `(dashboard)/layout` or `(auth)/layout` before `<main>` is reached — unmounted the whole tree
 * and left the clinician on a white page mid-consultation. Next's App Router routes exactly those
 * to the nearest `error.tsx` in the parent segment, which is this file.
 *
 * The copy is the same clinical contract as `ErrorBoundary`'s, and for the same reason: a screen
 * that fails must never read as a screen with nothing to report. It says outright that content is
 * missing, and that the record is untouched — a clinician who thinks a failed save went through
 * is in a worse position than one who knows it did not.
 */
export default function RouteError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Local only. A component stack from this app names patient-facing components and can carry
    // rendered props, and DPDP data residency (CLAUDE.md pitfall #9) rules out shipping that to a
    // remote collector. `digest` is the server-side hash Next assigns, and is the only handle
    // support has for correlating a report with a server log.
    console.error('Route render failed:', error.digest ?? '(no digest)', error);
  }, [error]);

  return (
    <main className="mx-auto grid min-h-screen max-w-2xl place-items-center px-4">
      <div
        role="alert"
        className="w-full rounded-xl border border-red-200 bg-red-50 p-6 text-sm text-red-900"
      >
        <h1 className="text-base font-semibold">This screen could not be displayed</h1>
        <p className="mt-2 text-red-400">
          Something on this screen failed to load. Nothing here is a clinical finding — treat this
          screen as missing, not as empty, and do not read it as &ldquo;nothing to report&rdquo;.
        </p>
        <p className="mt-2 text-red-400">
          The patient record is unaffected and nothing was changed.
        </p>
        <div className="mt-4 flex flex-wrap gap-2">
          <Button onClick={reset}>Try again</Button>
          {/* A full navigation rather than a router push: whatever failed may have left the
              client router's cache holding the state that failed, and the point of this link is
              to get the clinician to a screen that works. */}
          <Button variant="secondary" onClick={() => window.location.assign('/patients')}>
            Back to patients
          </Button>
        </div>
        {error.digest && (
          <p className="mt-4 font-mono text-xs text-red-400">Reference: {error.digest}</p>
        )}
      </div>
    </main>
  );
}
