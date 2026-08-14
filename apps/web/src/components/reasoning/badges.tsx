import type { AutonomyTier, ProbabilityBand } from '@/lib/types';

const TIER: Record<AutonomyTier, { label: string; cls: string }> = {
  // Blue = informational, green = suggestive, amber = flag-for-review (architecture §8.2).
  informational: { label: 'Informational', cls: 'bg-blue-50 text-blue-700 ring-1 ring-inset ring-blue-200' },
  suggestive: { label: 'Suggestive', cls: 'bg-emerald-50 text-emerald-700 ring-1 ring-inset ring-emerald-200' },
  flag_for_review: {
    label: 'Flag for review',
    cls: 'bg-amber-50 text-amber-800 ring-1 ring-inset ring-amber-300',
  },
};

export function AutonomyBadge({ tier }: { tier: AutonomyTier }) {
  const t = TIER[tier];
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-semibold ${t.cls}`}>
      {tier === 'flag_for_review' && (
        <svg aria-hidden="true" className="h-3 w-3" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z" />
        </svg>
      )}
      {t.label}
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
export function ProbabilityBandBadge({ band }: { band: ProbabilityBand }) {
  return (
    <span className={`inline-block rounded-full px-2.5 py-1 text-xs font-medium ${BAND[band]}`}>
      {band.replace('_', ' ').toUpperCase()}
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
