import type { AutonomyTier, ProbabilityBand } from '@/lib/types';

const TIER: Record<AutonomyTier, { label: string; cls: string }> = {
  // Blue = informational, green = suggestive, amber = flag-for-review (architecture §8.2).
  informational: {
    label: 'Informational',
    cls: 'bg-blue-50 text-blue-700 ring-1 ring-inset ring-blue-200',
  },
  suggestive: {
    label: 'Suggestive',
    cls: 'bg-emerald-50 text-emerald-700 ring-1 ring-inset ring-emerald-200',
  },
  flag_for_review: {
    label: 'Flag for review',
    cls: 'bg-amber-50 text-amber-800 ring-1 ring-inset ring-amber-300',
  },
};

/**
 * The tier this badge should render as, for a value that arrived over the wire.
 *
 * `TIER[tier]` for an unrecognised tier is `undefined`, and the next line reads `.cls` off it —
 * so an autonomy tier this build has never heard of did not render oddly, it threw a TypeError
 * and took out whatever was rendering it. That is not hypothetical: `ReasoningTheatre` renders
 * this badge from `ev.verifier.autonomy_tier`, which is a *cast* of raw SSE JSON with no
 * validation behind it, so a backend deployed ahead of the web app — a fourth tier, a renamed
 * one — white-screens the signature screen in the middle of a live reasoning run.
 *
 * Which tier to fall back to is a safety decision, not a rendering one. Critical Safety Rule #2
 * says the more conservative classification wins when there is disagreement, and "the server
 * says a tier this build cannot interpret" is the strongest form of that disagreement. So an
 * unrecognised tier renders as flag-for-review — amber, warning icon, and (in `SuggestionCard`)
 * behind the engagement gate. Falling back to `informational` would take the one case where the
 * UI provably does not understand what it was told and render it as the quietest thing on the
 * screen.
 *
 * The fallback also says so rather than silently relabelling: the badge reads "Flag for review"
 * and carries a screen-reader-only note naming the tier that was not recognised. Degrading
 * loudly is what this codebase does everywhere else it cannot interpret its input — an
 * unreadable lab is reported unread, an unresolvable drug is a 422 — and a clinician who sees
 * an unexplained amber badge deserves the same courtesy.
 */
export function resolveTier(tier: unknown): { tier: AutonomyTier; unrecognised: string | null } {
  if (typeof tier === 'string' && tier in TIER) {
    return { tier: tier as AutonomyTier, unrecognised: null };
  }
  return {
    tier: 'flag_for_review',
    unrecognised: typeof tier === 'string' && tier.trim() ? tier : 'none',
  };
}

export function AutonomyBadge({ tier: raw }: { tier: AutonomyTier }) {
  const { tier, unrecognised } = resolveTier(raw);
  const t = TIER[tier];
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-semibold ${t.cls}`}
    >
      {tier === 'flag_for_review' && (
        <svg
          aria-hidden="true"
          className="h-3 w-3"
          fill="none"
          viewBox="0 0 24 24"
          strokeWidth={2}
          stroke="currentColor"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z"
          />
        </svg>
      )}
      {t.label}
      {unrecognised && (
        <span className="sr-only">
          {' '}
          — the autonomy tier reported for this output ({unrecognised}) is not one this version
          recognises, so it is shown at the most conservative tier.
        </span>
      )}
    </span>
  );
}

const BAND: Record<ProbabilityBand, string> = {
  high: 'bg-slate-800 text-white',
  moderate: 'bg-slate-600 text-white',
  low: 'bg-slate-300 text-slate-800',
  very_low: 'bg-slate-200 text-slate-600',
  insufficient_data: 'bg-slate-100 text-slate-500',
};

// Qualitative bands only — never numeric percentages (anti-automation-bias).
//
// Unlike the autonomy tier above, an unrecognised band is not a safety decision — a confidence
// band changes nothing about what the clinician has to do — so this degrades to neutral styling
// and prints the band as it came, rather than claiming a band the server did not send. It still
// has to not crash: `BAND[band]` for an unknown value is `undefined`, which merely styles badly,
// but `band.replace` on a null or a number thrown into this field by a non-conforming payload
// throws, and this badge sits inline in the header of every suggestion card.
//
// `replaceAll`, not `replace`: the latter substitutes only the first underscore, so any band
// name with two of them would render half-formatted.
export function ProbabilityBandBadge({ band }: { band: ProbabilityBand }) {
  const known = typeof band === 'string' && band in BAND;
  const label = typeof band === 'string' && band.trim() ? band : 'unspecified';
  return (
    <span
      className={`inline-block rounded-full px-2.5 py-1 text-xs font-medium ${
        known ? BAND[band] : 'bg-slate-100 text-slate-500'
      }`}
    >
      {label.replaceAll('_', ' ').toUpperCase()}
    </span>
  );
}

export function CantMissBadge() {
  return (
    <span className="inline-flex animate-pulse items-center gap-1.5 rounded-full bg-orange-100 px-2.5 py-1 text-xs font-bold uppercase text-orange-900 ring-1 ring-inset ring-orange-300">
      <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-orange-500" />
      Can&apos;t miss
    </span>
  );
}
