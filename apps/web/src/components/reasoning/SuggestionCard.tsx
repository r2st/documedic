'use client';

import { useState } from 'react';
import type { AutonomyTier, ClinicalSuggestion } from '@/lib/types';
import { api } from '@/lib/api';
import { Button } from '@/components/ui';
import { AutonomyBadge, CantMissBadge, ProbabilityBandBadge } from './badges';

interface EvidenceItem {
  text: string;
  source?: string;
  source_ref?: string | null;
}

/**
 * Coerce a value to a renderable string. Reasoning output is LLM-generated, and a
 * non-conforming model can return an object/array where a string is expected (e.g. a
 * rationale shaped `{evidence, probability}`). Rendering that object directly throws React
 * error #31 and white-screens this clinical UI, so every LLM-sourced string is funneled
 * through here as a last line of defense.
 */
function asText(value: unknown): string {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) return value.map(asText).filter(Boolean).join('; ');
  if (typeof value === 'object') {
    const o = value as Record<string, unknown>;
    for (const k of ['text', 'rationale', 'summary', 'reason', 'explanation', 'evidence']) {
      if (typeof o[k] === 'string' && o[k]) return o[k] as string;
    }
    return Object.values(o).map(asText).filter(Boolean).join('; ');
  }
  return '';
}

function EvidenceList({ items, kind }: { items: EvidenceItem[]; kind: 'for' | 'against' }) {
  if (!items.length) return null;
  const styles =
    kind === 'for'
      ? {
          text: 'text-emerald-800',
          bg: 'bg-emerald-50',
          icon: 'text-emerald-500',
          marker: '✓',
          label: 'supporting',
        }
      : {
          text: 'text-red-800',
          bg: 'bg-red-50',
          icon: 'text-red-500',
          marker: '✗',
          label: 'against',
        };
  return (
    <div className={`rounded-lg ${styles.bg} p-3`}>
      <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-500">
        Evidence {styles.label}
      </p>
      <ul className="space-y-1.5">
        {items.map((e, i) => (
          <li key={i} className={`flex items-start gap-2 text-sm ${styles.text}`}>
            {/* The tick/cross repeats the "Evidence supporting"/"Evidence against" heading above;
                read aloud on every row it is noise, so it stays decorative. */}
            <span aria-hidden="true" className={`mt-0.5 flex-shrink-0 ${styles.icon}`}>
              {styles.marker}
            </span>
            <span>
              {asText(e.text)}
              {e.source_ref ? (
                <span className="ml-1 text-xs text-slate-400">({asText(e.source_ref)})</span>
              ) : null}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function DevilsAdvocate({ critique }: { critique: Record<string, unknown> }) {
  // Anti-automation-bias: ALWAYS visible, never collapsed by default.
  const disconfirming = ((critique.disconfirming_evidence as unknown[]) ?? [])
    .map(asText)
    .filter(Boolean);
  const alternatives = ((critique.alternative_explanations as unknown[]) ?? [])
    .map(asText)
    .filter(Boolean);
  const baseRate = asText(critique.base_rate_caveat);
  const summary = asText(critique.summary);
  return (
    <div className="mt-4 rounded-xl border-l-4 border-purple-500 bg-purple-50 p-4">
      <div className="mb-2 flex items-center gap-2">
        <svg
          aria-hidden="true"
          className="h-4 w-4 text-purple-600"
          fill="none"
          viewBox="0 0 24 24"
          strokeWidth={1.5}
          stroke="currentColor"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M12 18v-5.25m0 0a6.01 6.01 0 0 0 1.5-.189m-1.5.189a6.01 6.01 0 0 1-1.5-.189m3.75 7.478a12.06 12.06 0 0 1-4.5 0m3.75 2.383a14.406 14.406 0 0 1-3 0M14.25 18v-.192c0-.983.658-1.823 1.508-2.316a7.5 7.5 0 1 0-7.517 0c.85.493 1.509 1.333 1.509 2.316V18"
          />
        </svg>
        <p className="text-xs font-bold uppercase tracking-wider text-purple-800">
          Devil&apos;s advocate — counter-argument
        </p>
      </div>
      {summary && <p className="text-sm text-purple-900">{summary}</p>}
      {disconfirming.length > 0 && (
        <ul className="mt-2 space-y-1 pl-6 text-sm text-purple-900">
          {disconfirming.map((d, i) => (
            <li key={i} className="list-disc">
              {d}
            </li>
          ))}
        </ul>
      )}
      {alternatives.length > 0 && (
        <p className="mt-2 text-sm text-purple-900">
          <span className="font-medium">Alternatives to consider: </span>
          {alternatives.join(', ')}
        </p>
      )}
      {baseRate && <p className="mt-2 text-xs italic text-purple-700">{baseRate}</p>}
    </div>
  );
}

export function SuggestionCard({
  suggestion,
  sessionId,
}: {
  suggestion: ClinicalSuggestion;
  sessionId: string;
}) {
  const s = suggestion;
  const [acknowledged, setAcknowledged] = useState(s.autonomy_tier !== 'flag_for_review');
  const [overrideReason, setOverrideReason] = useState('');
  const [decided, setDecided] = useState<string | null>(null);

  const evFor = (s.evidence.evidence_for as EvidenceItem[]) ?? [];
  const evAgainst = (s.evidence.evidence_against as EvidenceItem[]) ?? [];
  const hasDevil = s.devils_advocate && Object.keys(s.devils_advocate).length > 0;

  async function record(decision: string, reason?: string) {
    await api.recordDecision(sessionId, s.id, decision, reason);
    setDecided(decision);
  }

  if (s.is_hard_block) {
    const reasonId = `override-reason-${s.id}`;
    const overrideHintId = `override-hint-${s.id}`;
    return (
      // A hard block stops the clinician from proceeding; it is not something to be discovered
      // by wandering the page, so it is announced the moment it renders.
      <div
        role="alert"
        className="animate-slide-up rounded-xl border-2 border-red-600 bg-red-50 p-5 shadow-sm"
      >
        <div className="mb-2 flex items-center gap-2">
          <svg
            aria-hidden="true"
            className="h-5 w-5 text-red-600"
            fill="none"
            viewBox="0 0 24 24"
            strokeWidth={2}
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M18.364 18.364A9 9 0 0 0 5.636 5.636m12.728 12.728A9 9 0 0 1 5.636 5.636m12.728 12.728L5.636 5.636"
            />
          </svg>
          <p className="text-sm font-bold uppercase tracking-wide text-red-800">
            Hard block — cannot proceed
          </p>
        </div>
        <p className="text-sm text-red-900">{asText(s.body)}</p>
        {decided ? (
          <p
            role="status"
            className="mt-3 rounded-lg bg-red-100 px-3 py-2 text-xs font-medium text-red-800"
          >
            Recorded decision: {decided}
          </p>
        ) : (
          <div className="mt-4 space-y-3 border-t border-red-200 pt-4">
            {/* This field had a placeholder and no label. A placeholder is not an accessible
                name — it vanishes on the first keystroke, and it left the one control that
                documents an override of a clinical hard block announced as "edit text, blank". */}
            <label htmlFor={reasonId} className="block text-sm font-medium text-red-900">
              Reason for overriding this hard block
            </label>
            <textarea
              id={reasonId}
              value={overrideReason}
              onChange={(e) => setOverrideReason(e.target.value)}
              placeholder="Documented clinical reasoning is required to override a hard block."
              className="w-full rounded-lg border-red-300 bg-white"
              rows={2}
            />
            <div className="flex flex-col gap-2 sm:flex-row">
              <Button
                className="w-full sm:w-auto"
                variant="danger"
                disabled={!overrideReason.trim()}
                aria-describedby={overrideHintId}
                onClick={() => record('overridden', overrideReason)}
              >
                Override with documented reason
              </Button>
              <Button
                className="w-full sm:w-auto"
                variant="secondary"
                onClick={() => record('acknowledged')}
              >
                Acknowledge (do not override)
              </Button>
            </div>
            {/* A disabled control with no stated reason is a dead end for anyone who cannot see
                that the field above it is empty. */}
            <p id={overrideHintId} className="sr-only">
              {overrideReason.trim()
                ? 'A reason is recorded; the override can be submitted.'
                : 'Enter a reason above to enable the override.'}
            </p>
          </div>
        )}
      </div>
    );
  }

  const tierBorder: Record<AutonomyTier, string> = {
    informational: 'border-blue-200',
    suggestive: 'border-emerald-200',
    flag_for_review: 'border-amber-300',
  };

  return (
    <div
      className={`animate-slide-up rounded-xl border bg-white p-5 shadow-card ${tierBorder[s.autonomy_tier]}`}
    >
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <AutonomyBadge tier={s.autonomy_tier} />
        {s.cant_miss_flag && <CantMissBadge />}
        {s.confidence_band && <ProbabilityBandBadge band={s.confidence_band} />}
        <span className="rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-medium text-slate-500">
          {s.output_type.replace(/_/g, ' ')}
        </span>
      </div>

      {/* Engagement gate for flag-for-review: clinician must actively click through. */}
      {!acknowledged ? (
        <div className="rounded-xl border-2 border-dashed border-amber-300 bg-amber-50 p-5 text-center">
          <svg
            aria-hidden="true"
            className="mx-auto mb-2 h-8 w-8 text-amber-500"
            fill="none"
            viewBox="0 0 24 24"
            strokeWidth={1.5}
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z"
            />
          </svg>
          <p className="mb-3 text-sm font-medium text-amber-900">
            This output is flagged for review. Engage to see the evidence and assessment.
          </p>
          <Button
            onClick={() => {
              setAcknowledged(true);
              void record('acknowledged');
            }}
          >
            Review evidence & assessment
          </Button>
        </div>
      ) : (
        <>
          {/* Evidence BEFORE conclusion (anti-automation-bias ordering). */}
          {(evFor.length > 0 || evAgainst.length > 0) && (
            <div className="grid gap-3 sm:grid-cols-2">
              <EvidenceList items={evFor} kind="for" />
              <EvidenceList items={evAgainst} kind="against" />
            </div>
          )}

          {/* Conclusion comes after the evidence. */}
          <div className="mt-4 border-t border-slate-100 pt-3">
            <p className="mb-1 text-xs font-semibold uppercase tracking-wider text-slate-400">
              Assessment to consider
            </p>
            <p className="text-base font-semibold text-slate-900">{asText(s.title)}</p>
            {asText(s.body) && (
              <p className="mt-1.5 text-sm leading-relaxed text-slate-600">{asText(s.body)}</p>
            )}
          </div>

          {hasDevil && <DevilsAdvocate critique={s.devils_advocate} />}

          {s.citations.length > 0 && (
            <div className="mt-4 rounded-xl bg-slate-50 p-4">
              <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-500">
                Guideline citations (grounding)
              </p>
              <div className="space-y-2">
                {s.citations.map((c, i) => (
                  <div key={i} className="border-l-2 border-slate-300 pl-3">
                    <p className="text-xs font-medium text-slate-700">
                      <span className="rounded bg-brand-50 px-1.5 py-0.5 text-brand-700 ring-1 ring-brand-200">
                        {c.source.toUpperCase()}
                      </span>{' '}
                      {c.document_title}
                      {c.heading ? ` — ${c.heading}` : ''}{' '}
                      <span className="text-slate-400">[{c.section_id}]</span>
                    </p>
                    {c.snippet && (
                      <p className="mt-1 text-xs italic text-slate-500">
                        &ldquo;{c.snippet}&rdquo;
                      </p>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {decided && (
            // The only confirmation that a decision reached the audit log, rendered without
            // moving focus off the button that was just pressed.
            <p
              role="status"
              className="mt-3 rounded-lg bg-slate-50 px-3 py-2 text-xs font-medium text-slate-500"
            >
              Recorded decision: {decided}
            </p>
          )}
        </>
      )}
    </div>
  );
}
