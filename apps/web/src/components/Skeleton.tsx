// Placeholder shapes shown while a chart, a roster or a metric set is still in flight.
//
// These were written inline three times over, and each copy had drifted: one announced itself
// to a screen reader, one did not, and a third hid only some of its boxes. That difference is
// not cosmetic here. A skeleton is a set of empty grey rectangles — it says "loading" to the
// eye and nothing at all to a screen reader, which would otherwise sit in silence and then
// find a chart that appeared without comment. `LoadingBlock` is the one place that gets right.

import type { ReactNode } from 'react';

import { Card } from './ui';

/**
 * One placeholder bar. Always `aria-hidden` — it carries no information, and the enclosing
 * {@link LoadingBlock} is what announces the wait.
 */
export function Skeleton({ className = '' }: { className?: string }) {
  return (
    <div aria-hidden="true" className={`animate-pulse rounded-md bg-slate-100 ${className}`} />
  );
}

/**
 * Wraps a set of placeholders and names what is being waited on.
 *
 * `role="status"` with `aria-busy` is the pairing that makes the wait audible: the label is
 * announced when the region appears, and assistive tech knows the content is provisional
 * rather than final. `label` should name what is being waited on — "Loading patient record",
 * not "Loading" — because a bare "loading" says nothing about which of several regions on a
 * screen is the one that has not arrived.
 */
export function LoadingBlock({
  label,
  className = '',
  children,
}: {
  label: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div role="status" aria-label={label} aria-busy="true" className={className}>
      {children}
    </div>
  );
}

/** Placeholder rows for a list of records — the patient roster, a set of results. */
export function SkeletonList({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-3">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="rounded-xl border border-slate-200/80 bg-white p-5 shadow-card">
          <div className="flex items-center gap-4">
            <Skeleton className="h-10 w-10 rounded-full" />
            <div className="flex-1">
              <Skeleton className="mb-2 h-5 w-36" />
              <Skeleton className="h-4 w-52 bg-slate-50" />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

/** Placeholder cards for a grid — the record sections, the metric tiles. */
export function SkeletonCards({
  count = 4,
  className = 'grid gap-4 md:grid-cols-2',
  cardClassName = '',
}: {
  count?: number;
  className?: string;
  cardClassName?: string;
}) {
  return (
    <div className={className}>
      {Array.from({ length: count }).map((_, i) => (
        <Card key={i} className={cardClassName}>
          <Skeleton className="mb-3 h-5 w-28" />
          <Skeleton className="h-4 w-full bg-slate-50" />
        </Card>
      ))}
    </div>
  );
}
