'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { api } from '@/lib/api';
import type { ClinicalSuggestion, IntakeQuestion } from '@/lib/types';
import { Button, Card } from '@/components/ui';
import { IntakeFlow } from '@/components/reasoning/IntakeFlow';
import { ReasoningTheatre } from '@/components/reasoning/ReasoningTheatre';
import { SuggestionCard } from '@/components/reasoning/SuggestionCard';

type Phase = 'complaint' | 'intake' | 'reasoning' | 'results';

export default function EncounterPage() {
  const { id } = useParams<{ id: string }>();
  const [phase, setPhase] = useState<Phase>('complaint');
  const [complaint, setComplaint] = useState('');
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [questions, setQuestions] = useState<IntakeQuestion[]>([]);
  const [suggestions, setSuggestions] = useState<ClinicalSuggestion[]>([]);
  const [error, setError] = useState<string | null>(null);

  async function startReasoning() {
    setError(null);
    try {
      const state = await api.startReasoning(id, complaint.trim());
      setSessionId(state.session.id);
      if (state.intake_complete || state.pending_questions.length === 0) {
        setPhase('reasoning');
      } else {
        setQuestions(state.pending_questions);
        setPhase('intake');
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to start');
    }
  }

  async function loadResults() {
    if (!sessionId) return;
    setSuggestions(await api.listSuggestions(sessionId));
    setPhase('results');
  }

  // Group results so can't-miss and hard blocks surface first.
  const ordered = [...suggestions].sort((a, b) => rank(a) - rank(b));

  return (
    <div className="space-y-4">
      <Link href={`/patients/${id}`} className="text-sm text-blue-600 hover:underline">
        ← Back to patient
      </Link>
      <h1 className="text-xl font-bold">Diagnostic reasoning</h1>

      {error && <p className="rounded bg-red-50 p-2 text-sm text-red-700">{error}</p>}

      {phase === 'complaint' && (
        <Card>
          <label className="mb-1 block text-sm font-medium">Presenting complaint</label>
          <textarea
            value={complaint}
            onChange={(e) => setComplaint(e.target.value)}
            rows={3}
            placeholder="e.g. 54-year-old with central chest pain radiating to the left arm for 1 hour"
            className="w-full rounded-md border border-slate-300 p-2 text-sm"
          />
          <Button className="mt-3" onClick={() => void startReasoning()} disabled={complaint.trim().length < 3}>
            Begin intake
          </Button>
        </Card>
      )}

      {phase === 'intake' && sessionId && (
        <IntakeFlow
          sessionId={sessionId}
          questions={questions}
          onComplete={() => setPhase('reasoning')}
        />
      )}

      {phase === 'reasoning' && sessionId && (
        <>
          <ReasoningTheatre sessionId={sessionId} onComplete={() => void loadResults()} />
          <p className="text-center text-xs text-slate-400">
            Evidence and dissent are shown before conclusions to counter automation bias.
          </p>
        </>
      )}

      {phase === 'results' && sessionId && (
        <div className="space-y-3">
          <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
            <h2 className="text-lg font-semibold">Results</h2>
            <span className="text-xs text-slate-400">
              The clinician is the decision-maker. These are decision-support outputs only.
            </span>
          </div>
          {ordered.map((s) => (
            <SuggestionCard key={s.id} suggestion={s} sessionId={sessionId} />
          ))}
        </div>
      )}
    </div>
  );
}

function rank(s: ClinicalSuggestion): number {
  if (s.is_hard_block) return 0;
  if (s.cant_miss_flag) return 1;
  if (s.output_type === 'differential') return 2;
  if (s.output_type === 'investigation') return 3;
  if (s.output_type === 'management') return 4;
  return 5;
}
