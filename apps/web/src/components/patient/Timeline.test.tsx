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
});
