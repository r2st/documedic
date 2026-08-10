import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Timeline, buildTimeline } from './Timeline';
import type { LongitudinalRecord } from '@/lib/types';

function record(overrides: Partial<LongitudinalRecord> = {}): LongitudinalRecord {
  return {
    patient_id: 'p1',
    medications: [],
    lab_results: [],
    conditions: [],
    allergies: [],
    derived_markers: [],
    ...overrides,
  };
}

describe('buildTimeline', () => {
  it('sorts dated entries most-recent-first across event types', () => {
    const entries = buildTimeline(
      record({
        medications: [
          { id: 'm1', generic_name: 'Metformin', event_date: '2024-01-10', dose: '500mg', frequency: 'BD' },
        ],
        lab_results: [
          { id: 'l1', marker_name: 'HbA1c', sample_date: '2024-06-01', value_numeric: 9.2, unit: '%' },
        ],
        conditions: [
          { id: 'c1', condition_name: 'Type 2 Diabetes Mellitus', onset_date: '2023-03-15', status: 'active' },
        ],
      }),
    );
    expect(entries.map((e) => e.id)).toEqual(['l1', 'm1-start', 'c1']);
  });

  it('emits a separate stop entry when a medication has an end_date different from its start', () => {
    const entries = buildTimeline(
      record({
        medications: [
          { id: 'm1', generic_name: 'Amoxicillin', event_date: '2024-01-01', end_date: '2024-01-08' },
        ],
      }),
    );
    expect(entries).toHaveLength(2);
    expect(entries.find((e) => e.kind === 'medication_start')).toBeTruthy();
    expect(entries.find((e) => e.kind === 'medication_stop')).toBeTruthy();
  });

  it('does not emit a stop entry when end_date equals event_date', () => {
    const entries = buildTimeline(
      record({
        medications: [{ id: 'm1', generic_name: 'Paracetamol', event_date: '2024-01-01', end_date: '2024-01-01' }],
      }),
    );
    expect(entries).toHaveLength(1);
  });

  it('skips entries with no usable date instead of fabricating one', () => {
    const entries = buildTimeline(
      record({
        medications: [{ id: 'm1', generic_name: 'Amlodipine', event_date: null }],
        lab_results: [{ id: 'l1', marker_name: 'Creatinine', sample_date: undefined }],
        conditions: [{ id: 'c1', condition_name: 'Hypertension' }],
      }),
    );
    expect(entries).toHaveLength(0);
  });

  it('returns an empty list for a null/undefined record', () => {
    expect(buildTimeline(null)).toEqual([]);
    expect(buildTimeline(undefined)).toEqual([]);
  });

  it('includes derived markers with their computed_at date, value and unit', () => {
    const entries = buildTimeline(
      record({
        derived_markers: [
          { id: 'd1', marker_name: 'eGFR', computed_at: '2024-05-02', value_numeric: 48, unit: 'mL/min/1.73m2' },
        ],
      }),
    );
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({
      id: 'd1',
      kind: 'derived_marker',
      title: 'eGFR',
      detail: '48 mL/min/1.73m2',
    });
  });

  it('skips derived markers with no computed_at date rather than fabricating one', () => {
    const entries = buildTimeline(
      record({ derived_markers: [{ id: 'd1', marker_name: 'BMI', computed_at: null }] }),
    );
    expect(entries).toEqual([]);
  });

  it('keeps a string-valued measurement verbatim and omits the unit when absent', () => {
    const entries = buildTimeline(
      record({
        derived_markers: [
          { id: 'd1', marker_name: 'BP category', computed_at: '2024-05-02', value_numeric: 'stage 2' },
        ],
      }),
    );
    expect(entries[0].detail).toBe('stage 2');
  });

  it('leaves the detail null when a marker carries no value at all', () => {
    const entries = buildTimeline(
      record({
        derived_markers: [{ id: 'd1', marker_name: 'BMI', computed_at: '2024-05-02', value_numeric: null }],
      }),
    );
    expect(entries[0].detail).toBeNull();
  });

  it('falls back to a generic title when a derived marker has no name', () => {
    const entries = buildTimeline(
      record({ derived_markers: [{ id: 'd1', computed_at: '2024-05-02', value_numeric: 3 }] }),
    );
    expect(entries[0].title).toBe('Derived marker');
  });

  it('tolerates a record whose event collections are absent entirely', () => {
    // The API omits empty collections rather than sending [], so every loop has to survive
    // an undefined source without throwing.
    expect(buildTimeline({ patient_id: 'p1' } as LongitudinalRecord)).toEqual([]);
  });

  it('synthesises a key for every event type when the row carries no id', () => {
    const entries = buildTimeline(
      record({
        medications: [{ generic_name: 'Metformin', event_date: '2024-01-10' }],
        lab_results: [{ marker_name: 'HbA1c', sample_date: '2024-01-11' }],
        conditions: [{ condition_name: 'T2DM', onset_date: '2024-01-12' }],
        derived_markers: [{ marker_name: 'eGFR', computed_at: '2024-01-13' }],
      }),
    );
    expect(entries).toHaveLength(4);
    const ids = entries.map((e) => e.id);
    expect(ids.every((id) => typeof id === 'string' && id.length > 0)).toBe(true);
    expect(new Set(ids).size).toBe(4);
  });

  it('falls back to the raw Indian brand name when no generic has been resolved yet', () => {
    // Vocabulary resolution can lag ingestion; the timeline must still name the drug.
    const entries = buildTimeline(
      record({ medications: [{ id: 'm1', brand_name_raw: 'Crocin', event_date: '2024-01-10' }] }),
    );
    expect(entries[0].title).toBe('Crocin');
  });

  it('labels a medication with neither generic nor brand name as unknown', () => {
    const entries = buildTimeline(record({ medications: [{ id: 'm1', event_date: '2024-01-10' }] }));
    expect(entries[0].title).toBe('Unknown medication');
    expect(entries[0].detail).toBeNull();
  });

  it('falls back to generic titles for an unnamed lab and an unnamed condition', () => {
    const entries = buildTimeline(
      record({
        lab_results: [{ id: 'l1', sample_date: '2024-02-01' }],
        conditions: [{ id: 'c1', onset_date: '2024-01-01' }],
      }),
    );
    expect(entries.find((e) => e.kind === 'lab_result')?.title).toBe('Lab result');
    expect(entries.find((e) => e.kind === 'condition')?.title).toBe('Condition');
  });

  it('omits the unit and the abnormal marker from a lab detail when neither applies', () => {
    const entries = buildTimeline(
      record({
        lab_results: [
          { id: 'l1', marker_name: 'Sodium', sample_date: '2024-02-01', value_numeric: 138, is_abnormal: false },
        ],
      }),
    );
    expect(entries[0].detail).toBe('138');
  });

  it('keeps both entries in a stable order when two events share a date', () => {
    const entries = buildTimeline(
      record({
        conditions: [
          { id: 'c1', condition_name: 'Hypertension', onset_date: '2024-01-01' },
          { id: 'c2', condition_name: 'T2DM', onset_date: '2024-01-01' },
        ],
      }),
    );
    expect(entries.map((e) => e.id)).toEqual(['c1', 'c2']);
  });
});

describe('Timeline component', () => {
  it('renders a dated event with its date and detail', () => {
    render(
      <Timeline
        record={record({
          lab_results: [
            { id: 'l1', marker_name: 'HbA1c', sample_date: '2024-06-01', value_numeric: 9.2, unit: '%', is_abnormal: true },
          ],
        })}
      />,
    );
    expect(screen.getByText('HbA1c')).toBeInTheDocument();
    expect(screen.getByText(/9\.2 %/)).toBeInTheDocument();
  });

  it('renders undated allergies in a separate section without a fabricated date', () => {
    render(
      <Timeline
        record={record({
          allergies: [{ allergen_name: 'Penicillin', severity: 'severe', status: 'active' }],
        })}
      />,
    );
    expect(screen.getByText(/Allergies \(no onset date recorded\)/)).toBeInTheDocument();
    expect(screen.getByText(/Penicillin/)).toBeInTheDocument();
  });

  it('shows an empty state when there is nothing to show', () => {
    render(<Timeline record={record()} />);
    expect(screen.getByText('No timeline events yet.')).toBeInTheDocument();
  });

  it('does not crash on a null record', () => {
    render(<Timeline record={null} />);
    expect(screen.getByText('No timeline events yet.')).toBeInTheDocument();
  });

  it('renders a derived marker row with its formatted date and value', () => {
    render(
      <Timeline
        record={record({
          derived_markers: [
            { id: 'd1', marker_name: 'eGFR', computed_at: '2024-05-02', value_numeric: 48, unit: 'mL/min' },
          ],
        })}
      />,
    );
    expect(screen.getByText('eGFR')).toBeInTheDocument();
    expect(screen.getByText(/48 mL\/min/)).toBeInTheDocument();
  });

  it('renders the kind label alone when an event has no detail to append', () => {
    render(
      <Timeline
        record={record({
          conditions: [{ id: 'c1', condition_name: 'Hypertension', onset_date: '2024-01-01' }],
        })}
      />,
    );
    // No ` · <detail>` suffix -- the label must stand on its own rather than trailing a separator.
    expect(screen.getByText('Condition noted')).toBeInTheDocument();
  });

  it('falls back to the raw string when a date cannot be parsed', () => {
    render(
      <Timeline
        record={record({
          conditions: [{ id: 'c1', condition_name: 'Hypertension', onset_date: 'not-a-date', status: 'active' }],
        })}
      />,
    );
    expect(screen.getByText('not-a-date')).toBeInTheDocument();
  });
});
