'use client';

import { useState } from 'react';
import { api } from '@/lib/api';
import type { IntakeQuestion } from '@/lib/types';
import { Button, Card } from '@/components/ui';

const TYPE_STYLE: Record<string, string> = {
  red_flag: 'bg-red-100 text-red-800',
  relevant_negative: 'bg-blue-100 text-blue-800',
  clarifying: 'bg-slate-100 text-slate-700',
  history: 'bg-slate-100 text-slate-700',
  exam: 'bg-green-100 text-green-800',
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
    <Card>
      <h2 className="mb-1 text-lg font-semibold">Clarifying questions</h2>
      <p className="mb-3 text-sm text-slate-500">
        The triage agent asks the questions that most change the differential. Answer what you can —
        relevant negatives are valuable.
      </p>
      <div className="space-y-4">
        {questions.map((q) => (
          <div key={q.id}>
            <div className="mb-1 flex flex-wrap items-start gap-2">
              <span
                className={`flex-shrink-0 rounded px-1.5 py-0.5 text-xs font-medium ${
                  TYPE_STYLE[q.question_type] ?? 'bg-slate-100 text-slate-700'
                }`}
              >
                {q.question_type.replace('_', ' ')}
              </span>
              <span className="text-sm font-medium text-slate-800">{q.question_text}</span>
            </div>
            {q.rationale && <p className="mb-1 text-xs italic text-slate-400">{q.rationale}</p>}
            <input
              value={answers[q.id] ?? ''}
              onChange={(e) => setAnswers((a) => ({ ...a, [q.id]: e.target.value }))}
              placeholder="Your answer (e.g. no / yes, 3 days / 38.5°C)"
              className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            />
          </div>
        ))}
      </div>
      <div className="mt-4 flex flex-col gap-2 sm:flex-row">
        <Button onClick={() => void submit()} disabled={submitting}>
          {submitting ? 'Submitting…' : 'Submit answers'}
        </Button>
        <Button variant="secondary" onClick={onComplete} disabled={submitting}>
          Skip — proceed to reasoning
        </Button>
      </div>
    </Card>
  );
}
