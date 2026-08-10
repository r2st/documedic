import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return { ...actual, api: { ...actual.api, reasoningStreamUrl: vi.fn() } };
});

import { api } from '@/lib/api';
import { useReasoningStream } from './useReasoningStream';

type Listener = (event: { data: string }) => void;

/**
 * Stand-in for the browser EventSource. `emit` JSON-encodes like the real server does;
 * `emitRaw` bypasses that so tests can deliver the non-JSON frames (keep-alive comments)
 * the hook is expected to swallow.
 */
class MockEventSource {
  static instances: MockEventSource[] = [];
  listeners: Record<string, Listener[]> = {};
  closed = false;
  onerror: (() => void) | null = null;

  constructor(public url: string) {
    MockEventSource.instances.push(this);
  }

  addEventListener(name: string, cb: Listener) {
    (this.listeners[name] ??= []).push(cb);
  }

  close() {
    this.closed = true;
  }

  emit(name: string, data: unknown) {
    this.emitRaw(name, JSON.stringify(data));
  }

  emitRaw(name: string, data: string) {
    for (const cb of this.listeners[name] ?? []) cb({ data });
  }
}

beforeEach(() => {
  MockEventSource.instances = [];
  vi.stubGlobal('EventSource', MockEventSource);
  vi.mocked(api.reasoningStreamUrl)
    .mockReset()
    .mockResolvedValue('http://api/api/v1/reasoning/s1/stream?token=stream-token');
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/** The socket opens only after the session-scoped token round trip resolves. */
async function openStream(start: (id: string, onComplete?: () => void) => Promise<void>, onComplete?: () => void) {
  await act(async () => {
    await start('s1', onComplete);
  });
  await waitFor(() => expect(MockEventSource.instances.length).toBeGreaterThan(0));
  return MockEventSource.instances.at(-1) as MockEventSource;
}

describe('useReasoningStream', () => {
  it('starts idle', () => {
    const { result } = renderHook(() => useReasoningStream());
    expect(result.current.state).toEqual({
      running: false,
      done: false,
      error: null,
      lanes: [],
      events: {},
      log: [],
    });
  });

  it('opens the stream at the minted token URL', async () => {
    const { result } = renderHook(() => useReasoningStream());
    const es = await openStream(result.current.start);

    expect(api.reasoningStreamUrl).toHaveBeenCalledWith('s1');
    expect(es.url).toBe('http://api/api/v1/reasoning/s1/stream?token=stream-token');
    expect(result.current.state.running).toBe(true);
  });

  it('surfaces a soft error when the stream token cannot be minted', async () => {
    vi.mocked(api.reasoningStreamUrl).mockRejectedValue(new Error('403'));
    const { result } = renderHook(() => useReasoningStream());

    await act(async () => {
      await result.current.start('s1');
    });

    expect(MockEventSource.instances).toHaveLength(0);
    expect(result.current.state.running).toBe(false);
    expect(result.current.state.error).toBe('Could not open the reasoning stream');
  });

  it('does not open an orphaned stream when stop() lands during the token round trip', async () => {
    let release: (url: string) => void = () => {};
    vi.mocked(api.reasoningStreamUrl).mockReturnValue(
      new Promise<string>((resolve) => {
        release = resolve;
      }),
    );
    const { result } = renderHook(() => useReasoningStream());

    let started: Promise<void>;
    act(() => {
      started = result.current.start('s1');
    });
    // stop() arrives while start() is still awaiting the token.
    act(() => result.current.stop());
    await act(async () => {
      release('http://api/stream?token=t');
      await started;
    });

    expect(MockEventSource.instances).toHaveLength(0);
  });

  it('builds lanes from reasoning_start and advances them through the agent events', async () => {
    const { result } = renderHook(() => useReasoningStream());
    const es = await openStream(result.current.start);

    act(() => es.emit('reasoning_start', { sequence: ['triage_intake', 'hypothesis_panel'] }));
    expect(result.current.state.lanes).toEqual([
      { agent: 'triage_intake', label: 'triage intake', status: 'idle' },
      { agent: 'hypothesis_panel', label: 'hypothesis panel', status: 'idle' },
    ]);

    act(() => es.emit('agent_start', { agent: 'triage_intake', label: 'Triage / Intake' }));
    expect(result.current.state.lanes[0]).toEqual({
      agent: 'triage_intake',
      label: 'Triage / Intake',
      status: 'running',
    });
    expect(result.current.state.lanes[1].status).toBe('idle');

    act(() => es.emit('agent_complete', { agent: 'triage_intake' }));
    expect(result.current.state.lanes[0].status).toBe('done');
  });

  it('keeps the existing label when agent_start omits one', async () => {
    const { result } = renderHook(() => useReasoningStream());
    const es = await openStream(result.current.start);

    act(() => es.emit('reasoning_start', { sequence: ['verifier'] }));
    act(() => es.emit('agent_start', { agent: 'verifier' }));

    expect(result.current.state.lanes[0].label).toBe('verifier');
  });

  it('ignores lane events whose shape does not match', async () => {
    const { result } = renderHook(() => useReasoningStream());
    const es = await openStream(result.current.start);

    // sequence must be an array; agent must be a string. Neither should build lanes.
    act(() => es.emit('reasoning_start', { sequence: 'triage_intake' }));
    expect(result.current.state.lanes).toEqual([]);

    act(() => es.emit('agent_start', { agent: 42 }));
    act(() => es.emit('agent_complete', { agent: null }));
    expect(result.current.state.lanes).toEqual([]);
  });

  it('marks every lane done on reasoning_complete without closing the stream', async () => {
    const onComplete = vi.fn();
    const { result } = renderHook(() => useReasoningStream());
    const es = await openStream(result.current.start, onComplete);

    act(() => es.emit('reasoning_start', { sequence: ['triage_intake', 'verifier'] }));
    act(() => es.emit('reasoning_complete', {}));

    expect(result.current.state.lanes.map((l) => l.status)).toEqual(['done', 'done']);
    // The server has not committed suggestions yet at this point, so the socket stays open
    // and the completion callback must not have fired.
    expect(es.closed).toBe(false);
    expect(result.current.state.running).toBe(true);
    expect(result.current.state.done).toBe(false);
    expect(onComplete).not.toHaveBeenCalled();
  });

  it('closes the stream and fires onComplete only on done', async () => {
    const onComplete = vi.fn();
    const { result } = renderHook(() => useReasoningStream());
    const es = await openStream(result.current.start, onComplete);

    act(() => es.emit('done', {}));

    expect(es.closed).toBe(true);
    expect(result.current.state).toMatchObject({ running: false, done: true });
    expect(onComplete).toHaveBeenCalledTimes(1);
  });

  it('completes cleanly when no onComplete callback was supplied', async () => {
    const { result } = renderHook(() => useReasoningStream());
    const es = await openStream(result.current.start);

    act(() => es.emit('done', {}));

    expect(result.current.state.done).toBe(true);
  });

  it('records an error event message, falling back when none is given', async () => {
    const { result } = renderHook(() => useReasoningStream());
    const es = await openStream(result.current.start);

    act(() => es.emit('error', { message: 'verifier rejected the output' }));
    expect(result.current.state).toMatchObject({
      running: false,
      error: 'verifier rejected the output',
    });

    act(() => es.emit('error', {}));
    expect(result.current.state.error).toBe('error');
  });

  it('swallows a non-JSON frame instead of throwing', async () => {
    const { result } = renderHook(() => useReasoningStream());
    const es = await openStream(result.current.start);

    act(() => es.emitRaw('synthesis', ': keep-alive'));

    expect(result.current.state.events.synthesis).toEqual({});
    expect(result.current.state.error).toBeNull();
  });

  it('stops running on a transport error, but leaves a finished stream alone', async () => {
    const { result } = renderHook(() => useReasoningStream());
    const es = await openStream(result.current.start);

    act(() => es.onerror?.());
    expect(result.current.state.running).toBe(false);
    expect(result.current.state.done).toBe(false);

    // Once done, a late transport error must not rewrite the terminal state.
    act(() => es.emit('done', {}));
    const settled = result.current.state;
    act(() => es.onerror?.());
    expect(result.current.state).toBe(settled);
  });

  it('keeps the latest payload per event type and an ordered log', async () => {
    const { result } = renderHook(() => useReasoningStream());
    const es = await openStream(result.current.start);

    act(() => es.emit('hypotheses', { ranked: ['a'] }));
    act(() => es.emit('hypotheses', { ranked: ['b'] }));
    act(() => es.emit('cant_miss', { items: [] }));

    expect(result.current.state.events.hypotheses).toEqual({ ranked: ['b'] });
    expect(result.current.state.log.map((e) => e.event)).toEqual([
      'hypotheses',
      'hypotheses',
      'cant_miss',
    ]);
  });

  it('caps the log at the most recent 200 entries', async () => {
    const { result } = renderHook(() => useReasoningStream());
    const es = await openStream(result.current.start);

    act(() => {
      for (let i = 0; i < 205; i++) es.emit('specialist', { i });
    });

    expect(result.current.state.log).toHaveLength(200);
    expect(result.current.state.log[199].data).toEqual({ i: 204 });
    expect(result.current.state.log[0].data).toEqual({ i: 5 });
  });

  it('closes the previous stream when start() is called again', async () => {
    const { result } = renderHook(() => useReasoningStream());
    const first = await openStream(result.current.start);

    act(() => first.emit('reasoning_start', { sequence: ['triage_intake'] }));
    const second = await openStream(result.current.start);

    expect(first.closed).toBe(true);
    expect(second).not.toBe(first);
    expect(second.closed).toBe(false);
    expect(MockEventSource.instances).toHaveLength(2);
    // A restart resets the reducer state rather than accumulating across runs.
    expect(result.current.state.lanes).toEqual([]);
    expect(result.current.state.log).toEqual([]);
  });

  it('stop() closes the socket and tolerates being called with none open', async () => {
    const { result } = renderHook(() => useReasoningStream());
    const es = await openStream(result.current.start);

    act(() => result.current.stop());
    expect(es.closed).toBe(true);

    // Second call has no socket to close and must not throw.
    expect(() => act(() => result.current.stop())).not.toThrow();
  });
});
