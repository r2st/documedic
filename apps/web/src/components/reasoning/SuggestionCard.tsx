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

function EvidenceList({ items, kind }: { items: EvidenceItem[]; kind: 'for' | 'against' }) {
  if (!items?.length) return null;
  const color = kind === 'for' ? 'text-green-800' : 'text-red-800';
  const marker = kind === 'for' ? '✓' : '✗';
  return (
    <div>
      <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
        Evidence {kind === 'for' ? 'supporting' : 'against'}
      </p>
      <ul className="space-y-1">
        {items.map((e, i) => (
          <li key={i} className={`text-sm ${color}`}>
            <span className="mr-1.5">{marker}</span>
            {e.text}
            {e.source_ref ? (
              <span className="ml-1 text-xs text-slate-400">({e.source_ref})</span>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

function DevilsAdvocate({ critique }: { critique: Record<string, unknown> }) {
  // Anti-automation-bias: ALWAYS visible, never collapsed by default.
  const disconfirming = (critique.disconfirming_evidence as string[]) ?? [];
  const alternatives = (critique.alternative_explanations as string[]) ?? [];
  const baseRate = critique.base_rate_caveat as string | undefined;
  const summary = critique.summary as string | undefined;
  return (
    <div className="mt-3 rounded-md border-l-4 border-purple-500 bg-purple-50 p-3">
      <p className="mb-1 text-xs font-bold uppercase tracking-wide text-purple-800">
        Devil&apos;s advocate — counter-argument
      </p>
      {summary && <p className="text-sm text-purple-900">{summary}</p>}
      {disconfirming.length > 0 && (
        <ul className="mt-1 list-disc pl-5 text-sm text-purple-900">
          {disconfirming.map((d, i) => (
            <li key={i}>{d}</li>
          ))}
        </ul>
      )}
      {alternatives.length > 0 && (
        <p className="mt-1 text-sm text-purple-900">
          <span className="font-medium">Alternatives to consider: </span>
          {alternatives.join(', ')}
        </p>
      )}
      {baseRate && <p className="mt-1 text-xs italic text-purple-700">{baseRate}</p>}
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
    return (
      <div className="rounded-lg border-2 border-red-700 bg-red-50 p-4">
        <p className="mb-1 text-sm font-bold uppercase text-red-800">Hard block — cannot proceed</p>
        <p className="text-sm text-red-900">{s.body}</p>
        {decided ? (
          <p className="mt-2 text-xs text-slate-600">Recorded decision: {decided}</p>
        ) : (
          <div className="mt-3 space-y-2">
            <textarea
              value={overrideReason}
              onChange={(e) => setOverrideReason(e.target.value)}
              placeholder="Documented clinical reasoning is required to override a hard block."
              className="w-full rounded border border-red-300 p-2 text-sm"
              rows={2}
            />
            <div className="flex flex-col gap-2 sm:flex-row">
              <Button
                variant="danger"
                disabled={!overrideReason.trim()}
                onClick={() => record('overridden', overrideReason)}
              >
                Override with documented reason
              </Button>
              <Button variant="secondary" onClick={() => record('acknowledged')}>
                Acknowledge (do not override)
              </Button>
            </div>
          </div>
        )}
      </div>
    );
  }

  const tierBorder: Record<AutonomyTier, string> = {
    informational: 'border-blue-200',
    suggestive: 'border-green-200',
    flag_for_review: 'border-amber-400',
  };

  return (
    <div className={`rounded-lg border bg-white p-4 shadow-sm ${tierBorder[s.autonomy_tier]}`}>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <AutonomyBadge tier={s.autonomy_tier} />
        {s.cant_miss_flag && <CantMissBadge />}
        {s.confidence_band && <ProbabilityBandBadge band={s.confidence_band} />}
        <span className="text-xs text-slate-400">{s.output_type}</span>
      </div>

      {/* Engagement gate for flag-for-review: clinician must actively click through. */}
      {!acknowledged ? (
        <div className="rounded-md border border-dashed border-amber-400 bg-amber-50 p-4 text-center">
          <p className="text-sm text-amber-900">
            This output is flagged for review. Engage to see the evidence and assessment.
          </p>
          <Button
            className="mt-2"
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
          <div className="mt-3 border-t border-slate-100 pt-2">
            <p className="text-xs uppercase tracking-wide text-slate-400">
              Assessment to consider
            </p>
            <p className="font-semibold text-slate-900">{s.title}</p>
            {s.body && <p className="mt-1 text-sm text-slate-600">{s.body}</p>}
          </div>

          {hasDevil && <DevilsAdvocate critique={s.devils_advocate} />}

          {s.citations.length > 0 && (
            <div className="mt-3 space-y-2 rounded bg-slate-50 p-2">
              <p className="text-xs font-semibold text-slate-500">
                Guideline citations (grounding)
              </p>
              {s.citations.map((c, i) => (
                <div key={i} className="border-l-2 border-slate-300 pl-2">
                  <p className="text-xs font-medium text-slate-700">
                    <span className="uppercase">{c.source}</span> · {c.document_title}
                    {c.heading ? ` — ${c.heading}` : ''}{' '}
                    <span className="text-slate-400">[{c.section_id}]</span>
                  </p>
                  {c.snippet && <p className="text-xs italic text-slate-500">“{c.snippet}”</p>}
                </div>
              ))}
            </div>
          )}

          {decided && (
            <p className="mt-2 text-xs text-slate-500">Recorded decision: {decided}</p>
          )}
        </>
      )}
    </div>
  );
}
