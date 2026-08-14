import Link from 'next/link';

/**
 * A route that does not exist — in practice, a mistyped or stale patient URL.
 *
 * Next's built-in 404 is a bare "This page could not be found" with no way out of it, which in
 * this app is reached by editing a patient id in the address bar or following a bookmark to a
 * record that has since been removed. Both leave a clinician on a dead screen with the chart they
 * were looking for nowhere in reach.
 *
 * The wording matters for the same reason the error boundaries' does: "not found" must not be
 * heard as "this patient has no record". It says which of the two this is.
 */
export default function NotFound() {
  return (
    <main className="mx-auto grid min-h-screen max-w-2xl place-items-center px-4">
      <div className="w-full rounded-xl border border-slate-200 bg-white p-6 text-sm text-slate-700">
        <h1 className="text-base font-semibold text-slate-900">This page does not exist</h1>
        <p className="mt-2">
          The address could not be matched to a screen in Aether Clinician. This is a broken link,
          not a statement about any patient&rsquo;s record.
        </p>
        <Link
          href="/patients"
          className="mt-4 inline-flex items-center rounded-lg bg-teal-600 px-4 py-2 text-sm font-medium text-white hover:bg-teal-700 focus:outline-none focus:ring-2 focus:ring-teal-500 focus:ring-offset-2"
        >
          Back to patients
        </Link>
      </div>
    </main>
  );
}
