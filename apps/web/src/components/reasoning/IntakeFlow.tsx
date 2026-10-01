'use client';

import { useState } from 'react';
import { api } from '@/lib/api';
import { requestErrorMessage } from '@/lib/errors';
import type { IntakeQuestion } from '@/lib/types';
import { Button, Card, ErrorBanner } from '@aether/ui';

const TYPE_STYLE: Record<string, string> = {
  red_flag: 'bg-red-950/30 text-red-400 ring-1 ring-red-800/50',
  relevant_negative: 'bg-blue-950/30 text-blue-400 ring-1 ring-blue-800/50',
  clarifying: 'bg-[#1A1A1D] text-[#9CA3AF] ring-1 ring-[#2A2A2D]',
  history: 'bg-[#1A1A1D] text-[#9CA3AF] ring-1 ring-[#2A2A2D]',
  exam: 'bg-emerald-950/30 text-emerald-400 ring-1 ring-emerald-800/50',
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
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    // Cleared before the retry: a failed submit says the answers "may not have completed", and
    // leaving that banner up while the retry is in flight describes the previous attempt as
    // though it were the current one.
    setError(null);
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
    } catch (err) {
      // Without this the rejection was unhandled: the button went back from "Submitting…" to
      // "Submit answers" and nothing else on the screen changed. The clinician had just typed
      // the history the whole differential turns on, and the only signal that none of it
      // reached the triage agent was a button that stopped spinning.
      //
      // The typed answers are deliberately left in place — `setAnswers({})` only runs on the
      // success path — so the retry re-sends them rather than asking for them a second time.
      setError(requestErrorMessage(err, 'these intake answers'));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card className="animate-slide-up">
      <div className="mb-4 flex items-center gap-2">
        <svg
          aria-hidden="true"
          className="h-5 w-5 text-[#F0B429]"
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
        <h2 className="text-lg font-semibold text-[#E5E7EB]">Clarifying questions</h2>
      </div>
      <p className="mb-5 text-sm text-[#9CA3AF]">
        Answer what you can — relevant negatives are valuable.
      </p>
      <div className="space-y-5" role="group" aria-label="Clarifying questions">
        {questions.map((q, index) => (
          <div key={q.id} className="rounded-xl border border-[#2A2A2D] bg-[#111113] p-4">
            <div className="mb-2 flex flex-wrap items-start gap-2">
              {/* Decorative ordinal: the questions are already a numbered visual list. */}
              <span
                aria-hidden="true"
                className="flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-full bg-[#F0B429]/15 text-xs font-bold text-[#F0B429]"
              >
                {index + 1}
              </span>
              <span
                className={`flex-shrink-0 rounded-md px-2 py-0.5 text-xs font-medium ${
                  TYPE_STYLE[q.question_type] ?? 'bg-[#1A1A1D] text-[#9CA3AF] ring-1 ring-[#2A2A2D]'
                }`}
              >
                {q.question_type.replace(/_/g, ' ')}
              </span>
            </div>
            {/* The question is the field's label and the rationale is its description, so
                they are wired up as such — the placeholder is an example, not a label, and
                disappears the moment the clinician starts typing. */}
            <p id={`intake-q-${q.id}`} className="mb-2 text-sm font-medium text-[#E5E7EB]">
              {q.question_text}
            </p>
            {q.rationale && (
              <p id={`intake-why-${q.id}`} className="mb-2 text-xs italic text-[#6B7280]">
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
      {/* Retryable: the answers are still in the fields, so this re-sends the same batch
          rather than making the clinician retype an interview. */}
      {error && (
        <ErrorBanner
          className="mt-5"
          message={error}
          onRetry={() => void submit()}
          retrying={submitting}
          retryLabel="Send answers again"
        />
      )}

      <div className="mt-5 flex flex-col gap-2 border-t border-[#2A2A2D] pt-4 sm:flex-row">
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
