'use client';

import { useEffect, useState } from 'react';
import { useReasoningStream } from '@/hooks/useReasoningStream';
import type { AgentLane } from '@/hooks/useReasoningStream';
import { AutonomyBadge } from './badges';
import type { AutonomyTier } from '@/lib/types';
import { Card, ErrorBanner } from '@aether/ui';

// Keyed by the lane status union rather than by `string`, so both maps are total by
// construction: a lane's status is only ever assigned by the stream reducer (never taken from
// the server's payload), so there is no unknown-status case to fall back to at runtime. Typing
// them this way makes adding a fourth status a compile error here instead of a lane that
// silently renders with no dot and reads out a raw enum value to a screen reader.
const DOT: Record<AgentLane['status'], string> = {
  idle: 'bg-[#6B7280] ring-2 ring-[#2A2A2D]',
  running: 'bg-[#F0B429] ring-2 ring-[#F0B429]/20 animate-pulse',
  done: 'bg-emerald-500 ring-2 ring-emerald-500/20',
  failed: 'bg-amber-500 ring-2 ring-amber-500/20',
};

// A lane's progress is drawn as a coloured dot and, when finished, a tick. Both are colour and
// shape only, so the same fact is spelled out for a screen reader alongside the lane's name.
const LANE_STATUS: Record<AgentLane['status'], string> = {
  idle: 'waiting',
  running: 'running',
  done: 'done',
  // Spelled out rather than "failed": what matters to the clinician reading the case is not
  // that something errored but that this lane contributed nothing to what they are looking at.
  failed: 'did not complete — contributed nothing to this case',
};

/**
 * Signature screen: live agent activity during reasoning. Streams SSE events and shows each
 * agent lane activating, hypotheses appearing, can't-miss flags pulsing, the devil's-advocate
 * critique in a distinct section, and the verifier verdict last.
 */
export function ReasoningTheatre({
  sessionId,
  onComplete,
}: {
  sessionId: string;
  onComplete: () => void;
}) {
  const { state, start, stop } = useReasoningStream();
  // Set while a manual reconnect is being made, so the retry button can say it is working.
  // `state.running` is not enough on its own: it is also true for the whole of a healthy run.
  const [reconnecting, setReconnecting] = useState(false);

  useEffect(() => {
    void start(sessionId, onComplete);
    return () => stop();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  /**
   * Reopen the stream after it failed.
   *
   * A failed stream used to be a dead end: the banner said what had gone wrong and the only way
   * on was to leave the encounter and start the whole reasoning run again. `start()` mints a
   * fresh stream token and resubscribes to the same session, so a run that is still going on
   * the server is picked back up rather than re-run.
   */
  async function reconnect() {
    setReconnecting(true);
    try {
      await start(sessionId, onComplete);
    } finally {
      setReconnecting(false);
    }
  }

  const ev = state.events;
  const cantMiss = (ev.cant_miss?.items as Array<{ diagnosis_name: string; why: string }>) ?? [];
  const devil = ev.devils_advocate?.critique as Record<string, unknown> | undefined;
  const verifier = ev.verifier as
    { status?: string; autonomy_tier?: AutonomyTier; case_caveats?: string[] } | undefined;
  const hypotheses =
    (ev.hypotheses?.hypotheses as Array<{
      diagnosis_name: string;
      probability_band: string;
      cant_miss_flag?: boolean;
    }>) ?? [];

  // Whether the Verifier gate reported on this run. Critical Safety Rule #1: no clinical output
  // reaches the clinician without passing through it, and the panels below — a ranked
  // differential, the can't-miss list, the devil's-advocate critique — are clinical output.
  //
  // During a healthy run they stream in before the verdict does, which is the whole point of the
  // theatre and is fine: the run is still going and the gate is still ahead of it. What was not
  // fine is what happened when the run *stopped* there. `state.error` cleared `running` and put a
  // banner at the top of the first card, and every panel below it stayed exactly as it was —
  // agents 2, 3 and 4's output, rendered identically to a completed run's, with agent 7 never
  // having run. A clinician scrolling past a banner they have already read finds a differential
  // that looks finished.
  //
  // The can't-miss block is the worst of them, because it is the one whose *absence* carries
  // meaning: a partial sentinel list reads as "these are the dangerous things, and nothing else",
  // when the sentinel may have been cut off mid-scan. And this is reachable by an ordinary
  // outage — an upstream provider going away mid-run is what `describe_exception` on that path
  // exists for.
  //
  // So an errored run with no verdict withholds the conclusions and says why. An errored run
  // that *did* get a verdict keeps them: that output passed the gate, and the banner already
  // says the run did not finish.
  const verified = Boolean(verifier);
  const withheld = Boolean(state.error) && !verified;

  // Whether the engine produced this case without the LLM. `reasoning_complete` has carried
  // `degraded` since the graph was written and nothing on this screen read it, so the one state
  // Critical Safety Rule #8 names an indicator for — "AI reasoning paused" — was the one state
  // the theatre rendered identically to a healthy run.
  //
  // The clinician was not told *nothing*: the Verifier's floor adds a caveat line and escalates
  // the tier. But that lands as one sentence inside the verdict card, below the differential,
  // among unrelated floor reasons ("A can't-miss diagnosis is on the differential"), and it is
  // the same card a full-strength run shows. The fact that changes how every panel above it
  // should be read cannot be a footnote underneath them — that is the anti-automation-bias
  // argument of Rule #6 applied to the run's own provenance.
  //
  // So it goes first, above the output it qualifies, and it is neither collapsible nor dismissible.
  const degraded = Boolean(ev.reasoning_complete?.degraded);

  const statusLabel = state.error
    ? 'Error'
    : state.done
      ? 'Complete'
      : state.running
        ? 'Reasoning…'
        : 'Connecting…';

  const statusColor = state.error
    ? 'text-red-600'
    : state.done
      ? 'text-emerald-600'
      : 'text-[#F0B429]';

  return (
    <div className="space-y-4 animate-fade-in">
      <Card>
        <div className="mb-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
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
                d="M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09ZM18.259 8.715 18 9.75l-.259-1.035a3.375 3.375 0 0 0-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 0 0 2.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 0 0 2.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 0 0-2.455 2.456ZM16.894 20.567 16.5 21.75l-.394-1.183a2.25 2.25 0 0 0-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 0 0 1.423-1.423l.394-1.183.394 1.183a2.25 2.25 0 0 0 1.423 1.423l1.183.394-1.183.394a2.25 2.25 0 0 0-1.423 1.423Z"
              />
            </svg>
            <h2 className="text-lg font-semibold text-[#E5E7EB]">Reasoning Theatre</h2>
          </div>
          {/* The run reports its own progress with nothing else on the page changing and focus
              still wherever the clinician left it, so this pill is a live region. */}
          <span
            role="status"
            className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-semibold ${statusColor} ${
              state.running
                ? 'bg-[#F0B429]/10 ring-1 ring-[#F0B429]/30'
                : state.done
                  ? 'bg-emerald-950/30 ring-1 ring-emerald-800/50'
                  : state.error
                    ? 'bg-red-950/30 ring-1 ring-red-800/50'
                    : 'bg-[#1A1A1D] ring-1 ring-[#2A2A2D]'
            }`}
          >
            {state.running && (
              <span
                aria-hidden="true"
                className="h-1.5 w-1.5 rounded-full bg-[#F0B429] animate-pulse"
              />
            )}
            {statusLabel}
          </span>
        </div>
        {state.error && (
          <ErrorBanner
            className="mb-3"
            message={state.error}
            onRetry={() => void reconnect()}
            retrying={reconnecting}
            retryLabel="Reconnect"
          />
        )}
        <ol className="space-y-2">
          {state.lanes.map((lane) => (
            <li
              key={lane.agent}
              className="flex items-center gap-3 rounded-lg px-3 py-2 text-sm hover:bg-[#111113] transition-colors"
            >
              <span
                aria-hidden="true"
                className={`inline-block h-3 w-3 rounded-full ${DOT[lane.status]}`}
              />
              <span
                className={lane.status === 'done' ? 'text-[#9CA3AF]' : 'font-medium text-[#E5E7EB]'}
              >
                {lane.label}
              </span>
              <span className="sr-only">{LANE_STATUS[lane.status]}</span>
              {lane.status === 'failed' && (
                <span
                  className="rounded-full bg-amber-950/30 px-2 py-0.5 text-xs font-semibold text-amber-400"
                  aria-hidden="true"
                >
                  did not complete
                </span>
              )}
              {lane.status === 'done' && (
                <svg
                  aria-hidden="true"
                  className="h-4 w-4 text-emerald-500"
                  fill="none"
                  viewBox="0 0 24 24"
                  strokeWidth={2}
                  stroke="currentColor"
                >
                  <path strokeLinecap="round" strokeLinejoin="round" d="m4.5 12.75 6 6 9-13.5" />
                </svg>
              )}
            </li>
          ))}
          {state.lanes.length === 0 && (
            <li className="flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-[#6B7280]">
              <span
                aria-hidden="true"
                className="inline-block h-3 w-3 animate-pulse rounded-full bg-[#2A2A2D]"
              />
              Initializing…
            </li>
          )}
        </ol>
      </Card>

      {/* Everything below arrives from the SSE stream while focus is still wherever the
          clinician left it. Nothing scrolls it into view and nothing takes focus, so it is a
          polite live region — the can't-miss block inside it is assertive on its own. */}
      <div aria-live="polite" className="space-y-4">
        {degraded && (
          // Above the output, because it changes how all of it should be read. `role="status"`
          // rather than `alert`: the run has finished and nothing is being interrupted, and the
          // enclosing region is already polite.
          <div
            role="status"
            className="rounded-xl border-2 border-amber-500/50 bg-amber-950/30 p-5 text-sm text-amber-300"
          >
            <div className="mb-1.5 flex items-center gap-2">
              <svg
                aria-hidden="true"
                className="h-5 w-5 text-amber-400"
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
              <p className="font-bold uppercase tracking-wide">AI reasoning paused</p>
            </div>
            <p>
              Deterministic rules only — no digital robot panel ran. Treat as a checklist, not a differential.
            </p>
            <p className="mt-1.5">
              Drug-safety checks are unaffected.
            </p>
          </div>
        )}

        {withheld && (
          <div
            role="status"
            className="rounded-xl border-2 border-[#2A2A2D] bg-[#1A1A1D] p-5 text-sm text-[#E5E7EB]"
          >
            <p className="font-semibold text-[#E5E7EB]">
              Unverified — output withheld.
            </p>
            <p className="mt-1.5">
              This run stopped before verification. Reconnect or start a new run.
              Drug-safety checks are unaffected.
            </p>
          </div>
        )}

        {!withheld && hypotheses.length > 0 && (
          <Card className="animate-slide-up">
            <div className="mb-3 flex items-center gap-2">
              <svg
                aria-hidden="true"
                className="h-5 w-5 text-[#9CA3AF]"
                fill="none"
                viewBox="0 0 24 24"
                strokeWidth={1.5}
                stroke="currentColor"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M3.75 12h16.5m-16.5 3.75h16.5M3.75 19.5h16.5M5.625 4.5h12.75a1.875 1.875 0 0 1 0 3.75H5.625a1.875 1.875 0 0 1 0-3.75Z"
                />
              </svg>
              <p className="text-sm font-semibold text-[#E5E7EB]">Hypotheses (live ranking)</p>
            </div>
            <ul className="space-y-1.5">
              {hypotheses.map((h, i) => (
                <li
                  key={i}
                  className="flex items-center justify-between rounded-lg bg-[#111113] px-3 py-2 text-sm"
                >
                  <span className="text-[#E5E7EB]">{h.diagnosis_name}</span>
                  <span className="rounded-full bg-[#2A2A2D] px-2 py-0.5 text-xs font-medium uppercase text-[#9CA3AF]">
                    {h.probability_band}
                  </span>
                </li>
              ))}
            </ul>
          </Card>
        )}

        {!withheld && cantMiss.length > 0 && (
          // A can't-miss diagnosis being forced onto the differential is the one event on this
          // screen that must interrupt rather than queue behind whatever else is being read.
          <div
            role="alert"
            className="animate-slide-up rounded-xl border-2 border-orange-500/50 bg-orange-950/30 p-5"
          >
            <div className="mb-2 flex items-center gap-2">
              <span
                aria-hidden="true"
                className="flex h-6 w-6 items-center justify-center rounded-full bg-orange-500 text-xs font-bold text-white"
              >
                !
              </span>
              <p className="text-sm font-bold uppercase tracking-wide text-orange-400">
                Can&apos;t-miss conditions forced onto the differential
              </p>
            </div>
            <ul className="space-y-1.5 pl-8">
              {cantMiss.map((c, i) => (
                <li key={i} className="text-sm text-orange-300">
                  <span className="font-semibold">{String(c.diagnosis_name ?? '')}</span> —{' '}
                  {String(c.why ?? '')}
                </li>
              ))}
            </ul>
          </div>
        )}

        {!withheld && devil && (
          <div className="animate-slide-up rounded-xl border-l-4 border-purple-500 bg-purple-950/30 p-5">
            <div className="mb-2 flex items-center gap-2">
              <svg
                aria-hidden="true"
                className="h-5 w-5 text-purple-400"
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
              <p className="text-sm font-bold uppercase tracking-wide text-purple-400">
                Devil&apos;s advocate
              </p>
            </div>
            <p className="text-sm text-purple-300">{String(devil.summary ?? '')}</p>
          </div>
        )}

        {verifier && (
          <Card className="animate-slide-up border-l-4 border-red-500">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <svg
                  aria-hidden="true"
                  className="h-5 w-5 text-red-600"
                  fill="none"
                  viewBox="0 0 24 24"
                  strokeWidth={1.5}
                  stroke="currentColor"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M9 12.75 11.25 15 15 9.75m-3-7.036A11.959 11.959 0 0 1 3.598 6 11.99 11.99 0 0 0 3 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285Z"
                  />
                </svg>
                <p className="text-sm font-bold uppercase tracking-wide text-red-400">
                  Verifier verdict
                </p>
              </div>
              {verifier.autonomy_tier && <AutonomyBadge tier={verifier.autonomy_tier} />}
            </div>
            <p className="mt-2 text-sm text-[#9CA3AF]">Status: {verifier.status}</p>
            {(verifier.case_caveats ?? []).map((c, i) => (
              <p key={i} className="mt-1.5 text-xs italic text-[#9CA3AF]">
                <span aria-hidden="true">• </span>
                {String(c ?? '')}
              </p>
            ))}
          </Card>
        )}
      </div>
    </div>
  );
}
