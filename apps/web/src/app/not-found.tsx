import Link from 'next/link';

export default function NotFound() {
  return (
    <main className="mx-auto grid min-h-screen max-w-2xl place-items-center px-4">
      <div className="w-full rounded-xl border border-[var(--da-border)] bg-[var(--da-surface)] p-6 text-sm text-[var(--da-text)]">
        <h1 className="text-base font-semibold text-[var(--da-text)]">This page does not exist</h1>
        <p className="mt-2">
          Broken link — not a clinical finding.
        </p>
        <Link
          href="/patients"
          className="mt-4 inline-flex items-center rounded-lg bg-[var(--da-primary)] px-4 py-2 text-sm font-medium text-[var(--da-text-on-primary)] hover:bg-[var(--da-primary-hover)] focus:outline-none focus:ring-2 focus:ring-[var(--da-primary)] focus:ring-offset-2"
        >
          Back to patients
        </Link>
      </div>
    </main>
  );
}
