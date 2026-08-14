'use client';

import { useState } from 'react';
import { api } from '@/lib/api';
import type { IntakeQuestion } from '@/lib/types';
import { Button, Card } from '@/components/ui';

const TYPE_STYLE: Record<string, string> = {
  red_flag: 'bg-red-50 text-red-700 ring-1 ring-red-200',
  relevant_negative: 'bg-blue-50 text-blue-700 ring-1 ring-blue-200',
  clarifying: 'bg-slate-50 text-slate-600 ring-1 ring-slate-200',
  history: 'bg-slate-50 text-slate-600 ring-1 ring-slate-200',
  exam: 'bg-emerald-50 text-emerald-700 ring-1 ring-emerald-200',
};

/**
 * Adaptive intake interview. Renders the Triage agent's highest-information-gain questions and
 * loops (submit → next batch) until intake is complete, then invokes onComplete.
 */
export function IntakeFlow({
  sessionId,
  questions: initial,
  onComplete,
}: {
  sessionId: string;
  questions: IntakeQuestion[];
  onComplete: () => void;
}) {
  const [questions, setQuestions] = useState<IntakeQuestion[]>(initial);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);

  async function submit() {
    setSubmitting(true);
    try {
      const payload = questions
        .filter((q) => (answers[q.id] ?? '').trim())
        .map((q) => ({ question_id: q.id, answer_text: answers[q.id] }));
      const state = await api.submitIntakeAnswers(sessionId, payload);
      if (state.intake_complete) {
        onComplete();
      } else {
        setQuestions(state.pending_questions);
        setAnswers({});
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card className="animate-slide-up">
      <div className="mb-4 flex items-center gap-2">
        <svg
          aria-hidden="true"
          className="h-5 w-5 text-brand-600"
          fill="none"
          viewBox="0 0 24 24"
          strokeWidth={1.5}
          stroke="currentColor"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M9.879 7.519c1.171-1.025 3.071-1.025 4.242 0 1.172 1.025 1.172 2.687 0 3.712-.203.179-.43.326-.67.442-.745.361-1.45.999-1.45 1.827v.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9 5.25h.008v.008H12v-.008Z"
          />
        </svg>
        <h2 className="text-lg font-semibold text-slate-900">Clarifying questions</h2>
      </div>
      <p className="mb-5 text-sm text-slate-500">
        The triage agent asks the questions that most change the differential. Answer what you can —
        relevant negatives are valuable.
      </p>
      <div className="space-y-5" role="group" aria-label="Clarifying questions">
        {questions.map((q, index) => (
          <div key={q.id} className="rounded-xl border border-slate-200 bg-slate-50/50 p-4">
            <div className="mb-2 flex flex-wrap items-start gap-2">
              {/* Decorative ordinal: the questions are already a numbered visual list. */}
              <span
                aria-hidden="true"
                className="flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-full bg-brand-100 text-xs font-bold text-brand-700"
              >
                {index + 1}
              </span>
              <span
                className={`flex-shrink-0 rounded-md px-2 py-0.5 text-xs font-medium ${
                  TYPE_STYLE[q.question_type] ?? 'bg-slate-50 text-slate-600 ring-1 ring-slate-200'
                }`}
              >
                {q.question_type.replace(/_/g, ' ')}
              </span>
            </div>
            {/* The question is the field's label and the rationale is its description, so
                they are wired up as such — the placeholder is an example, not a label, and
                disappears the moment the clinician starts typing. */}
            <p id={`intake-q-${q.id}`} className="mb-2 text-sm font-medium text-slate-800">
              {q.question_text}
            </p>
            {q.rationale && (
              <p id={`intake-why-${q.id}`} className="mb-2 text-xs italic text-slate-400">
                {q.rationale}
              </p>
            )}
            <input
              id={`intake-answer-${q.id}`}
              aria-labelledby={`intake-q-${q.id}`}
              aria-describedby={q.rationale ? `intake-why-${q.id}` : undefined}
              value={answers[q.id] ?? ''}
              onChange={(e) => setAnswers((a) => ({ ...a, [q.id]: e.target.value }))}
              placeholder="Your answer (e.g. no / yes, 3 days / 38.5°C)"
              className="w-full"
            />
          </div>
        ))}
      </div>
      <div className="mt-5 flex flex-col gap-2 border-t border-slate-100 pt-4 sm:flex-row">
        <Button
          className="w-full sm:w-auto"
          onClick={() => void submit()}
          disabled={submitting}
          aria-busy={submitting}
        >
          {submitting ? 'Submitting…' : 'Submit answers'}
        </Button>
        <Button
          className="w-full sm:w-auto"
          variant="ghost"
          onClick={onComplete}
          disabled={submitting}
        >
          Skip — proceed to reasoning
        </Button>
      </div>
    </Card>
  );
}
