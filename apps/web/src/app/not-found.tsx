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
      <div className="w-full rounded-xl border border-[#2A2A2D] bg-[#1A1A1D] p-6 text-sm text-[#E5E7EB]">
        <h1 className="text-base font-semibold text-[#E5E7EB]">This page does not exist</h1>
        <p className="mt-2">
          The address could not be matched to a screen in DoAide Clinician. This is a broken link,
          not a statement about any patient&rsquo;s record.
        </p>
        <Link
          href="/patients"
          className="mt-4 inline-flex items-center rounded-lg bg-[#F0B429] px-4 py-2 text-sm font-medium text-[#0A0A0B] hover:bg-[#D4A025] focus:outline-none focus:ring-2 focus:ring-[#F0B429] focus:ring-offset-2 focus:ring-offset-[#0A0A0B]"
        >
          Back to patients
        </Link>
      </div>
    </main>
  );
}
