import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ClinicalSuggestion } from '@/lib/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return { ...actual, api: { ...actual.api, recordDecision: vi.fn() } };
});

import { api } from '@/lib/api';
import { SuggestionCard } from './SuggestionCard';

function suggestion(overrides: Partial<ClinicalSuggestion> = {}): ClinicalSuggestion {
  return {
    id: 'sug-1',
    session_id: 'sess-1',
    patient_id: 'pat-1',
    output_type: 'differential',
    autonomy_tier: 'suggestive',
    confidence_band: 'moderate',
    title: 'Community-acquired pneumonia',
    body: 'Guidelines support considering CAP given the presentation.',
    evidence: {
      evidence_for: [{ text: 'Productive cough and fever for 3 days' }],
      evidence_against: [{ text: 'No hypoxia on exam' }],
    },
    citations: [],
    agent_trace: [],
    verifier_verdict: {},
    devils_advocate: {},
    is_hard_block: false,
    cant_miss_flag: false,
    supersedes_id: null,
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

describe('SuggestionCard', () => {
  beforeEach(() => {
    vi.mocked(api.recordDecision).mockReset().mockResolvedValue({ id: 'd1', decision: 'ok' });
  });

  it('renders evidence-for and evidence-against sections before the conclusion (anti-automation-bias ordering)', () => {
    const { container } = render(<SuggestionCard suggestion={suggestion()} sessionId="sess-1" />);
    const text = container.textContent ?? '';
    const evidenceIdx = text.indexOf('Productive cough and fever for 3 days');
    const conclusionIdx = text.indexOf('Assessment to consider');
    expect(evidenceIdx).toBeGreaterThan(-1);
    expect(conclusionIdx).toBeGreaterThan(-1);
    expect(evidenceIdx).toBeLessThan(conclusionIdx);
  });

  it('always renders the devils-advocate critique when present, not collapsed (Critical Safety Rule #5)', () => {
    render(
      <SuggestionCard
        suggestion={suggestion({
          devils_advocate: { summary: 'Consider atypical pneumonia coverage before narrowing.' },
        })}
        sessionId="sess-1"
      />,
    );
    expect(screen.getByText("Devil's advocate — counter-argument")).toBeInTheDocument();
    expect(
      screen.getByText('Consider atypical pneumonia coverage before narrowing.'),
    ).toBeVisible();
  });

  it('does not render a devils-advocate section when there is no critique', () => {
    render(<SuggestionCard suggestion={suggestion({ devils_advocate: {} })} sessionId="sess-1" />);
    expect(screen.queryByText(/Devil's advocate/)).not.toBeInTheDocument();
  });

  it('gates flag_for_review output behind an explicit engagement click before showing evidence', async () => {
    render(
      <SuggestionCard
        suggestion={suggestion({ autonomy_tier: 'flag_for_review' })}
        sessionId="sess-1"
      />,
    );
    expect(screen.getByText(/flagged for review/)).toBeInTheDocument();
    expect(screen.queryByText('Assessment to consider')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Review evidence & assessment' }));
    expect(screen.getByText('Assessment to consider')).toBeInTheDocument();
    expect(api.recordDecision).toHaveBeenCalledWith('sess-1', 'sug-1', 'acknowledged', undefined);
  });

  it('does not gate suggestive-tier output behind engagement', () => {
    render(<SuggestionCard suggestion={suggestion({ autonomy_tier: 'suggestive' })} sessionId="sess-1" />);
    expect(screen.getByText('Assessment to consider')).toBeInTheDocument();
  });

  it('renders a hard block distinctly and requires documented reasoning before it can be overridden', async () => {
    render(
      <SuggestionCard
        suggestion={suggestion({ is_hard_block: true, body: 'Documented allergy conflict.' })}
        sessionId="sess-1"
      />,
    );
    expect(screen.getByText('Hard block — cannot proceed')).toBeInTheDocument();
    const overrideButton = screen.getByRole('button', { name: 'Override with documented reason' });
    expect(overrideButton).toBeDisabled();

    await userEvent.type(
      screen.getByPlaceholderText(/Documented clinical reasoning is required/),
      'Nephrology approved continued use with monitoring.',
    );
    expect(overrideButton).toBeEnabled();

    await userEvent.click(overrideButton);
    expect(api.recordDecision).toHaveBeenCalledWith(
      'sess-1',
      'sug-1',
      'overridden',
      'Nephrology approved continued use with monitoring.',
    );
    expect(await screen.findByText(/Recorded decision: overridden/)).toBeInTheDocument();
  });

  it('lets a hard block be acknowledged without overriding it', async () => {
    render(<SuggestionCard suggestion={suggestion({ is_hard_block: true })} sessionId="sess-1" />);
    await userEvent.click(screen.getByRole('button', { name: 'Acknowledge (do not override)' }));
    expect(api.recordDecision).toHaveBeenCalledWith('sess-1', 'sug-1', 'acknowledged', undefined);
  });

  it('never renders raw non-string LLM output (asText guard against React error #31)', () => {
    render(
      <SuggestionCard
        suggestion={suggestion({
          // Malformed LLM output: object where a string was expected.
          body: { rationale: 'Structured object instead of a string' } as unknown as string,
        })}
        sessionId="sess-1"
      />,
    );
    expect(screen.getByText('Structured object instead of a string')).toBeInTheDocument();
  });

  it('renders every part of the devils-advocate critique, not just the summary', () => {
    render(
      <SuggestionCard
        suggestion={suggestion({
          devils_advocate: {
            summary: 'The leading impression rests on limited data.',
            disconfirming_evidence: ['No chest imaging has confirmed consolidation.', ''],
            alternative_explanations: ['Acute bronchitis', 'Viral upper respiratory infection'],
            base_rate_caveat: 'Most acute cough is viral and self-limiting.',
          },
        })}
        sessionId="sess-1"
      />,
    );
    expect(screen.getByText('The leading impression rests on limited data.')).toBeVisible();
    expect(screen.getByText('No chest imaging has confirmed consolidation.')).toBeVisible();
    expect(
      screen.getByText(/Acute bronchitis, Viral upper respiratory infection/),
    ).toBeVisible();
    expect(screen.getByText('Most acute cough is viral and self-limiting.')).toBeVisible();
  });

  it('renders guideline citations with source, document, heading and snippet', () => {
    render(
      <SuggestionCard
        suggestion={suggestion({
          output_type: 'management',
          citations: [
            {
              source: 'icmr',
              document_title: 'Standard Treatment Workflow — Pneumonia',
              heading: 'Empirical antibiotics',
              section_id: 'stw-cap-3.2',
              snippet: 'Assess severity before selecting an empirical regimen.',
              score: 0.91,
            },
          ],
        })}
        sessionId="sess-1"
      />,
    );
    expect(screen.getByText('Guideline citations (grounding)')).toBeInTheDocument();
    expect(screen.getByText('ICMR')).toBeInTheDocument();
    expect(screen.getByText(/Standard Treatment Workflow — Pneumonia/)).toBeInTheDocument();
    expect(screen.getByText(/Empirical antibiotics/)).toBeInTheDocument();
    expect(screen.getByText('[stw-cap-3.2]')).toBeInTheDocument();
    expect(
      screen.getByText(/Assess severity before selecting an empirical regimen\./),
    ).toBeInTheDocument();
  });

  it('renders a citation with no heading or snippet without emitting placeholder text', () => {
    render(
      <SuggestionCard
        suggestion={suggestion({
          citations: [
            {
              source: 'nice',
              document_title: 'NG191',
              heading: null,
              section_id: 'ng191-1.1',
              snippet: null,
              score: null,
            },
          ],
        })}
        sessionId="sess-1"
      />,
    );
    expect(screen.getByText('NICE')).toBeInTheDocument();
    expect(screen.getByText('[ng191-1.1]')).toBeInTheDocument();
    expect(screen.queryByText(/undefined|null/)).not.toBeInTheDocument();
  });

  it('does not render a citations block when the suggestion has no citations', () => {
    render(<SuggestionCard suggestion={suggestion({ citations: [] })} sessionId="sess-1" />);
    expect(screen.queryByText('Guideline citations (grounding)')).not.toBeInTheDocument();
  });
});
