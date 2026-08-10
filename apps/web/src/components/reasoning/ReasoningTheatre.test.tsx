import { act, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return { ...actual, api: { ...actual.api, reasoningStreamUrl: vi.fn() } };
});

import { api } from '@/lib/api';
import { ReasoningTheatre } from './ReasoningTheatre';

type Listener = (event: { data: string }) => void;

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
    for (const cb of this.listeners[name] ?? []) {
      cb({ data: JSON.stringify(data) });
    }
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

/** The stream opens only after the session-scoped token round trip resolves. */
async function latestSource(): Promise<MockEventSource> {
  await waitFor(() => expect(MockEventSource.instances.length).toBeGreaterThan(0));
  return MockEventSource.instances.at(-1) as MockEventSource;
}

describe('ReasoningTheatre', () => {
  it('shows a waiting state before any agent lane exists', () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    expect(screen.getByText('Waiting for the orchestrator…')).toBeInTheDocument();
    expect(screen.getByText('Reasoning…')).toBeInTheDocument();
  });

  it('mints a session-scoped stream token before opening the stream', async () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    const source = await latestSource();

    expect(api.reasoningStreamUrl).toHaveBeenCalledWith('s1');
    expect(source.url).toContain('token=stream-token');
  });

  it('surfaces a failure to mint the token instead of opening an unauthenticated stream', async () => {
    vi.mocked(api.reasoningStreamUrl).mockRejectedValue(new Error('401'));
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);

    expect(await screen.findByText('Could not open the reasoning stream')).toBeInTheDocument();
    expect(MockEventSource.instances).toHaveLength(0);
  });

  it('renders agent lanes from reasoning_start and updates their status as they run', async () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    const source = await latestSource();

    act(() => {
      source.emit('reasoning_start', { sequence: ['triage', 'verifier'] });
    });
    expect(screen.getByText('triage')).toBeInTheDocument();
    expect(screen.getByText('verifier')).toBeInTheDocument();

    act(() => {
      source.emit('agent_start', { agent: 'triage', label: 'Triage' });
    });
    expect(screen.getByText('Triage')).toBeInTheDocument();
  });

  it('always renders the devils-advocate critique when present (Critical Safety Rule #5)', async () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    const source = await latestSource();

    act(() => {
      source.emit('devils_advocate', {
        critique: { summary: 'The leading diagnosis overlooks a can-not-miss alternative.' },
      });
    });
    expect(screen.getByText("Devil's advocate")).toBeInTheDocument();
    expect(
      screen.getByText('The leading diagnosis overlooks a can-not-miss alternative.'),
    ).toBeInTheDocument();
  });

  it('renders can-not-miss diagnoses distinctly', async () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    const source = await latestSource();

    act(() => {
      source.emit('cant_miss', {
        items: [{ diagnosis_name: 'Acute coronary syndrome', why: 'Chest pain with risk factors' }],
      });
    });
    expect(screen.getByText("Can't-miss conditions forced onto the differential")).toBeInTheDocument();
    expect(screen.getByText('Acute coronary syndrome')).toBeInTheDocument();
  });

  it('renders the verifier verdict with its autonomy tier', async () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    const source = await latestSource();

    act(() => {
      source.emit('verifier', {
        status: 'approved',
        autonomy_tier: 'flag_for_review',
        case_caveats: ['Limited history available'],
      });
    });
    expect(screen.getByText('Verifier verdict')).toBeInTheDocument();
    expect(screen.getByText('Status: approved')).toBeInTheDocument();
    expect(screen.getByText('• Limited history available')).toBeInTheDocument();
  });

  it('surfaces stream errors', async () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    const source = await latestSource();

    act(() => {
      source.emit('error', { message: 'LLM provider unavailable' });
    });
    expect(screen.getByText('LLM provider unavailable')).toBeInTheDocument();
    expect(screen.getByText('Error')).toBeInTheDocument();
  });

  it('calls onComplete and closes the stream when done fires', async () => {
    const onComplete = vi.fn();
    render(<ReasoningTheatre sessionId="s1" onComplete={onComplete} />);
    const source = await latestSource();

    act(() => {
      source.emit('done', {});
    });
    expect(onComplete).toHaveBeenCalledOnce();
    expect(source.closed).toBe(true);
    expect(screen.getByText('Complete')).toBeInTheDocument();
  });

  it('does not open a stream that was cancelled while the token was being minted', async () => {
    let resolveUrl: (url: string) => void = () => {};
    vi.mocked(api.reasoningStreamUrl).mockReturnValue(
      new Promise((res) => {
        resolveUrl = res;
      }),
    );

    const { unmount } = render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    unmount();
    await act(async () => {
      resolveUrl('http://api/api/v1/reasoning/s1/stream?token=late');
    });

    expect(MockEventSource.instances).toHaveLength(0);
  });
});
