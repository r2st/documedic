// App-level presentational components.
//
// The generic primitives that used to live here — Button, Card, ErrorBanner and the skeleton
// placeholders — now come from `@aether/ui`. What is left is what could not go with them:
// both of these take clinical vocabulary in their props (an extraction confidence band, a
// drug-safety severity and whether it is a hard block), so they describe this product's
// domain rather than a design system, and the shared library should not have to know what a
// hard block is in order to render a card.

const CONFIDENCE_STYLES: Record<string, string> = {
  high: 'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200',
  medium: 'bg-amber-50 text-amber-700 ring-1 ring-amber-200',
  low: 'bg-red-50 text-red-700 ring-1 ring-red-200',
};

export function ConfidenceBadge({ band, value }: { band: string; value: number }) {
  // The band is conveyed visually by colour alone, and a bare "85%" next to an extracted
  // value does not say what it is a percentage of. The label carries both.
  return (
    <span
      aria-label={`Extraction confidence ${band}, ${(value * 100).toFixed(0)} percent`}
      className={`inline-block rounded-md px-2 py-0.5 text-xs font-semibold ${
        CONFIDENCE_STYLES[band] ?? 'bg-slate-50 text-slate-600 ring-1 ring-slate-200'
      }`}
    >
      {(value * 100).toFixed(0)}%
    </span>
  );
}

const SEVERITY_STYLES: Record<string, string> = {
  hard_block: 'border-red-600 bg-red-50 text-red-900',
  critical: 'border-red-500 bg-red-50 text-red-800',
  warning: 'border-amber-500 bg-amber-50 text-amber-900',
  info: 'border-brand-500 bg-brand-50 text-brand-900',
};

export function SafetyFlagCard({
  severity,
  isHardBlock,
  summary,
}: {
  severity: string;
  isHardBlock: boolean;
  summary: string;
}) {
  return (
    <div className={`rounded-lg border-l-4 p-4 text-sm ${SEVERITY_STYLES[severity] ?? ''}`}>
      <span className="mr-2 text-xs font-bold uppercase tracking-wider">
        {isHardBlock ? 'HARD BLOCK' : severity.replace(/_/g, ' ')}
      </span>
      {summary}
    </div>
  );
}
