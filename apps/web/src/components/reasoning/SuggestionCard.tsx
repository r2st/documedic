'use client';

import { useState } from 'react';
import type { AutonomyTier, ClinicalSuggestion } from '@/lib/types';
import { api } from '@/lib/api';
import { requestErrorMessage } from '@/lib/errors';
import { AutonomyBadge, CantMissBadge, ProbabilityBandBadge, resolveTier } from './badges';
import { Button, ErrorBanner } from '@aether/ui';

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

/**
 * Coerce a value to a renderable list of strings.
 *
 * `((x as unknown[]) ?? []).map(asText)` only defends against null: a *string* passes the `??`
 * and then has no `.map`, which throws while rendering and takes out the whole card. The
 * reasoning engine now coerces these fields on the way out, but suggestions are immutable and
 * already persisted, so a session recorded before that fix still returns whatever the model
 * said — and this component is the last line of defense by design.
 */
function asTextList(value: unknown): string[] {
  if (value == null) return [];
  if (Array.isArray(value)) return value.map(asText).filter(Boolean);
  const text = asText(value);
  return text ? [text] : [];
}

/**
 * Coerce the evidence field of a suggestion to renderable items.
 *
 * Same reason as `asTextList`: `(s.evidence.evidence_for as EvidenceItem[]) ?? []` lets a string
 * through, where `.length` quietly reports its character count and `.map` throws. Evidence is
 * ordered before the conclusion by design, so this throws before the clinician has seen anything
 * at all.
 */
function asEvidenceItems(value: unknown): EvidenceItem[] {
  const raw = Array.isArray(value) ? value : value == null ? [] : [value];
  return raw
    .map((e) => {
      if (e && typeof e === 'object' && !Array.isArray(e)) {
        const o = e as Record<string, unknown>;
        return { text: asText(o.text ?? o.evidence ?? o.finding ?? o), source_ref: o.source_ref };
      }
      return { text: asText(e) };
    })
    .filter((e): e is EvidenceItem => Boolean(e.text)) as EvidenceItem[];
}

interface CitationItem {
  source: string;
  document_title: string;
  heading: string;
  section_id: string;
  snippet: string;
}

/**
 * Coerce the citations field to renderable items.
 *
 * `s.citations.length` was read straight off the payload, so a response without the field — a
 * suggestion row from before citations existed, an endpoint that omits them on a list view —
 * threw on `.length` before rendering anything. Each field then had the same problem one level
 * down: `c.source.toUpperCase()` throws for a citation carrying a title but no source, which is
 * exactly the shape a partially-populated retrieval hit has.
 *
 * Citations are the grounding for a clinical claim, so the failure direction matters: a citation
 * that cannot be fully rendered is shown with the parts that survived rather than dropped, and
 * only one with nothing to show at all is discarded. Showing a claim while silently dropping the
 * guideline it was grounded in is the one outcome worth avoiding.
 */
function asCitations(value: unknown): CitationItem[] {
  const raw = Array.isArray(value) ? value : value == null ? [] : [value];
  return raw
    .map((c) => {
      const o = (c && typeof c === 'object' ? c : {}) as Record<string, unknown>;
      return {
        source: asText(o.source),
        document_title: asText(o.document_title),
        heading: asText(o.heading),
        section_id: asText(o.section_id),
        snippet: asText(o.snippet),
      };
    })
    .filter((c) => c.source || c.document_title || c.heading || c.section_id || c.snippet);
}

function EvidenceList({ items, kind }: { items: EvidenceItem[]; kind: 'for' | 'against' }) {
  if (!items.length) return null;
  const styles =
    kind === 'for'
      ? {
          text: 'text-emerald-300',
          bg: 'bg-emerald-950/30',
          icon: 'text-emerald-400',
          marker: '✓',
          label: 'supporting',
        }
      : {
          text: 'text-red-300',
          bg: 'bg-red-950/30',
          icon: 'text-red-400',
          marker: '✗',
          label: 'against',
        };
  return (
    <div className={`rounded-lg ${styles.bg} p-3`}>
      <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-[#9CA3AF]">
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
                <span className="ml-1 text-xs text-[#6B7280]">({asText(e.source_ref)})</span>
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
  const disconfirming = asTextList(critique.disconfirming_evidence);
  const alternatives = asTextList(critique.alternative_explanations);
  const baseRate = asText(critique.base_rate_caveat);
  const summary = asText(critique.summary);
  return (
    <div className="mt-4 rounded-xl border-l-4 border-purple-500 bg-purple-950/30 p-4">
      <div className="mb-2 flex items-center gap-2">
        <svg
          aria-hidden="true"
          className="h-4 w-4 text-purple-400"
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
        <p className="text-xs font-bold uppercase tracking-wider text-purple-400">
          Devil&apos;s advocate — counter-argument
        </p>
      </div>
      {summary && <p className="text-sm text-purple-300">{summary}</p>}
      {disconfirming.length > 0 && (
        <ul className="mt-2 space-y-1 pl-6 text-sm text-purple-300">
          {disconfirming.map((d, i) => (
            <li key={i} className="list-disc">
              {d}
            </li>
          ))}
        </ul>
      )}
      {alternatives.length > 0 && (
        <p className="mt-2 text-sm text-purple-300">
          <span className="font-medium">Alternatives to consider: </span>
          {alternatives.join(', ')}
        </p>
      )}
      {baseRate && <p className="mt-2 text-xs italic text-purple-400">{baseRate}</p>}
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
  // The tier this card behaves as. Resolved rather than read, because everything below keys off
  // it and `!== 'flag_for_review'` treats every value that is not that exact string — including
  // one from a newer backend, and including a missing field — as safe to show ungated. See
  // `resolveTier`: an unrecognised tier is the case where this build provably does not know what
  // it was told, so it gets the gate, not the bypass (Critical Safety Rule #2).
  const { tier } = resolveTier(s.autonomy_tier);
  const [acknowledged, setAcknowledged] = useState(tier !== 'flag_for_review');
  const [overrideReason, setOverrideReason] = useState('');
  const [decided, setDecided] = useState<string | null>(null);
  // The decision that failed to reach the audit log, kept so the banner can offer to send that
  // same decision again rather than making the clinician find the right button a second time.
  const [failed, setFailed] = useState<{
    decision: string;
    reason?: string;
    message: string;
  } | null>(null);
  const [recording, setRecording] = useState(false);

  const evFor = asEvidenceItems(s.evidence?.evidence_for);
  const evAgainst = asEvidenceItems(s.evidence?.evidence_against);
  const citations = asCitations(s.citations);
  const hasDevil =
    s.devils_advocate &&
    typeof s.devils_advocate === 'object' &&
    Object.keys(s.devils_advocate).length > 0;

  /**
   * Write a clinician decision to the immutable audit trail.
   *
   * This had no error handling at all: `api.recordDecision` rejecting left an unhandled
   * rejection, `setDecided` never ran, and the card simply stayed as it was. On a hard block
   * that is the worst possible failure mode — the clinician types their reasoning, presses
   * "Override with documented reason", and the card gives back nothing: no confirmation, no
   * error. Nothing distinguishes "the override is recorded" from "the override never left the
   * browser", and the audit trail is the whole point of the control.
   */
  async function record(decision: string, reason?: string) {
    setFailed(null);
    setRecording(true);
    try {
      await api.recordDecision(sessionId, s.id, decision, reason);
      setDecided(decision);
    } catch (err) {
      setFailed({
        decision,
        reason,
        message: requestErrorMessage(err, `this ${decision.replace(/_/g, ' ')} decision`),
      });
    } finally {
      setRecording(false);
    }
  }

  const decisionError = failed && (
    <ErrorBanner
      className="mt-3"
      message={failed.message}
      onRetry={() => void record(failed.decision, failed.reason)}
      retrying={recording}
      retryLabel="Record it again"
    />
  );

  if (s.is_hard_block) {
    const reasonId = `override-reason-${s.id}`;
    const overrideHintId = `override-hint-${s.id}`;
    return (
      // A hard block stops the clinician from proceeding; it is not something to be discovered
      // by wandering the page, so it is announced the moment it renders.
      <div
        role="alert"
        className="animate-slide-up rounded-xl border-2 border-red-600 bg-red-950/30 p-5 shadow-sm"
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
          <p className="text-sm font-bold uppercase tracking-wide text-red-300">
            Hard block — cannot proceed
          </p>
        </div>
        <p className="text-sm text-red-300">{asText(s.body)}</p>
        {decided ? (
          <p
            role="status"
            className="mt-3 rounded-lg bg-red-950/50 px-3 py-2 text-xs font-medium text-red-300"
          >
            Recorded decision: {decided}
          </p>
        ) : (
          <div className="mt-4 space-y-3 border-t border-red-800/50 pt-4">
            {/* This field had a placeholder and no label. A placeholder is not an accessible
                name — it vanishes on the first keystroke, and it left the one control that
                documents an override of a clinical hard block announced as "edit text, blank". */}
            <label htmlFor={reasonId} className="block text-sm font-medium text-red-300">
              Reason for overriding this hard block
            </label>
            <textarea
              id={reasonId}
              value={overrideReason}
              onChange={(e) => setOverrideReason(e.target.value)}
              placeholder="Documented clinical reasoning is required to override a hard block."
              className="w-full rounded-lg border-red-700 bg-[#111113] text-[#E5E7EB]"
              rows={2}
            />
            <div className="flex flex-col gap-2 sm:flex-row">
              <Button
                className="w-full sm:w-auto"
                variant="danger"
                disabled={!overrideReason.trim() || recording}
                aria-busy={recording}
                aria-describedby={overrideHintId}
                onClick={() => void record('overridden', overrideReason)}
              >
                Override with documented reason
              </Button>
              <Button
                className="w-full sm:w-auto"
                variant="secondary"
                disabled={recording}
                aria-busy={recording}
                onClick={() => void record('acknowledged')}
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
            {decisionError}
          </div>
        )}
      </div>
    );
  }

  const tierBorder: Record<AutonomyTier, string> = {
    informational: 'border-blue-800/50',
    suggestive: 'border-emerald-800/50',
    flag_for_review: 'border-amber-700',
  };

  return (
    <div
      className={`animate-slide-up rounded-xl border bg-[#1A1A1D] p-5 shadow-card ${tierBorder[tier]}`}
    >
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <AutonomyBadge tier={s.autonomy_tier} />
        {s.cant_miss_flag && <CantMissBadge />}
        {s.confidence_band && <ProbabilityBandBadge band={s.confidence_band} />}
        {/* `output_type` is a required field of the response schema and was read as one. A
            payload missing it — an older suggestion, a partial row, a proxy that dropped it —
            made this `undefined.replace(...)`, which throws and takes the card down over a
            decorative label. */}
        {asText(s.output_type) && (
          <span className="rounded-full bg-[#2A2A2D] px-2.5 py-0.5 text-xs font-medium text-[#9CA3AF]">
            {asText(s.output_type).replace(/_/g, ' ')}
          </span>
        )}
      </div>

      {/* Engagement gate for flag-for-review: clinician must actively click through. */}
      {!acknowledged ? (
        <div className="rounded-xl border-2 border-dashed border-amber-700 bg-amber-950/30 p-5 text-center">
          <svg
            aria-hidden="true"
            className="mx-auto mb-2 h-8 w-8 text-amber-400"
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
          <p className="mb-3 text-sm font-medium text-amber-300">
            Flagged for review — tap to see evidence.
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
          <div className="mt-4 border-t border-[#2A2A2D] pt-3">
            <p className="mb-1 text-xs font-semibold uppercase tracking-wider text-[#6B7280]">
              Assessment to consider
            </p>
            <p className="text-base font-semibold text-[#E5E7EB]">{asText(s.title)}</p>
            {asText(s.body) && (
              <p className="mt-1.5 text-sm leading-relaxed text-[#9CA3AF]">{asText(s.body)}</p>
            )}
          </div>

          {hasDevil && <DevilsAdvocate critique={s.devils_advocate} />}

          {citations.length > 0 && (
            <div className="mt-4 rounded-xl bg-[#111113] p-4">
              <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-[#9CA3AF]">
                Guideline citations (grounding)
              </p>
              <div className="space-y-2">
                {citations.map((c, i) => (
                  <div key={i} className="border-l-2 border-[#2A2A2D] pl-3">
                    <p className="text-xs font-medium text-[#E5E7EB]">
                      {c.source && (
                        <>
                          <span className="rounded bg-[#F0B429]/10 px-1.5 py-0.5 text-[#F0B429] ring-1 ring-[#F0B429]/30">
                            {c.source.toUpperCase()}
                          </span>{' '}
                        </>
                      )}
                      {c.document_title}
                      {c.heading ? ` — ${c.heading}` : ''}{' '}
                      {c.section_id && <span className="text-[#6B7280]">[{c.section_id}]</span>}
                    </p>
                    {c.snippet && (
                      <p className="mt-1 text-xs italic text-[#9CA3AF]">
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
              className="mt-3 rounded-lg bg-[#111113] px-3 py-2 text-xs font-medium text-[#9CA3AF]"
            >
              Recorded decision: {decided}
            </p>
          )}

          {/* The evidence stays revealed even when the acknowledgement fails to record: the
              clinician did engage, and hiding a flagged output behind a network failure would
              be the worse of the two errors. What must not happen silently is the audit log
              missing that engagement, so the failure says so and offers to send it again. */}
          {decisionError}
        </>
      )}
    </div>
  );
}
