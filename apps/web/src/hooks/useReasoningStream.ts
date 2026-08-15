'use client';

import { useCallback, useRef, useState } from 'react';
import { api } from '@/lib/api';

export interface AgentLane {
  agent: string;
  label: string;
  status: 'idle' | 'running' | 'done' | 'failed';
  // Why the lane failed, when it did. Log-safe text from the server.
  reason?: string;
}

export interface StreamState {
  running: boolean;
  done: boolean;
  error: string | null;
  lanes: AgentLane[];
  // Latest payload received per event type (drives the live theatre panels).
  events: Record<string, Record<string, unknown>>;
  log: Array<{ event: string; data: Record<string, unknown> }>;
}

const INITIAL: StreamState = {
  running: false,
  done: false,
  error: null,
  lanes: [],
  events: {},
  log: [],
};

/**
 * Subscribes to the reasoning SSE stream and exposes a live, reducer-style view of agent
 * activity for the Reasoning Theatre. Uses EventSource (token passed via query param).
 */
export function useReasoningStream() {
  const [state, setState] = useState<StreamState>(INITIAL);
  const sourceRef = useRef<EventSource | null>(null);
  // Guards the await between start() and opening the EventSource: a stop() in that window
  // must not leave an orphaned stream running.
  const cancelledRef = useRef(false);

  const stop = useCallback(() => {
    cancelledRef.current = true;
    sourceRef.current?.close();
    sourceRef.current = null;
  }, []);

  const start = useCallback(
    async (sessionId: string, onComplete?: () => void) => {
      stop();
      cancelledRef.current = false;
      setState({ ...INITIAL, running: true });

      // One round trip to mint the session-scoped stream token before opening the socket.
      let url: string;
      try {
        url = await api.reasoningStreamUrl(sessionId);
      } catch {
        setState((s) => ({ ...s, running: false, error: 'Could not open the reasoning stream' }));
        return;
      }
      // A stop() between the await and here means this stream was cancelled; don't open it.
      if (cancelledRef.current) return;

      const es = new EventSource(url);
      sourceRef.current = es;

      const handle = (type: string, raw: string) => {
        let data: Record<string, unknown> = {};
        try {
          data = JSON.parse(raw);
        } catch {
          /* keep-alive comment or empty */
        }
        setState((s) => reduce(s, type, data));
        if (type === 'reasoning_complete') {
          // Mark reasoning as logically complete in the theatre UI, but do NOT
          // close the stream or fetch suggestions yet — the server hasn't
          // committed them to the DB at this point.
          //
          // A lane that failed keeps its status. Sweeping every lane to 'done' at the end of
          // the run would repaint exactly the fact the run is escalated for — that an agent
          // crashed and contributed nothing — as a green tick, which is the automation bias
          // this screen exists to work against.
          setState((s) => ({
            ...s,
            lanes: s.lanes.map((l) => (l.status === 'failed' ? l : { ...l, status: 'done' })),
          }));
        }
        if (type === 'done') {
          // The 'done' event fires AFTER the server has persisted and committed
          // suggestions, so it is safe to fetch them now.
          stop();
          setState((s) => ({ ...s, running: false, done: true }));
          onComplete?.();
        }
        if (type === 'error') {
          setState((s) => ({ ...s, running: false, error: String(data.message ?? 'error') }));
        }
      };

      const NAMED = [
        'reasoning_start',
        'agent_start',
        'agent_complete',
        'agent_failed',
        'specialist',
        'hypotheses',
        'cant_miss',
        'devils_advocate',
        'investigations',
        'management',
        'drug_safety',
        'verifier',
        'conservative_resolution',
        'synthesis',
        'reasoning_complete',
        'error',
        'done',
        'intake',
      ];
      for (const name of NAMED) {
        es.addEventListener(name, (e) => handle(name, (e as MessageEvent).data));
      }
      es.onerror = () => {
        // Two different failures arrive on this one handler, and they need opposite answers.
        //
        // A transient drop mid-stream leaves the EventSource in CONNECTING: the browser is
        // already retrying and will replay from `Last-Event-ID`, so raising an error here
        // would put a red banner on a run that is about to carry on by itself.
        //
        // A stream that the server refused — an expired stream token, a 404 session, a proxy
        // that will not hold the connection — leaves it CLOSED, and the browser does not
        // retry. That case used to only clear `running`, which left the pill reading
        // "Connecting…" for as long as the clinician was willing to wait, with the eight
        // agents either never started or finished long ago. Say so, and let them restart.
        setState((s) => {
          if (s.done) return s;
          if (es.readyState !== EventSource.CLOSED) return { ...s, running: false };
          return {
            ...s,
            running: false,
            error: 'The reasoning stream closed before the run finished.',
          };
        });
      };
    },
    [stop],
  );

  return { state, start, stop };
}

function reduce(s: StreamState, event: string, data: Record<string, unknown>): StreamState {
  const log = [...s.log, { event, data }].slice(-200);
  let lanes = s.lanes;

  if (event === 'reasoning_start' && Array.isArray(data.sequence)) {
    lanes = (data.sequence as string[]).map((agent) => ({
      agent,
      label: agent.replace(/_/g, ' '),
      status: 'idle' as const,
    }));
  }
  if (event === 'agent_start' && typeof data.agent === 'string') {
    lanes = lanes.map((l) =>
      l.agent === data.agent
        ? { ...l, status: 'running', label: (data.label as string) ?? l.label }
        : l,
    );
  }
  if (event === 'agent_complete' && typeof data.agent === 'string') {
    lanes = lanes.map((l) => (l.agent === data.agent ? { ...l, status: 'done' } : l));
  }
  // An agent that crashed. The run carries on without it, degraded and escalated, so the lane
  // has to say it did not run rather than quietly stopping at 'running' — a lane stuck mid-way
  // reads as "still working" on a screen where everything else has finished.
  if (event === 'agent_failed' && typeof data.agent === 'string') {
    lanes = lanes.map((l) =>
      l.agent === data.agent
        ? { ...l, status: 'failed', reason: typeof data.reason === 'string' ? data.reason : undefined }
        : l,
    );
  }

  return { ...s, lanes, log, events: { ...s.events, [event]: data } };
}
