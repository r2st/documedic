import { describe, expect, it } from 'vitest';

import { MEDICATION_STALE_AFTER_DAYS, medicationDocumentedLabel } from './medications';

const NOW = new Date('2026-08-15T09:00:00Z');

function daysBefore(days: number): string {
  const when = new Date(NOW.getTime() - days * 86_400_000);
  return when.toISOString().slice(0, 10);
}

describe('medicationDocumentedLabel', () => {
  it('shows the date a recent prescription carried, without a caution', () => {
    const label = medicationDocumentedLabel({ event_date: daysBefore(10), is_current: true }, NOW);
    expect(label.stale).toBe(false);
    expect(label.text).toContain('Documented');
  });

  it('marks a drug last documented years ago, and says how long ago', () => {
    const label = medicationDocumentedLabel({ event_date: '2019-06-01', is_current: true }, NOW);
    expect(label.stale).toBe(true);
    expect(label.text).toBe('Documented 1 Jun 2019 · over 7 years ago');
  });

  it('treats the threshold as the boundary, not a day either side', () => {
    const inside = medicationDocumentedLabel(
      { event_date: daysBefore(MEDICATION_STALE_AFTER_DAYS - 1), is_current: true },
      NOW,
    );
    const on = medicationDocumentedLabel(
      { event_date: daysBefore(MEDICATION_STALE_AFTER_DAYS), is_current: true },
      NOW,
    );
    expect(inside.stale).toBe(false);
    expect(on.stale).toBe(true);
  });

  it('does not caution about a drug the patient was taken off', () => {
    // A discontinued drug is history, and a longitudinal record is supposed to keep history. The
    // row already says "Stopped"; adding "stale" to it would be an alert about nothing.
    const label = medicationDocumentedLabel({ event_date: '2019-06-01', is_current: false }, NOW);
    expect(label.stale).toBe(false);
    expect(label.text).toBe('Documented 1 Jun 2019');
  });

  it('says so when the prescription carried no date, rather than showing nothing', () => {
    // "No date" and "today" looked identical on this screen, which is the failure the line is
    // here to fix — so the absent case has to render too.
    expect(medicationDocumentedLabel({ is_current: true }, NOW)).toEqual({
      text: 'No date on the prescription',
      stale: false,
    });
    expect(medicationDocumentedLabel({ event_date: '', is_current: true }, NOW).stale).toBe(false);
    expect(medicationDocumentedLabel({ event_date: 'not-a-date' }, NOW).text).toBe(
      'No date on the prescription',
    );
  });

  it('phrases the middle band in months', () => {
    const label = medicationDocumentedLabel({ event_date: daysBefore(240), is_current: true }, NOW);
    expect(label.text).toContain('about 8 months ago');
  });

  it('phrases a single year as over a year', () => {
    const label = medicationDocumentedLabel({ event_date: daysBefore(400), is_current: true }, NOW);
    expect(label.text).toContain('over a year ago');
  });
});
