'use client';

import { useEffect } from 'react';
import { Card } from '@/components/ui';
import { useReasoningStream } from '@/hooks/useReasoningStream';
import { AutonomyBadge } from './badges';
import type { AutonomyTier } from '@/lib/types';

const DOT: Record<string, string> = {
  idle: 'bg-slate-300 ring-2 ring-slate-100',
  running: 'bg-brand-500 ring-2 ring-brand-100 animate-pulse',
  done: 'bg-emerald-500 ring-2 ring-emerald-100',
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

  useEffect(() => {
    void start(sessionId, onComplete);
    return () => stop();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  const ev = state.events;
  const cantMiss = (ev.cant_miss?.items as Array<{ diagnosis_name: string; why: string }>) ?? [];
  const devil = ev.devils_advocate?.critique as Record<string, unknown> | undefined;
  const verifier = ev.verifier as
    | { status?: string; autonomy_tier?: AutonomyTier; case_caveats?: string[] }
    | undefined;
  const hypotheses =
    (ev.hypotheses?.hypotheses as Array<{
      diagnosis_name: string;
      probability_band: string;
      cant_miss_flag?: boolean;
    }>) ?? [];

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
      : 'text-brand-600';

  return (
    <div className="space-y-4 animate-fade-in">
      <Card>
        <div className="mb-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <svg className="h-5 w-5 text-brand-600" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09ZM18.259 8.715 18 9.75l-.259-1.035a3.375 3.375 0 0 0-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 0 0 2.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 0 0 2.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 0 0-2.455 2.456ZM16.894 20.567 16.5 21.75l-.394-1.183a2.25 2.25 0 0 0-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 0 0 1.423-1.423l.394-1.183.394 1.183a2.25 2.25 0 0 0 1.423 1.423l1.183.394-1.183.394a2.25 2.25 0 0 0-1.423 1.423Z" />
            </svg>
            <h2 className="text-lg font-semibold text-slate-900">Reasoning Theatre</h2>
          </div>
          <span className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-semibold ${statusColor} ${
            state.running ? 'bg-brand-50 ring-1 ring-brand-200' : state.done ? 'bg-emerald-50 ring-1 ring-emerald-200' : state.error ? 'bg-red-50 ring-1 ring-red-200' : 'bg-slate-50 ring-1 ring-slate-200'
          }`}>
            {state.running && (
              <span className="h-1.5 w-1.5 rounded-full bg-brand-500 animate-pulse" />
            )}
            {statusLabel}
          </span>
        </div>
        {state.error && (
          <div className="mb-3 flex items-start gap-2 rounded-lg bg-red-50 p-3 text-sm text-red-700 ring-1 ring-red-200">
            <svg className="mt-0.5 h-4 w-4 flex-shrink-0" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
            </svg>
            <span>{state.error}</span>
          </div>
        )}
        <ol className="space-y-2">
          {state.lanes.map((lane) => (
            <li key={lane.agent} className="flex items-center gap-3 rounded-lg px-3 py-2 text-sm hover:bg-slate-50 transition-colors">
              <span className={`inline-block h-3 w-3 rounded-full ${DOT[lane.status]}`} />
              <span
                className={lane.status === 'done' ? 'text-slate-500' : 'font-medium text-slate-900'}
              >
                {lane.label}
              </span>
              {lane.status === 'done' && (
                <svg className="h-4 w-4 text-emerald-500" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" d="m4.5 12.75 6 6 9-13.5" />
                </svg>
              )}
            </li>
          ))}
          {state.lanes.length === 0 && (
            <li className="flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-slate-400">
              <span className="inline-block h-3 w-3 animate-pulse rounded-full bg-slate-200" />
              Waiting for the orchestrator…
            </li>
          )}
        </ol>
      </Card>

      {hypotheses.length > 0 && (
        <Card className="animate-slide-up">
          <div className="mb-3 flex items-center gap-2">
            <svg className="h-5 w-5 text-slate-500" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 12h16.5m-16.5 3.75h16.5M3.75 19.5h16.5M5.625 4.5h12.75a1.875 1.875 0 0 1 0 3.75H5.625a1.875 1.875 0 0 1 0-3.75Z" />
            </svg>
            <p className="text-sm font-semibold text-slate-700">Hypotheses (live ranking)</p>
          </div>
          <ul className="space-y-1.5">
            {hypotheses.map((h, i) => (
              <li key={i} className="flex items-center justify-between rounded-lg bg-slate-50 px-3 py-2 text-sm">
                <span className="text-slate-800">{h.diagnosis_name}</span>
                <span className="rounded-full bg-slate-200 px-2 py-0.5 text-xs font-medium uppercase text-slate-600">
                  {h.probability_band}
                </span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      {cantMiss.length > 0 && (
        <div className="animate-slide-up rounded-xl border-2 border-orange-300 bg-orange-50 p-5">
          <div className="mb-2 flex items-center gap-2">
            <span className="flex h-6 w-6 items-center justify-center rounded-full bg-orange-500 text-xs font-bold text-white">!</span>
            <p className="text-sm font-bold uppercase tracking-wide text-orange-800">
              Can&apos;t-miss conditions forced onto the differential
            </p>
          </div>
          <ul className="space-y-1.5 pl-8">
            {cantMiss.map((c, i) => (
              <li key={i} className="text-sm text-orange-900">
                <span className="font-semibold">{String(c.diagnosis_name ?? '')}</span> — {String(c.why ?? '')}
              </li>
            ))}
          </ul>
        </div>
      )}

      {devil && (
        <div className="animate-slide-up rounded-xl border-l-4 border-purple-500 bg-purple-50 p-5">
          <div className="mb-2 flex items-center gap-2">
            <svg className="h-5 w-5 text-purple-600" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 18v-5.25m0 0a6.01 6.01 0 0 0 1.5-.189m-1.5.189a6.01 6.01 0 0 1-1.5-.189m3.75 7.478a12.06 12.06 0 0 1-4.5 0m3.75 2.383a14.406 14.406 0 0 1-3 0M14.25 18v-.192c0-.983.658-1.823 1.508-2.316a7.5 7.5 0 1 0-7.517 0c.85.493 1.509 1.333 1.509 2.316V18" />
            </svg>
            <p className="text-sm font-bold uppercase tracking-wide text-purple-800">
              Devil&apos;s advocate
            </p>
          </div>
          <p className="text-sm text-purple-900">{String(devil.summary ?? '')}</p>
        </div>
      )}

      {verifier && (
        <Card className="animate-slide-up border-l-4 border-red-500">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <svg className="h-5 w-5 text-red-600" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75 11.25 15 15 9.75m-3-7.036A11.959 11.959 0 0 1 3.598 6 11.99 11.99 0 0 0 3 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285Z" />
              </svg>
              <p className="text-sm font-bold uppercase tracking-wide text-red-700">Verifier verdict</p>
            </div>
            {verifier.autonomy_tier && <AutonomyBadge tier={verifier.autonomy_tier} />}
          </div>
          <p className="mt-2 text-sm text-slate-600">Status: {verifier.status}</p>
          {(verifier.case_caveats ?? []).map((c, i) => (
            <p key={i} className="mt-1.5 text-xs italic text-slate-500">
              • {String(c ?? '')}
            </p>
          ))}
        </Card>
      )}
    </div>
  );
}
