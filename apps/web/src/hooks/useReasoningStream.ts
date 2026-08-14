'use client';

import { useCallback, useRef, useState } from 'react';
import { api } from '@/lib/api';

export interface AgentLane {
  agent: string;
  label: string;
  status: 'idle' | 'running' | 'done';
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
          setState((s) => ({ ...s, lanes: s.lanes.map((l) => ({ ...l, status: 'done' })) }));
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
        // Browser will retry; if it never opened, surface a soft error.
        setState((s) => (s.done ? s : { ...s, running: false }));
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

  return { ...s, lanes, log, events: { ...s.events, [event]: data } };
}
