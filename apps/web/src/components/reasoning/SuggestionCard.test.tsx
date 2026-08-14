import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ClinicalSuggestion } from '@/lib/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return { ...actual, api: { ...actual.api, recordDecision: vi.fn() } };
});

import { api, ApiError } from '@/lib/api';
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
    render(
      <SuggestionCard
        suggestion={suggestion({ autonomy_tier: 'suggestive' })}
        sessionId="sess-1"
      />,
    );
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
    expect(screen.getByText(/Acute bronchitis, Viral upper respiratory infection/)).toBeVisible();
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
          body: [
            'Consolidation on imaging',
            '',
            'Raised inflammatory markers',
          ] as unknown as string,
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
    render(
      <SuggestionCard
        suggestion={suggestion({ body: true as unknown as string })}
        sessionId="sess-1"
      />,
    );
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

describe('SuggestionCard decision failures', () => {
  beforeEach(() => {
    vi.mocked(api.recordDecision).mockReset();
  });

  it('says so when a hard-block override never reaches the audit log', async () => {
    // The worst silent failure on this screen: the clinician documents their reasoning,
    // presses override, and the card previously gave back nothing at all -- no confirmation
    // and no error -- with the override missing from the immutable trail.
    vi.mocked(api.recordDecision).mockRejectedValue(new TypeError('Failed to fetch'));
    const user = userEvent.setup();
    render(<SuggestionCard suggestion={suggestion({ is_hard_block: true })} sessionId="sess-1" />);

    await user.type(
      screen.getByPlaceholderText(/Documented clinical reasoning is required/),
      'Nephrology approved continued use with monitoring.',
    );
    await user.click(screen.getByRole('button', { name: 'Override with documented reason' }));

    const banners = await screen.findAllByRole('alert');
    const failure = banners.find((b) => /may not have completed/i.test(b.textContent ?? ''));
    expect(failure).toBeDefined();
    expect(failure).toHaveTextContent(/this overridden decision may not have completed/i);
    // And it must not claim the decision was recorded.
    expect(screen.queryByText(/Recorded decision/)).not.toBeInTheDocument();
  });

  it('re-sends the same decision and reason when the override is retried', async () => {
    vi.mocked(api.recordDecision).mockRejectedValueOnce(new TypeError('Failed to fetch'));
    const user = userEvent.setup();
    render(<SuggestionCard suggestion={suggestion({ is_hard_block: true })} sessionId="sess-1" />);

    await user.type(
      screen.getByPlaceholderText(/Documented clinical reasoning is required/),
      'Nephrology approved continued use with monitoring.',
    );
    await user.click(screen.getByRole('button', { name: 'Override with documented reason' }));
    await screen.findByRole('button', { name: 'Record it again' });

    vi.mocked(api.recordDecision).mockResolvedValue({ id: 'd1', decision: 'overridden' });
    await user.click(screen.getByRole('button', { name: 'Record it again' }));

    expect(api.recordDecision).toHaveBeenNthCalledWith(
      2,
      'sess-1',
      'sug-1',
      'overridden',
      'Nephrology approved continued use with monitoring.',
    );
    expect(await screen.findByText(/Recorded decision: overridden/)).toBeInTheDocument();
  });

  it('surfaces the API message when the server rejects the decision', async () => {
    vi.mocked(api.recordDecision).mockRejectedValue(
      new ApiError(409, 'already_decided', 'A decision is already recorded for this suggestion.'),
    );
    const user = userEvent.setup();
    render(<SuggestionCard suggestion={suggestion({ is_hard_block: true })} sessionId="sess-1" />);

    await user.click(screen.getByRole('button', { name: 'Acknowledge (do not override)' }));

    const banners = await screen.findAllByRole('alert');
    expect(
      banners.some((b) =>
        /A decision is already recorded for this suggestion\./.test(b.textContent ?? ''),
      ),
    ).toBe(true);
  });

  it('keeps the evidence visible when a flag-for-review acknowledgement fails to record', async () => {
    // Critical Safety Rule #6 -- the clinician engaged, so hiding the evidence behind a
    // network failure would be the worse of the two errors. The unaudited engagement is what
    // has to be said out loud.
    vi.mocked(api.recordDecision).mockRejectedValue(new TypeError('Failed to fetch'));
    const user = userEvent.setup();
    render(
      <SuggestionCard
        suggestion={suggestion({ autonomy_tier: 'flag_for_review' })}
        sessionId="sess-1"
      />,
    );

    await user.click(screen.getByRole('button', { name: 'Review evidence & assessment' }));

    expect(screen.getByText('Assessment to consider')).toBeInTheDocument();
    expect(await screen.findByRole('alert')).toHaveTextContent(
      /this acknowledged decision may not have completed/i,
    );
  });

  it('clears the previous failure while the retry is in flight', async () => {
    vi.mocked(api.recordDecision).mockRejectedValueOnce(new TypeError('Failed to fetch'));
    const user = userEvent.setup();
    render(<SuggestionCard suggestion={suggestion({ is_hard_block: true })} sessionId="sess-1" />);

    await user.click(screen.getByRole('button', { name: 'Acknowledge (do not override)' }));
    await screen.findByRole('button', { name: 'Record it again' });

    vi.mocked(api.recordDecision).mockResolvedValue({ id: 'd1', decision: 'acknowledged' });
    await user.click(screen.getByRole('button', { name: 'Record it again' }));

    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Record it again' })).not.toBeInTheDocument(),
    );
    expect(screen.getByText(/Recorded decision: acknowledged/)).toBeInTheDocument();
  });

  it('disables both hard-block controls while a decision is being written', async () => {
    // Double-submitting an override would append a second record to an append-only trail.
    let release: (() => void) | undefined;
    vi.mocked(api.recordDecision).mockReturnValue(
      new Promise((resolve) => {
        release = () => resolve({ id: 'd1', decision: 'acknowledged' });
      }),
    );
    const user = userEvent.setup();
    render(<SuggestionCard suggestion={suggestion({ is_hard_block: true })} sessionId="sess-1" />);

    await user.click(screen.getByRole('button', { name: 'Acknowledge (do not override)' }));

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Acknowledge (do not override)' })).toBeDisabled(),
    );
    expect(screen.getByRole('button', { name: 'Override with documented reason' })).toBeDisabled();

    release?.();
    expect(await screen.findByText(/Recorded decision: acknowledged/)).toBeInTheDocument();
    expect(api.recordDecision).toHaveBeenCalledOnce();
  });
});

/**
 * Suggestions are immutable and persisted, so a card renders whatever the reasoning engine
 * recorded — including sessions recorded before the engine coerced these fields. Both list
 * fields of the critique, and both evidence lists, are rendered by mapping over them; a string
 * survives the `?? []` guard and then has no `.map`, which throws during render and unmounts
 * the whole card. The Devil's-Advocate case is the one that matters most: Critical Safety Rule
 * #5 says the counter-argument is always shown, and a malformed one took the suggestion it was
 * attached to down with it — leaving the leading hypothesis on screen with nothing against it.
 */
describe('SuggestionCard with malformed reasoning output', () => {
  beforeEach(() => {
    vi.mocked(api.recordDecision).mockReset().mockResolvedValue({ id: 'd1', decision: 'ok' });
  });

  it('renders a devils-advocate critique whose evidence arrived as a string', () => {
    render(
      <SuggestionCard
        suggestion={suggestion({
          devils_advocate: {
            summary: 'The findings are not specific.',
            disconfirming_evidence: 'The ECG was normal throughout.',
            alternative_explanations: 'Gastro-oesophageal reflux',
          },
        })}
        sessionId="sess-1"
      />,
    );

    expect(screen.getByText(/Devil's advocate/i)).toBeInTheDocument();
    expect(screen.getByText('The ECG was normal throughout.')).toBeInTheDocument();
    expect(screen.getByText(/Gastro-oesophageal reflux/)).toBeInTheDocument();
  });

  it('flattens objects inside the dissent lists rather than white-screening on them', () => {
    render(
      <SuggestionCard
        suggestion={suggestion({
          devils_advocate: {
            disconfirming_evidence: [{ text: 'No troponin rise.' }, null, ''],
            alternative_explanations: [{ summary: 'Costochondritis' }],
            base_rate_caveat: { reason: 'Low pre-test probability.' },
          },
        })}
        sessionId="sess-1"
      />,
    );

    expect(screen.getByText('No troponin rise.')).toBeInTheDocument();
    expect(screen.getByText(/Costochondritis/)).toBeInTheDocument();
    expect(screen.getByText(/Low pre-test probability\./)).toBeInTheDocument();
  });

  it('still shows the dissent when every critique field is unreadable (Safety Rule #5)', () => {
    render(
      <SuggestionCard
        suggestion={suggestion({
          devils_advocate: { disconfirming_evidence: 7, alternative_explanations: {} },
        })}
        sessionId="sess-1"
      />,
    );

    expect(screen.getByText(/Devil's advocate/i)).toBeInTheDocument();
    // The conclusion it is attached to must still be on screen — that is what used to be lost.
    expect(screen.getByText('Community-acquired pneumonia')).toBeInTheDocument();
  });

  it('renders evidence that arrived as a string as one item, not one per character', () => {
    render(
      <SuggestionCard
        suggestion={suggestion({
          evidence: {
            evidence_for: 'Productive cough and fever for three days',
            evidence_against: [{ evidence: 'No hypoxia on exam' }, 'Afebrile at review'],
          },
        })}
        sessionId="sess-1"
      />,
    );

    expect(screen.getByText('Productive cough and fever for three days')).toBeInTheDocument();
    expect(screen.getByText('No hypoxia on exam')).toBeInTheDocument();
    expect(screen.getByText('Afebrile at review')).toBeInTheDocument();
    expect(screen.getAllByRole('listitem')).toHaveLength(3);
  });

  it('reads the alternative keys a model uses for an evidence item, and flattens the rest', () => {
    render(
      <SuggestionCard
        suggestion={suggestion({
          evidence: {
            evidence_for: [
              { finding: 'Crackles at the right base' },
              { observation: 'Respiratory rate 24', severity: 'moderate' },
            ],
            evidence_against: [],
          },
        })}
        sessionId="sess-1"
      />,
    );

    expect(screen.getByText('Crackles at the right base')).toBeInTheDocument();
    // No recognised key: the object's own values are joined rather than dropped.
    expect(screen.getByText('Respiratory rate 24; moderate')).toBeInTheDocument();
  });

  it('renders the conclusion when the evidence object is missing entirely', () => {
    render(
      <SuggestionCard
        suggestion={suggestion({ evidence: undefined as never })}
        sessionId="sess-1"
      />,
    );

    expect(screen.getByText('Community-acquired pneumonia')).toBeInTheDocument();
    expect(screen.queryByText(/Evidence supporting/)).not.toBeInTheDocument();
  });
});

// A response shaped by a backend this build has not caught up with. Every field below is read
// straight off the payload by the card, and each one threw before rendering anything.
describe('SuggestionCard given a payload from a newer backend', () => {
  beforeEach(() => {
    vi.mocked(api.recordDecision).mockReset().mockResolvedValue({ id: 'd1', decision: 'ok' });
  });

  it('does not crash on an autonomy tier it does not recognise', () => {
    expect(() =>
      render(
        <SuggestionCard
          suggestion={suggestion({ autonomy_tier: 'advisory' as never })}
          sessionId="sess-1"
        />,
      ),
    ).not.toThrow();
  });

  it.each([['advisory'], [undefined], [null], ['']] as const)(
    'gates an unrecognised tier (%s) behind the engagement click rather than showing it ungated',
    (tier) => {
      render(
        <SuggestionCard
          suggestion={suggestion({ autonomy_tier: tier as never })}
          sessionId="sess-1"
        />,
      );
      // The conservative reading wins: an unknown tier is treated as flag-for-review, so the
      // evidence and the assessment stay behind the gate until the clinician engages.
      expect(screen.getByText(/This output is flagged for review/)).toBeInTheDocument();
      expect(screen.queryByText('Assessment to consider')).not.toBeInTheDocument();
      expect(screen.queryByText('Productive cough and fever for 3 days')).not.toBeInTheDocument();
    },
  );

  it('still reveals the evidence once an unrecognised tier is engaged with', async () => {
    const user = userEvent.setup();
    render(
      <SuggestionCard
        suggestion={suggestion({ autonomy_tier: 'advisory' as never })}
        sessionId="sess-1"
      />,
    );
    await user.click(screen.getByRole('button', { name: /Review evidence/ }));
    expect(screen.getByText('Productive cough and fever for 3 days')).toBeInTheDocument();
    expect(screen.getByText('Assessment to consider')).toBeInTheDocument();
  });

  it('keeps a known non-flagged tier ungated (the fallback does not gate everything)', () => {
    render(<SuggestionCard suggestion={suggestion()} sessionId="sess-1" />);
    expect(screen.queryByText(/This output is flagged for review/)).not.toBeInTheDocument();
    expect(screen.getByText('Assessment to consider')).toBeInTheDocument();
  });

  it.each([[undefined], [null], [{ nested: 'shape' }]] as const)(
    'renders the assessment when output_type is %s',
    (outputType) => {
      render(
        <SuggestionCard
          suggestion={suggestion({ output_type: outputType as never })}
          sessionId="sess-1"
        />,
      );
      expect(screen.getByText('Community-acquired pneumonia')).toBeInTheDocument();
    },
  );

  it.each([[undefined], [null], ['not-a-list']] as const)(
    'renders the assessment when citations is %s',
    (citations) => {
      render(
        <SuggestionCard
          suggestion={suggestion({ citations: citations as never })}
          sessionId="sess-1"
        />,
      );
      expect(screen.getByText('Community-acquired pneumonia')).toBeInTheDocument();
      expect(screen.queryByText(/Guideline citations/)).not.toBeInTheDocument();
    },
  );

  it('shows a citation missing its source rather than dropping the grounding', () => {
    render(
      <SuggestionCard
        suggestion={suggestion({
          citations: [
            { document_title: 'ICMR STW — Community-acquired pneumonia' },
            { source: 'icmr', document_title: 'ICMR STW', section_id: '4.2' },
          ] as never,
        })}
        sessionId="sess-1"
      />,
    );
    expect(screen.getByText(/Guideline citations/)).toBeInTheDocument();
    expect(
      screen.getByText(/ICMR STW — Community-acquired pneumonia/),
    ).toBeInTheDocument();
    expect(screen.getByText('ICMR')).toBeInTheDocument();
    expect(screen.getByText('[4.2]')).toBeInTheDocument();
  });

  it('drops only a citation with nothing renderable in it at all', () => {
    render(
      <SuggestionCard
        suggestion={suggestion({ citations: [{}, null, { snippet: 'Consider X.' }] as never })}
        sessionId="sess-1"
      />,
    );
    expect(screen.getByText(/Consider X\./)).toBeInTheDocument();
  });

  it('does not crash on a hard block whose body is a non-conforming shape', () => {
    render(
      <SuggestionCard
        suggestion={suggestion({
          is_hard_block: true,
          autonomy_tier: 'advisory' as never,
          body: { reason: 'Documented penicillin allergy' } as never,
        })}
        sessionId="sess-1"
      />,
    );
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByText(/Documented penicillin allergy/)).toBeInTheDocument();
  });
});
