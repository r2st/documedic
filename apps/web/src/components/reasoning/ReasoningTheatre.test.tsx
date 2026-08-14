import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
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
    expect(
      screen.getByText("Can't-miss conditions forced onto the differential"),
    ).toBeInTheDocument();
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
    // The bullet is a decorative sibling span (aria-hidden), so the caveat text stands alone.
    expect(screen.getByText('Limited history available')).toBeInTheDocument();
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

  it('marks a lane done when its agent completes and keeps the remaining lanes active', async () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    const source = await latestSource();

    act(() => {
      source.emit('reasoning_start', { sequence: ['triage', 'verifier'] });
      source.emit('agent_start', { agent: 'triage', label: 'Triage' });
      source.emit('agent_complete', { agent: 'triage' });
    });

    // The completed lane gets the muted treatment plus a tick; the pending one stays emphasised.
    const done = screen.getByText('Triage');
    expect(done.className).toContain('text-slate-500');
    expect(screen.getByText('verifier').className).toContain('font-medium');
    expect(done.closest('li')?.querySelector('svg')).not.toBeNull();
  });

  it('spells out every lane status for a screen reader, not just the dot colour', async () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    const source = await latestSource();

    // Three lanes held at the three different statuses at once, so each arm of the status map
    // is asserted against a lane whose only other cues (dot colour, tick) are visual.
    act(() => {
      source.emit('reasoning_start', { sequence: ['triage', 'verifier', 'synthesis'] });
      source.emit('agent_start', { agent: 'triage', label: 'Triage' });
      source.emit('agent_complete', { agent: 'triage' });
      source.emit('agent_start', { agent: 'verifier', label: 'Verifier' });
    });

    const laneStatus = (label: string) =>
      screen.getByText(label).closest('li')?.querySelector('.sr-only')?.textContent;

    expect(laneStatus('Triage')).toBe('done');
    expect(laneStatus('Verifier')).toBe('running');
    // Untouched by any agent event, so still queued behind the ones that have started.
    expect(laneStatus('synthesis')).toBe('waiting');
  });

  it('renders the live hypothesis ranking with each probability band', async () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    const source = await latestSource();

    act(() => {
      source.emit('hypotheses', {
        hypotheses: [
          { diagnosis_name: 'Community-acquired pneumonia', probability_band: 'moderate' },
          { diagnosis_name: 'Acute bronchitis', probability_band: 'low' },
        ],
      });
    });

    expect(screen.getByText('Hypotheses (live ranking)')).toBeInTheDocument();
    expect(screen.getByText('Community-acquired pneumonia')).toBeInTheDocument();
    expect(screen.getByText('moderate')).toBeInTheDocument();
    expect(screen.getByText('Acute bronchitis')).toBeInTheDocument();
    expect(screen.getByText('low')).toBeInTheDocument();
  });

  it('does not render a hypothesis card before any hypothesis has streamed', async () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    await latestSource();
    expect(screen.queryByText('Hypotheses (live ranking)')).not.toBeInTheDocument();
  });
});

describe('ReasoningTheatre partial payloads', () => {
  it('renders a can-not-miss entry that arrives without its narrative fields', async () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    const source = await latestSource();

    // Rule #3: a can't-miss item is never dropped for being incomplete -- the panel still
    // has to appear so the clinician sees that the sentinel fired.
    act(() => {
      source.emit('cant_miss', { items: [{}] });
    });

    expect(
      screen.getByText("Can't-miss conditions forced onto the differential"),
    ).toBeInTheDocument();
  });

  it('renders the devils-advocate panel even when the critique carries no summary', async () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    const source = await latestSource();

    // Rule #5: dissent is never suppressed, so an empty summary must not collapse the panel.
    act(() => {
      source.emit('devils_advocate', { critique: {} });
    });

    expect(screen.getByText("Devil's advocate")).toBeInTheDocument();
  });

  it('renders a verifier verdict that carries no tier and no caveats', async () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    const source = await latestSource();

    act(() => {
      source.emit('verifier', { status: 'approved' });
    });

    expect(screen.getByText('Verifier verdict')).toBeInTheDocument();
    expect(screen.getByText('Status: approved')).toBeInTheDocument();
    expect(screen.queryByText(/^•/)).not.toBeInTheDocument();
  });
});

describe('ReasoningTheatre caveat rendering', () => {
  it('renders a null caveat as an empty bullet rather than the text "null"', async () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    const source = await latestSource();

    act(() => {
      source.emit('verifier', { status: 'approved', case_caveats: [null, 'Limited history'] });
    });

    expect(screen.getByText('Limited history')).toBeInTheDocument();
    expect(screen.queryByText(/null/)).not.toBeInTheDocument();
  });
});

describe('ReasoningTheatre reconnect', () => {
  it('offers a reconnect when the token could not be minted, and reopens on it', async () => {
    // A failed stream used to be a dead end: the banner said what went wrong and the only way
    // on was to leave the encounter and start the whole eight-agent run again.
    vi.mocked(api.reasoningStreamUrl).mockRejectedValueOnce(new Error('401'));
    const user = userEvent.setup();
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);

    await screen.findByText('Could not open the reasoning stream');
    expect(MockEventSource.instances).toHaveLength(0);

    vi.mocked(api.reasoningStreamUrl).mockResolvedValue(
      'http://api/api/v1/reasoning/s1/stream?token=fresh-token',
    );
    await user.click(screen.getByRole('button', { name: 'Reconnect' }));

    const source = await latestSource();
    expect(source.url).toContain('token=fresh-token');
    await waitFor(() =>
      expect(screen.queryByText('Could not open the reasoning stream')).not.toBeInTheDocument(),
    );
  });

  it('reconnects to the same session rather than re-running the agents', async () => {
    const user = userEvent.setup();
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    const first = await latestSource();

    act(() => {
      first.emit('error', { message: 'LLM provider unavailable' });
    });
    await screen.findByText('LLM provider unavailable');

    await user.click(screen.getByRole('button', { name: 'Reconnect' }));

    await waitFor(() => expect(MockEventSource.instances).toHaveLength(2));
    // Same session, and the previous socket is not left open alongside the new one.
    expect(api.reasoningStreamUrl).toHaveBeenLastCalledWith('s1');
    expect(first.closed).toBe(true);
  });

  it('does not offer a reconnect while the run is healthy', async () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    const source = await latestSource();

    act(() => {
      source.emit('reasoning_start', { sequence: ['triage'] });
    });

    expect(screen.queryByRole('button', { name: 'Reconnect' })).not.toBeInTheDocument();
  });
});
