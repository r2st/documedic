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

const PHASES: { key: Phase; label: string }[] = [
  { key: 'complaint', label: 'Complaint' },
  { key: 'intake', label: 'Intake' },
  { key: 'reasoning', label: 'Reasoning' },
  { key: 'results', label: 'Results' },
];

function StepIndicator({ currentPhase }: { currentPhase: Phase }) {
  const currentIndex = PHASES.findIndex((p) => p.key === currentPhase);
  return (
    <nav aria-label="Progress" className="mb-2">
      <ol className="flex items-center gap-0">
        {PHASES.map((p, i) => {
          const isComplete = i < currentIndex;
          const isCurrent = i === currentIndex;
          return (
            <li key={p.key} className="flex items-center">
              <div className="flex items-center gap-1.5">
                <span
                  className={`flex h-7 w-7 items-center justify-center rounded-full text-xs font-semibold transition-colors ${
                    isComplete
                      ? 'bg-brand-600 text-white'
                      : isCurrent
                        ? 'bg-brand-100 text-brand-700 ring-2 ring-brand-600'
                        : 'bg-slate-100 text-slate-400'
                  }`}
                >
                  {isComplete ? (
                    <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" strokeWidth={3} stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                    </svg>
                  ) : (
                    i + 1
                  )}
                </span>
                <span
                  className={`text-xs font-medium ${
                    isCurrent ? 'text-brand-700' : isComplete ? 'text-slate-700' : 'text-slate-400'
                  }`}
                >
                  {p.label}
                </span>
              </div>
              {i < PHASES.length - 1 && (
                <div
                  className={`mx-2 h-px w-8 sm:w-12 ${
                    isComplete ? 'bg-brand-500' : 'bg-slate-200'
                  }`}
                />
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

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
    <div className="space-y-5">
      <Link
        href={`/patients/${id}`}
        className="group mb-2 inline-flex items-center gap-1 text-sm font-medium text-brand-600 hover:text-brand-700"
      >
        <svg
          className="h-4 w-4 transition-transform group-hover:-translate-x-0.5"
          fill="none"
          viewBox="0 0 24 24"
          strokeWidth={2}
          stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5L8.25 12l7.5-7.5" />
        </svg>
        Back to patient
      </Link>

      <h1 className="text-2xl font-bold tracking-tight text-slate-900">Diagnostic reasoning</h1>

      <StepIndicator currentPhase={phase} />

      {error && (
        <div className="flex items-start gap-2 rounded-lg bg-red-50 p-3 text-sm text-red-700 ring-1 ring-red-200">
          <svg className="mt-0.5 h-4 w-4 flex-shrink-0" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
          </svg>
          <span>{error}</span>
        </div>
      )}

      {phase === 'complaint' && (
        <Card className="animate-fade-in">
          <div className="mb-4">
            <label htmlFor="complaint" className="block text-sm font-medium text-slate-700">
              Presenting complaint
            </label>
            <p className="mt-0.5 text-xs text-slate-500">
              Describe the patient&apos;s chief complaint and relevant context.
            </p>
          </div>
          <textarea
            id="complaint"
            value={complaint}
            onChange={(e) => setComplaint(e.target.value)}
            rows={4}
            placeholder="e.g. 54-year-old with central chest pain radiating to the left arm for 1 hour"
            className="w-full"
          />
          <Button className="mt-4" onClick={() => void startReasoning()} disabled={complaint.trim().length < 3}>
            Begin intake
          </Button>
        </Card>
      )}

      {phase === 'intake' && sessionId && (
        <div className="animate-slide-up">
          <IntakeFlow
            sessionId={sessionId}
            questions={questions}
            onComplete={() => setPhase('reasoning')}
          />
        </div>
      )}

      {phase === 'reasoning' && sessionId && (
        <div className="animate-slide-up">
          <ReasoningTheatre sessionId={sessionId} onComplete={() => void loadResults()} />
          <p className="mt-3 text-center text-xs text-slate-400">
            Evidence and dissent are shown before conclusions to counter automation bias.
          </p>
        </div>
      )}

      {phase === 'results' && sessionId && (
        <div className="animate-fade-in space-y-3">
          <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
            <h2 className="text-lg font-semibold text-slate-900">Results</h2>
            <span className="rounded-full bg-amber-50 px-3 py-1 text-xs font-medium text-amber-700 ring-1 ring-inset ring-amber-200">
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
