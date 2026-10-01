'use client';

import type { LongitudinalRecord } from '@/lib/types';

type TimelineKind =
  'medication_start' | 'medication_stop' | 'lab_result' | 'condition' | 'derived_marker';

interface TimelineEntry {
  id: string;
  date: string; // ISO date/datetime, always present for entries in this array
  kind: TimelineKind;
  title: string;
  detail: string | null;
}

const KIND_META: Record<TimelineKind, { label: string; dot: string; icon: JSX.Element }> = {
  medication_start: {
    label: 'Medication started',
    dot: 'bg-blue-500',
    icon: (
      <svg
        className="h-4 w-4"
        fill="none"
        viewBox="0 0 24 24"
        strokeWidth={2}
        stroke="currentColor"
      >
        <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
      </svg>
    ),
  },
  medication_stop: {
    label: 'Medication stopped',
    dot: 'bg-[#6B7280]',
    icon: (
      <svg
        className="h-4 w-4"
        fill="none"
        viewBox="0 0 24 24"
        strokeWidth={2}
        stroke="currentColor"
      >
        <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 12h-15" />
      </svg>
    ),
  },
  lab_result: {
    label: 'Lab result',
    dot: 'bg-emerald-500',
    icon: (
      <svg
        className="h-4 w-4"
        fill="none"
        viewBox="0 0 24 24"
        strokeWidth={2}
        stroke="currentColor"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M9.75 3.104v5.714a2.25 2.25 0 0 1-.659 1.591L5 14.5m4.75-11.396c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 0 1 4.5 0m0 0v5.714c0 .597.237 1.17.659 1.591L19.8 15.3M14.25 3.104c.251.023.501.05.75.082M19.8 15.3l-1.57.393A9.065 9.065 0 0 1 12 15a9.065 9.065 0 0 0-6.23.693L5 14.5m14.8.8 1.402 1.402c1.232 1.232.65 3.318-1.067 3.611A48.309 48.309 0 0 1 12 21c-2.773 0-5.491-.235-8.135-.687-1.718-.293-2.3-2.379-1.067-3.61L5 14.5"
        />
      </svg>
    ),
  },
  condition: {
    label: 'Condition noted',
    dot: 'bg-amber-500',
    icon: (
      <svg
        className="h-4 w-4"
        fill="none"
        viewBox="0 0 24 24"
        strokeWidth={2}
        stroke="currentColor"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M9 12h3.75M9 15h3.75M9 18h3.75m3 .75H18a2.25 2.25 0 0 0 2.25-2.25V6.108c0-1.135-.845-2.098-1.976-2.192a48.424 48.424 0 0 0-1.123-.08m-5.801 0c-.065.21-.1.433-.1.664 0 .414.336.75.75.75h4.5a.75.75 0 0 0 .75-.75 2.25 2.25 0 0 0-.1-.664m-5.8 0A2.251 2.251 0 0 1 13.5 2.25H15c1.012 0 1.867.668 2.15 1.586m-5.8 0c-.376.023-.75.05-1.124.08C9.095 4.01 8.25 4.973 8.25 6.108V8.25m0 0H4.875c-.621 0-1.125.504-1.125 1.125v11.25c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125V9.375c0-.621-.504-1.125-1.125-1.125H8.25Z"
        />
      </svg>
    ),
  },
  derived_marker: {
    label: 'Marker computed',
    dot: 'bg-purple-500',
    icon: (
      <svg
        className="h-4 w-4"
        fill="none"
        viewBox="0 0 24 24"
        strokeWidth={2}
        stroke="currentColor"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M15.75 15.75V18m-7.5-6.75h.008v.008H8.25v-.008Zm0 2.25h.008v.008H8.25V13.5Zm0 2.25h.008v.008H8.25v-.008Zm0 2.25h.008v.008H8.25V18Zm2.498-6.75h.007v.008h-.007v-.008Zm0 2.25h.007v.008h-.007V13.5Zm0 2.25h.007v.008h-.007v-.008Zm0 2.25h.007v.008h-.007V18Zm2.504-6.75h.008v.008h-.008v-.008Zm0 2.25h.008v.008h-.008V13.5Zm0 2.25h.008v.008h-.008v-.008Zm0 2.25h.008v.008h-.008V18Zm2.498-6.75h.008v.008h-.008v-.008Zm0 2.25h.008v.008h-.008V13.5ZM8.25 6h7.5v2.25h-7.5V6ZM12 2.25c-1.892 0-3.758.11-5.593.322C5.307 2.7 4.5 3.65 4.5 4.757V19.5a2.25 2.25 0 0 0 2.25 2.25h10.5a2.25 2.25 0 0 0 2.25-2.25V4.757c0-1.108-.806-2.057-1.907-2.185A48.507 48.507 0 0 0 12 2.25Z"
        />
      </svg>
    ),
  },
};

function str(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

function num(value: unknown): number | string | null {
  if (typeof value === 'number') return value;
  if (typeof value === 'string' && value.length > 0) return value;
  return null;
}

/** Builds a chronological (most-recent-first) list of dated events from a LongitudinalRecord.
 * Entries with no usable date (e.g. allergies, which carry no date field in this schema) are
 * intentionally excluded rather than given a fabricated date -- callers should render those
 * separately. */
export function buildTimeline(record: LongitudinalRecord | null | undefined): TimelineEntry[] {
  const entries: TimelineEntry[] = [];
  if (!record) return entries;

  for (const med of record.medications ?? []) {
    const id = str(med.id) ?? crypto.randomUUID();
    const name = str(med.generic_name) ?? str(med.brand_name_raw) ?? 'Unknown medication';
    const dose = [str(med.dose), str(med.frequency)].filter(Boolean).join(' · ') || null;
    const start = str(med.event_date);
    const end = str(med.end_date);
    if (start) {
      entries.push({
        id: `${id}-start`,
        date: start,
        kind: 'medication_start',
        title: name,
        detail: dose,
      });
    }
    if (end && end !== start) {
      entries.push({
        id: `${id}-stop`,
        date: end,
        kind: 'medication_stop',
        title: name,
        detail: dose,
      });
    }
  }

  for (const lab of record.lab_results ?? []) {
    const id = str(lab.id) ?? crypto.randomUUID();
    const date = str(lab.sample_date);
    if (!date) continue;
    const marker = str(lab.marker_name) ?? 'Lab result';
    const value = num(lab.value_numeric);
    const unit = str(lab.unit);
    const abnormal = lab.is_abnormal === true;
    const detail =
      value !== null ? `${value}${unit ? ` ${unit}` : ''}${abnormal ? ' (abnormal)' : ''}` : null;
    entries.push({ id, date, kind: 'lab_result', title: marker, detail });
  }

  for (const cond of record.conditions ?? []) {
    const id = str(cond.id) ?? crypto.randomUUID();
    const date = str(cond.onset_date);
    if (!date) continue;
    const name = str(cond.condition_name) ?? 'Condition';
    const status = str(cond.status);
    entries.push({ id, date, kind: 'condition', title: name, detail: status });
  }

  for (const marker of record.derived_markers ?? []) {
    const id = str(marker.id) ?? crypto.randomUUID();
    const date = str(marker.computed_at);
    if (!date) continue;
    const name = str(marker.marker_name) ?? 'Derived marker';
    const value = num(marker.value_numeric);
    const unit = str(marker.unit);
    const detail = value !== null ? `${value}${unit ? ` ${unit}` : ''}` : null;
    entries.push({ id, date, kind: 'derived_marker', title: name, detail });
  }

  return entries.sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0));
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

export function Timeline({ record }: { record: LongitudinalRecord | null | undefined }) {
  const entries = buildTimeline(record);
  const undatedAllergies = (record?.allergies ?? []).filter((a) => a);

  if (entries.length === 0 && undatedAllergies.length === 0) {
    return <p className="text-sm text-[#6B7280] italic">No timeline events yet.</p>;
  }

  return (
    <div className="space-y-4">
      {entries.length > 0 && (
        <ol className="relative space-y-4 border-l border-[#2A2A2D] pl-5">
          {entries.map((entry) => {
            const meta = KIND_META[entry.kind];
            return (
              <li key={entry.id} className="relative">
                <span
                  className={`absolute -left-[27px] top-0.5 flex h-4 w-4 items-center justify-center rounded-full text-white ${meta.dot}`}
                  aria-hidden
                >
                  <span className="h-1.5 w-1.5 rounded-full bg-white" />
                </span>
                <div className="flex flex-col gap-0.5 sm:flex-row sm:items-baseline sm:justify-between">
                  <div className="flex items-center gap-1.5 text-sm font-medium text-[#E5E7EB]">
                    <span aria-hidden="true" className="text-[#6B7280]">
                      {meta.icon}
                    </span>
                    {entry.title}
                  </div>
                  <span className="text-xs text-[#6B7280]">{formatDate(entry.date)}</span>
                </div>
                <p className="text-xs text-[#9CA3AF]">
                  {meta.label}
                  {entry.detail ? ` · ${entry.detail}` : ''}
                </p>
              </li>
            );
          })}
        </ol>
      )}

      {undatedAllergies.length > 0 && (
        <div>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-[#6B7280]">
            Allergies (no onset date recorded)
          </h3>
          <ul className="space-y-1.5">
            {undatedAllergies.map((allergy, i) => (
              <li key={i} className="rounded-md bg-red-950/30 px-3 py-2 text-sm text-red-400">
                {[str(allergy.allergen_name), str(allergy.severity), str(allergy.status)]
                  .filter(Boolean)
                  .join(' · ')}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
