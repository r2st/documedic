// App-level presentational components.
//
// The generic primitives that used to live here — Button, Card, ErrorBanner and the skeleton
// placeholders — now come from `@aether/ui`. What is left is what could not go with them:
// both of these take clinical vocabulary in their props (an extraction confidence band, a
// drug-safety severity and whether it is a hard block), so they describe this product's
// domain rather than a design system, and the shared library should not have to know what a
// hard block is in order to render a card.

import { checkTypeLabel } from '@/lib/safety';

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

/**
 * The style for a flag, for a `severity` string this build may not know.
 *
 * The lookup used to be `SEVERITY_STYLES[severity] ?? ''`, and the empty string is the problem:
 * a severity added to the API after this bundle shipped rendered as a bare white box with a
 * transparent left border — no colour, in the one component whose entire job is to say how
 * much a finding weighs. `lib/safety.ts` already treats an unknown severity as a real
 * possibility ("the API can add a fifth") and ranks it with the warnings; the colour has to
 * make the same assumption, or the two halves of the same decision disagree.
 *
 * `isHardBlock` is consulted first, and that ordering is the point. It is a boolean the client
 * cannot misread, it is what actually stops the prescription, and it is what the badge in this
 * card already announces — so a flag that says HARD BLOCK is red even if its severity string is
 * a word this build has never seen. Anything else unrecognised takes the amber warning
 * treatment rather than the blue informational one: of the two ways to be wrong about an
 * unknown severity, over-weighting it costs a clinician a second look and under-weighting it
 * costs the look entirely.
 */
export function severityStyle(severity: string, isHardBlock: boolean): string {
  if (isHardBlock) return SEVERITY_STYLES.hard_block;
  return SEVERITY_STYLES[severity] ?? SEVERITY_STYLES.warning;
}

/**
 * One deterministic safety finding.
 *
 * `checkType` is optional so the component keeps working for a caller that has no type to hand,
 * but every caller that has one should pass it: the severity badge alone renders a Child-Pugh
 * assessment, a duplicate-therapy note and a guideline deviation as three identical amber boxes,
 * and "which check said this" is most of what tells a clinician whether the sentence is about
 * the proposed drug or about the chart underneath it.
 */
export function SafetyFlagCard({
  severity,
  isHardBlock,
  summary,
  checkType,
}: {
  severity: string;
  isHardBlock: boolean;
  summary: string;
  checkType?: string;
}) {
  return (
    // `listitem` because the caller renders these in a `role="list"`: the count is worth
    // announcing, and a screen reader reaching "3 of 7" has the same sense of how much is on
    // this screen that a sighted clinician gets from the length of the column.
    <div
      role="listitem"
      className={`rounded-lg border-l-4 p-4 text-sm ${severityStyle(severity, isHardBlock)}`}
    >
      <div className="mb-1 flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="text-xs font-bold uppercase tracking-wider">
          {isHardBlock ? 'HARD BLOCK' : severity.replace(/_/g, ' ')}
        </span>
        {checkType ? (
          // Not colour-coded. The severity carries the colour, and giving the label its own
          // would compete with it for the same glance.
          <span className="rounded bg-white/60 px-1.5 py-0.5 text-xs font-semibold ring-1 ring-inset ring-black/10">
            {checkTypeLabel(checkType)}
          </span>
        ) : null}
      </div>
      {summary}
    </div>
  );
}
