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

  it('flattens an array of malformed LLM output into readable text', () => {
    render(
      <SuggestionCard
        suggestion={suggestion({
          body: ['Consolidation on imaging', '', 'Raised inflammatory markers'] as unknown as string,
        })}
        sessionId="sess-1"
      />,
    );
    // Empty members are dropped rather than leaving a dangling separator.
    expect(
      screen.getByText('Consolidation on imaging; Raised inflammatory markers'),
    ).toBeInTheDocument();
  });

  it('stringifies a primitive that arrived where prose was expected', () => {
    render(<SuggestionCard suggestion={suggestion({ body: true as unknown as string })} sessionId="sess-1" />);
    expect(screen.getByText('true')).toBeInTheDocument();
  });

  it('falls back to the values of an object with none of the recognised text keys', () => {
    render(
      <SuggestionCard
        suggestion={suggestion({
          body: { finding: 'Crackles at the left base', severity: 'moderate' } as unknown as string,
        })}
        sessionId="sess-1"
      />,
    );
    expect(screen.getByText('Crackles at the left base; moderate')).toBeInTheDocument();
  });

  it('renders nothing rather than crashing when a value is of an unrenderable type', () => {
    render(
      <SuggestionCard
        // A function is the one typeof asText cannot coerce -- it must degrade to empty,
        // never reach React as-is.
        suggestion={suggestion({ body: (() => 'nope') as unknown as string })}
        sessionId="sess-1"
      />,
    );
    // The card still renders its title; the unrenderable body is simply dropped.
    expect(screen.getByText('Community-acquired pneumonia')).toBeInTheDocument();
    expect(screen.queryByText(/nope/)).not.toBeInTheDocument();
  });

  it('attributes an evidence item to its source reference when one is given', () => {
    render(
      <SuggestionCard
        suggestion={suggestion({
          evidence: {
            evidence_for: [{ text: 'CRP 180 mg/L', source_ref: 'lab:2026-01-02' }],
            evidence_against: [],
          },
        })}
        sessionId="sess-1"
      />,
    );
    expect(screen.getByText('(lab:2026-01-02)')).toBeInTheDocument();
  });

  it('renders the evidence block when only against-evidence is present', () => {
    render(
      <SuggestionCard
        suggestion={suggestion({
          evidence: { evidence_for: [], evidence_against: [{ text: 'Afebrile throughout' }] },
        })}
        sessionId="sess-1"
      />,
    );
    expect(screen.getByText('Evidence against')).toBeInTheDocument();
    expect(screen.queryByText('Evidence supporting')).not.toBeInTheDocument();
  });

  it('survives an evidence payload missing both arrays entirely', () => {
    render(<SuggestionCard suggestion={suggestion({ evidence: {} })} sessionId="sess-1" />);
    expect(screen.queryByText('Evidence supporting')).not.toBeInTheDocument();
    expect(screen.queryByText('Evidence against')).not.toBeInTheDocument();
    // The conclusion still renders -- absent evidence must not blank the card.
    expect(screen.getByText('Assessment to consider')).toBeInTheDocument();
  });

  it("surfaces the can't-miss badge so a time-critical diagnosis is never buried", () => {
    render(<SuggestionCard suggestion={suggestion({ cant_miss_flag: true })} sessionId="sess-1" />);
    expect(screen.getByText(/can'?t.?miss/i)).toBeInTheDocument();
  });

  it('omits the probability band badge when the model returned no confidence', () => {
    render(
      <SuggestionCard
        suggestion={suggestion({ confidence_band: null as unknown as 'moderate' })}
        sessionId="sess-1"
      />,
    );
    expect(screen.getByText('Assessment to consider')).toBeInTheDocument();
  });
});
