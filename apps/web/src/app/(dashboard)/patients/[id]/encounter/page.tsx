'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { api } from '@/lib/api';
import { requestErrorMessage } from '@/lib/errors';
import type { ClinicalSuggestion, IntakeQuestion } from '@/lib/types';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { IntakeFlow } from '@/components/reasoning/IntakeFlow';
import { ReasoningTheatre } from '@/components/reasoning/ReasoningTheatre';
import { SuggestionCard } from '@/components/reasoning/SuggestionCard';
import { Button, Card, ErrorBanner, LoadingBlock, SkeletonCards } from '@aether/ui';

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
            <li
              key={p.key}
              aria-current={isCurrent ? 'step' : undefined}
              className={`flex items-center ${i < PHASES.length - 1 ? 'flex-1' : ''}`}
            >
              <div className="flex flex-shrink-0 items-center gap-1.5">
                <span
                  className={`flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-full text-xs font-semibold transition-colors sm:h-7 sm:w-7 ${
                    isComplete
                      ? 'bg-brand-600 text-white'
                      : isCurrent
                        ? 'bg-brand-100 text-brand-700 ring-2 ring-brand-600'
                        : 'bg-slate-100 text-slate-400'
                  }`}
                >
                  {isComplete ? (
                    <svg
                      aria-hidden="true"
                      className="h-3.5 w-3.5"
                      fill="none"
                      viewBox="0 0 24 24"
                      strokeWidth={3}
                      stroke="currentColor"
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        d="M4.5 12.75l6 6 9-13.5"
                      />
                    </svg>
                  ) : (
                    i + 1
                  )}
                </span>
                {/* On the smallest screens only the active step's label shows, to avoid overflow. */}
                <span
                  className={`whitespace-nowrap text-[11px] font-medium sm:text-xs ${isCurrent ? 'inline' : 'hidden sm:inline'} ${
                    isCurrent ? 'text-brand-700' : isComplete ? 'text-slate-700' : 'text-slate-400'
                  }`}
                >
                  {p.label}
                </span>
              </div>
              {i < PHASES.length - 1 && (
                <div
                  className={`mx-2 h-px flex-1 ${isComplete ? 'bg-brand-500' : 'bg-slate-200'}`}
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
  const [starting, setStarting] = useState(false);
  const [resultsLoading, setResultsLoading] = useState(false);

  async function startReasoning() {
    setError(null);
    setStarting(true);
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
      setError(requestErrorMessage(e, 'the reasoning run'));
    } finally {
      setStarting(false);
    }
  }

  // Takes the session id as an argument rather than closing over the nullable state:
  // the only call site sits inside a `sessionId &&` guard, so the id is already narrowed.
  async function loadResults(sessionId: string) {
    setError(null);
    setResultsLoading(true);
    try {
      setSuggestions(await api.listSuggestions(sessionId));
      setPhase('results');
    } catch (e) {
      // Without this the rejection was unhandled and the screen simply stayed on the
      // reasoning phase after the run had finished — the eight agents had already done their
      // work and their output was sitting on the server, with nothing on screen saying so or
      // offering to fetch it again. Staying in 'reasoning' is still right (there is nothing
      // to show), but now it says why, and the retry re-reads rather than re-running.
      setError(requestErrorMessage(e, 'the reasoning results'));
    } finally {
      setResultsLoading(false);
    }
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
          aria-hidden="true"
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

      {/* The retry belongs to whichever step failed. On 'complaint' that is starting the run;
          on 'reasoning' it is re-reading results the agents have already produced, which is
          why it says so — a clinician who has just watched eight agents deliberate needs to
          know a retry here does not put them through it a second time. */}
      {error && phase === 'complaint' && (
        <ErrorBanner message={error} onRetry={() => void startReasoning()} retrying={starting} />
      )}
      {error && phase === 'reasoning' && sessionId && (
        <ErrorBanner
          message={error}
          onRetry={() => void loadResults(sessionId)}
          retrying={resultsLoading}
          retryLabel="Fetch results again"
        />
      )}
      {error && phase !== 'complaint' && phase !== 'reasoning' && <ErrorBanner message={error} />}

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
          <Button
            className="mt-4 w-full sm:w-auto"
            onClick={() => void startReasoning()}
            disabled={complaint.trim().length < 3}
          >
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
        <ErrorBoundary section="The reasoning theatre">
          <div className="animate-slide-up">
            <ReasoningTheatre
              sessionId={sessionId}
              onComplete={() => void loadResults(sessionId)}
            />
            {/* The gap between the theatre reading "Complete" and the cards appearing. The
                agents have finished and their output is being read back from the server, and
                until this there was nothing on screen that said so — the run simply looked
                finished with no results, which is indistinguishable from a run that produced
                none. */}
            {resultsLoading && (
              <LoadingBlock label="Fetching the reasoning results" className="mt-4 block space-y-3">
                <SkeletonCards count={2} className="space-y-3" />
              </LoadingBlock>
            )}
            <p className="mt-3 text-center text-xs text-slate-400">
              Evidence and dissent are shown before conclusions to counter automation bias.
            </p>
          </div>
        </ErrorBoundary>
      )}

      {/* The boundary that matters most on this screen. These cards render agent output, so
          their shape is the least predictable thing in the app — and the thing a crash would
          hide is a can't-miss flag or a hard block. Silently rendering fewer cards than the
          Verifier passed is the automation-bias failure CLAUDE.md rule #5 exists to prevent,
          only arrived at by accident, so the fallback says outright that something is missing
          rather than letting a short list pass for a complete one. */}
      {phase === 'results' && sessionId && (
        <ErrorBoundary section="The reasoning results" onReset={() => void loadResults(sessionId)}>
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
        </ErrorBoundary>
      )}
    </div>
  );
}

/**
 * Where a suggestion sorts on the results list. Lower is nearer the top.
 *
 * Severity first, then category — the same judgement the Safety screen's ordering makes, and for
 * the same reason. Below the two unmissable tiers this used to fall straight through to
 * `output_type`, which made position a statement about what *kind* of output something is rather
 * than about how much it matters, and the two come apart in exactly one place.
 *
 * A management option that conflicts with the patient's own record is escalated by the synthesis
 * agent to flag-for-review and retitled to say so — that module calls it "the most dangerous
 * thing this pipeline can emit, precisely because everything about its presentation says it was
 * checked". A moderate interaction or a cross-reactive allergy makes it conflicted without making
 * it a hard block, so it cleared neither of the first two tests and landed in the `management`
 * bucket: below every differential, below the investigations card, at the bottom of the list.
 * The one card on the screen the clinician is being asked to actively engage with was the one
 * they had to scroll for.
 *
 * So flag-for-review sorts above the categories. Read off `autonomy_tier` rather than re-derived
 * from the conflict, because the tier is the field the Verifier owns and the conservative
 * classification has already won by the time it is set (Critical Safety Rule #2); anything else
 * escalated for any other reason gets lifted by the same rule.
 *
 * The sort is stable, so within each band the server's own ordering survives — this only ever
 * lifts a more severe suggestion past a less severe one. An `output_type` this build does not
 * recognise sorts last rather than being dropped, for the reason the Safety screen renders an
 * unknown check type from its raw name: the web app and the API deploy separately.
 */
function rank(s: ClinicalSuggestion): number {
  if (s.is_hard_block) return 0;
  if (s.cant_miss_flag) return 1;
  if (s.autonomy_tier === 'flag_for_review') return 2;
  if (s.output_type === 'differential') return 3;
  if (s.output_type === 'investigation') return 4;
  if (s.output_type === 'management') return 5;
  return 6;
}
