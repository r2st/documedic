import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
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
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function latestSource(): MockEventSource {
  const source = MockEventSource.instances.at(-1);
  if (!source) throw new Error('no EventSource created');
  return source;
}

describe('ReasoningTheatre', () => {
  it('shows a waiting state before any agent lane exists', () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    expect(screen.getByText('Waiting for the orchestrator…')).toBeInTheDocument();
    expect(screen.getByText('Reasoning…')).toBeInTheDocument();
  });

  it('renders agent lanes from reasoning_start and updates their status as they run', () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    act(() => {
      latestSource().emit('reasoning_start', { sequence: ['triage', 'verifier'] });
    });
    expect(screen.getByText('triage')).toBeInTheDocument();
    expect(screen.getByText('verifier')).toBeInTheDocument();

    act(() => {
      latestSource().emit('agent_start', { agent: 'triage', label: 'Triage' });
    });
    expect(screen.getByText('Triage')).toBeInTheDocument();
  });

  it('always renders the devils-advocate critique when present (Critical Safety Rule #5)', () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    act(() => {
      latestSource().emit('devils_advocate', {
        critique: { summary: 'The leading diagnosis overlooks a can-not-miss alternative.' },
      });
    });
    expect(screen.getByText("Devil's advocate")).toBeInTheDocument();
    expect(
      screen.getByText('The leading diagnosis overlooks a can-not-miss alternative.'),
    ).toBeInTheDocument();
  });

  it('renders can-not-miss diagnoses distinctly', () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    act(() => {
      latestSource().emit('cant_miss', {
        items: [{ diagnosis_name: 'Acute coronary syndrome', why: 'Chest pain with risk factors' }],
      });
    });
    expect(screen.getByText("Can't-miss conditions forced onto the differential")).toBeInTheDocument();
    expect(screen.getByText('Acute coronary syndrome')).toBeInTheDocument();
  });

  it('renders the verifier verdict with its autonomy tier', () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    act(() => {
      latestSource().emit('verifier', {
        status: 'approved',
        autonomy_tier: 'flag_for_review',
        case_caveats: ['Limited history available'],
      });
    });
    expect(screen.getByText('Verifier verdict')).toBeInTheDocument();
    expect(screen.getByText('Status: approved')).toBeInTheDocument();
    expect(screen.getByText('• Limited history available')).toBeInTheDocument();
  });

  it('surfaces stream errors', () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    act(() => {
      latestSource().emit('error', { message: 'LLM provider unavailable' });
    });
    expect(screen.getByText('LLM provider unavailable')).toBeInTheDocument();
    expect(screen.getByText('Error')).toBeInTheDocument();
  });

  it('calls onComplete and closes the stream when done fires', () => {
    const onComplete = vi.fn();
    render(<ReasoningTheatre sessionId="s1" onComplete={onComplete} />);
    const source = latestSource();
    act(() => {
      source.emit('done', {});
    });
    expect(onComplete).toHaveBeenCalledOnce();
    expect(source.closed).toBe(true);
    expect(screen.getByText('Complete')).toBeInTheDocument();
  });
});
