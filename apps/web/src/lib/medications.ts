// How old a medication row is, said on the row itself.
//
// The record screen listed medications as generic name, dose and frequency, and no date — so a
// line charted this morning and a line lifted off a 2019 prescription were the same three words
// under the same heading. Nothing in this system ages a medication out (`is_current` is written
// at the merge and only a `stop` line clears it), which means the list a clinician reads as
// "what this patient is on" can be years old with nothing on the screen saying so.
//
// The clinical caution belongs on the safety screen, where the deterministic engine raises it as
// a `stale_medication` flag alongside the checks it qualifies. What belongs here is the plain
// fact the row already carries and was not showing: when it was documented.

/** Mirrors `app.core.safety._MEDICATION_STALE_AFTER_DAYS` — kept in step deliberately, so the
 *  row the safety screen calls stale is the row this screen marks. */
export const MEDICATION_STALE_AFTER_DAYS = 180;

export interface DocumentedLabel {
  /** What to render, e.g. "Documented 1 Jun 2019". Never empty. */
  text: string;
  /** Whether to render it as a caution. False for stopped rows: a discontinued drug is not a
   *  stale one, it is history, and the row already says "Stopped". */
  stale: boolean;
}

/**
 * The "documented on" line for one medication row.
 *
 * `event_date` is the date the prescription itself carried. A row without one is not treated as
 * stale — most of what this product ingests is handwritten and a date OCR could not read is the
 * ordinary case, not the alarming one — but it is still said out loud, because "no date" and
 * "today" must not look the same.
 *
 * `now` is injected rather than read from the clock so the boundary is testable.
 */
export function medicationDocumentedLabel(
  row: Record<string, unknown>,
  now: Date = new Date(),
): DocumentedLabel {
  const raw = row['event_date'];
  if (typeof raw !== 'string' || !raw.trim()) {
    return { text: 'No date on the prescription', stale: false };
  }
  const when = new Date(`${raw}T00:00:00Z`);
  if (Number.isNaN(when.getTime())) {
    return { text: 'No date on the prescription', stale: false };
  }
  const days = Math.floor((now.getTime() - when.getTime()) / 86_400_000);
  // A stopped drug is history, and history is allowed to be old.
  const stale = row['is_current'] !== false && days >= MEDICATION_STALE_AFTER_DAYS;
  const shown = when.toLocaleDateString('en-IN', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    timeZone: 'UTC',
  });
  return { text: `Documented ${shown}${stale ? ` · ${agoPhrase(days)}` : ''}`, stale };
}

/** "over 7 years ago" / "about 8 months ago" — the precision a clinician would use out loud. */
function agoPhrase(days: number): string {
  if (days >= 730) return `over ${Math.floor(days / 365)} years ago`;
  if (days >= 365) return 'over a year ago';
  return `about ${Math.round(days / 30)} months ago`;
}
