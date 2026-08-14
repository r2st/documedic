import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ClinicalSuggestion, IntakeQuestion, IntakeState } from '@/lib/types';

vi.mock('next/navigation', () => ({ useParams: () => ({ id: 'pat-1' }) }));

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    api: { ...actual.api, startReasoning: vi.fn(), listSuggestions: vi.fn() },
  };
});

// The intake and theatre widgets have their own suites; here they are reduced to their
// contract with the page — an id in, an onComplete out.
vi.mock('@/components/reasoning/IntakeFlow', () => ({
  IntakeFlow: ({
    sessionId,
    questions,
    onComplete,
  }: {
    sessionId: string;
    questions: IntakeQuestion[];
    onComplete: () => void;
  }) => (
    <div>
      <p>intake:{sessionId}</p>
      <p>questions:{questions.length}</p>
      <button onClick={onComplete}>finish intake</button>
    </div>
  ),
}));
vi.mock('@/components/reasoning/ReasoningTheatre', () => ({
  ReasoningTheatre: ({ sessionId, onComplete }: { sessionId: string; onComplete: () => void }) => (
    <div>
      <p>theatre:{sessionId}</p>
      <button onClick={onComplete}>finish reasoning</button>
    </div>
  ),
}));
/** Title that makes the mocked SuggestionCard below throw during render. */
const CRASHING_TITLE = 'crash-this-card';

vi.mock('@/components/reasoning/SuggestionCard', () => ({
  SuggestionCard: ({ suggestion }: { suggestion: ClinicalSuggestion }) => {
    // Throws on a sentinel title so the error-boundary test can model the real failure — one
    // malformed suggestion among several — rather than handing the page a payload so broken
    // it crashes in the sort above the boundary instead of inside it.
    if (suggestion.title === CRASHING_TITLE) throw new Error('malformed suggestion payload');
    return <li>{suggestion.title}</li>;
  },
}));

import { ApiError, api } from '@/lib/api';
import EncounterPage from './page';

function question(id: string): IntakeQuestion {
  return {
    id,
    question_text: `Question ${id}`,
    question_type: 'free_text',
    rationale: null,
    sequence_order: 1,
    info_gain_score: 0.4,
    answered_at: null,
  };
}

function intakeState(overrides: Partial<IntakeState> = {}): IntakeState {
  return {
    session: {
      id: 'sess-1',
      patient_id: 'pat-1',
      presenting_complaint: 'chest pain',
      status: 'intake',
      autonomy_tier: null,
      intake_complete: false,
      info_gain_score: null,
      online: true,
      started_at: null,
      completed_at: null,
      created_at: '2026-01-01T00:00:00Z',
    },
    pending_questions: [question('q1')],
    intake_complete: false,
    ...overrides,
  };
}

function suggestion(overrides: Partial<ClinicalSuggestion> = {}): ClinicalSuggestion {
  return {
    id: 'sug-1',
    session_id: 'sess-1',
    patient_id: 'pat-1',
    output_type: 'differential',
    autonomy_tier: 'suggestive',
    confidence_band: 'moderate',
    title: 'Differential',
    body: null,
    evidence: {},
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

async function enterComplaint(text = 'Central chest pain for 1 hour') {
  const user = userEvent.setup();
  render(<EncounterPage />);
  await user.type(screen.getByLabelText('Presenting complaint'), text);
  await user.click(screen.getByRole('button', { name: 'Begin intake' }));
  return user;
}

describe('EncounterPage', () => {
  beforeEach(() => {
    vi.mocked(api.startReasoning).mockReset().mockResolvedValue(intakeState());
    vi.mocked(api.listSuggestions).mockReset().mockResolvedValue([]);
  });

  it('starts on the complaint step with the later steps not yet active', () => {
    render(<EncounterPage />);
    expect(screen.getByLabelText('Presenting complaint')).toBeInTheDocument();
    expect(screen.getByRole('navigation', { name: 'Progress' })).toBeInTheDocument();
    expect(screen.queryByText(/^theatre:/)).not.toBeInTheDocument();
  });

  it('refuses to start on a complaint too short to reason about', async () => {
    const user = userEvent.setup();
    render(<EncounterPage />);

    await user.type(screen.getByLabelText('Presenting complaint'), 'ab');
    expect(screen.getByRole('button', { name: 'Begin intake' })).toBeDisabled();

    await user.type(screen.getByLabelText('Presenting complaint'), 'c');
    expect(screen.getByRole('button', { name: 'Begin intake' })).toBeEnabled();
  });

  it('trims the complaint before sending it to the reasoning engine', async () => {
    await enterComplaint('  chest pain  ');
    await waitFor(() => expect(api.startReasoning).toHaveBeenCalledWith('pat-1', 'chest pain'));
  });

  it('routes to intake when the triage agent has clarifying questions', async () => {
    await enterComplaint();
    expect(await screen.findByText('intake:sess-1')).toBeInTheDocument();
    expect(screen.getByText('questions:1')).toBeInTheDocument();
  });

  it('skips intake when the triage agent already has enough information', async () => {
    vi.mocked(api.startReasoning).mockResolvedValue(
      intakeState({ intake_complete: true, pending_questions: [] }),
    );
    await enterComplaint();
    expect(await screen.findByText('theatre:sess-1')).toBeInTheDocument();
    expect(screen.queryByText('intake:sess-1')).not.toBeInTheDocument();
  });

  it('skips intake when no questions were generated even if the flag is unset', async () => {
    vi.mocked(api.startReasoning).mockResolvedValue(intakeState({ pending_questions: [] }));
    await enterComplaint();
    expect(await screen.findByText('theatre:sess-1')).toBeInTheDocument();
  });

  it("advances intake → reasoning → results and loads that session's suggestions", async () => {
    vi.mocked(api.listSuggestions).mockResolvedValue([suggestion()]);
    const user = await enterComplaint();

    await user.click(await screen.findByRole('button', { name: 'finish intake' }));
    expect(await screen.findByText('theatre:sess-1')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'finish reasoning' }));
    expect(await screen.findByRole('heading', { name: 'Results' })).toBeInTheDocument();
    expect(api.listSuggestions).toHaveBeenCalledWith('sess-1');
  });

  it('keeps the anti-automation-bias framing visible during reasoning', async () => {
    vi.mocked(api.startReasoning).mockResolvedValue(
      intakeState({ intake_complete: true, pending_questions: [] }),
    );
    await enterComplaint();
    expect(
      await screen.findByText(/Evidence and dissent are shown before conclusions/),
    ).toBeInTheDocument();
  });

  it('states in the results header that the clinician remains the decision-maker', async () => {
    vi.mocked(api.startReasoning).mockResolvedValue(
      intakeState({ intake_complete: true, pending_questions: [] }),
    );
    const user = await enterComplaint();
    await user.click(await screen.findByRole('button', { name: 'finish reasoning' }));

    expect(await screen.findByText(/The clinician is the decision-maker/)).toBeInTheDocument();
  });

  it("surfaces hard blocks first, then can't-miss, then differentials, investigations, management", async () => {
    vi.mocked(api.startReasoning).mockResolvedValue(
      intakeState({ intake_complete: true, pending_questions: [] }),
    );
    vi.mocked(api.listSuggestions).mockResolvedValue([
      suggestion({ id: 's5', title: 'Management option', output_type: 'management' }),
      suggestion({ id: 's4', title: 'Next test', output_type: 'investigation' }),
      suggestion({ id: 's3', title: 'Leading differential', output_type: 'differential' }),
      suggestion({
        id: 's2',
        title: 'Aortic dissection',
        output_type: 'cant_miss',
        cant_miss_flag: true,
      }),
      suggestion({
        id: 's1',
        title: 'Allergy hard block',
        output_type: 'safety',
        is_hard_block: true,
      }),
      suggestion({ id: 's6', title: 'Case summary', output_type: 'summary' }),
    ]);
    const user = await enterComplaint();
    await user.click(await screen.findByRole('button', { name: 'finish reasoning' }));

    // Drop the four step-indicator list items that precede the results list.
    const rendered = (await screen.findAllByRole('listitem')).slice(4);
    expect(rendered.map((li) => li.textContent)).toEqual([
      'Allergy hard block',
      'Aortic dissection',
      'Leading differential',
      'Next test',
      'Management option',
      'Case summary',
    ]);
  });

  it("surfaces the API's own message and stays on the complaint step", async () => {
    vi.mocked(api.startReasoning).mockRejectedValue(
      new ApiError(422, 'validation_error', 'A presenting complaint is required.'),
    );
    await enterComplaint();

    expect(await screen.findByText('A presenting complaint is required.')).toBeInTheDocument();
    expect(screen.getByLabelText('Presenting complaint')).toBeInTheDocument();
  });

  it('reports an unreachable server as unreachable, not as a rejected run', async () => {
    vi.mocked(api.startReasoning).mockRejectedValue(new TypeError('Failed to fetch'));
    await enterComplaint();

    expect(
      await screen.findByText(/Could not reach the server, so the reasoning run/),
    ).toBeInTheDocument();
    expect(screen.getByLabelText('Presenting complaint')).toBeInTheDocument();
  });

  it('handles a thrown value that is not an Error at all', async () => {
    vi.mocked(api.startReasoning).mockRejectedValue('boom');
    await enterComplaint();
    expect(await screen.findByText(/Could not reach the server/)).toBeInTheDocument();
  });

  it('links back to the patient record', () => {
    render(<EncounterPage />);
    expect(screen.getByRole('link', { name: /Back to patient/ })).toHaveAttribute(
      'href',
      '/patients/pat-1',
    );
  });
});

describe('EncounterPage when the results cannot be fetched', () => {
  beforeEach(() => {
    vi.mocked(api.startReasoning)
      .mockReset()
      .mockResolvedValue(intakeState({ intake_complete: true, pending_questions: [] }));
    vi.mocked(api.listSuggestions).mockReset();
  });

  /** Drives the page to the point where the theatre reports the run has finished. */
  async function runToCompletion() {
    const user = userEvent.setup();
    render(<EncounterPage />);
    await user.type(screen.getByLabelText(/Presenting complaint/i), 'chest pain for one hour');
    await user.click(screen.getByRole('button', { name: 'Begin intake' }));
    await screen.findByText('theatre:sess-1');
    await user.click(screen.getByRole('button', { name: 'finish reasoning' }));
    return user;
  }

  it('says the results could not be read instead of stalling silently', async () => {
    // Previously this rejection was unhandled: the screen simply stayed on the reasoning
    // phase after the run had finished. Eight agents had done their work and their output was
    // sitting on the server, with nothing on screen saying so or offering to fetch it.
    vi.mocked(api.listSuggestions).mockRejectedValue(
      new ApiError(503, 'unavailable', 'Briefly unavailable.'),
    );

    await runToCompletion();

    expect(await screen.findByRole('alert')).toHaveTextContent('Briefly unavailable.');
  });

  it('offers a retry that re-reads rather than re-running the agents', async () => {
    // The label is the point. A clinician who has just watched eight agents deliberate needs
    // to know the button does not put them through it a second time.
    vi.mocked(api.listSuggestions)
      .mockRejectedValueOnce(new ApiError(503, 'unavailable', 'Briefly unavailable.'))
      .mockResolvedValue([suggestion()]);

    const user = await runToCompletion();
    await screen.findByRole('alert');

    await user.click(screen.getByRole('button', { name: 'Fetch results again' }));

    expect(await screen.findByText('Differential')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    // Re-read only: the run was never started again.
    expect(api.startReasoning).toHaveBeenCalledOnce();
  });

  it('shows no results heading while the fetch is failing', async () => {
    // Staying on the reasoning phase is right — there is nothing to show — but it now says
    // why, rather than looking like a run that never finished.
    vi.mocked(api.listSuggestions).mockRejectedValue(
      new ApiError(503, 'unavailable', 'Briefly unavailable.'),
    );

    await runToCompletion();
    await screen.findByRole('alert');

    expect(screen.queryByRole('heading', { name: 'Results' })).not.toBeInTheDocument();
  });
});

describe('EncounterPage retry on the opening step', () => {
  beforeEach(() => {
    vi.mocked(api.startReasoning).mockReset();
    vi.mocked(api.listSuggestions).mockReset().mockResolvedValue([]);
  });

  it('offers a retry when the run could not be started', async () => {
    vi.mocked(api.startReasoning)
      .mockRejectedValueOnce(new ApiError(503, 'unavailable', 'Briefly unavailable.'))
      .mockResolvedValue(intakeState());

    const user = userEvent.setup();
    render(<EncounterPage />);
    await user.type(screen.getByLabelText(/Presenting complaint/i), 'chest pain for one hour');
    await user.click(screen.getByRole('button', { name: 'Begin intake' }));
    await screen.findByRole('alert');

    await user.click(screen.getByRole('button', { name: 'Try again' }));

    expect(await screen.findByText('intake:sess-1')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('leaves an intake-phase error without a retry, since the retry belongs to the step', async () => {
    // On 'intake' the failing action is IntakeFlow's own submit, which owns its retry. A
    // second button here would re-run whichever step this page happened to know about.
    vi.mocked(api.startReasoning).mockResolvedValue(intakeState());
    vi.mocked(api.listSuggestions).mockRejectedValue(
      new ApiError(503, 'unavailable', 'Briefly unavailable.'),
    );

    const user = userEvent.setup();
    render(<EncounterPage />);
    await user.type(screen.getByLabelText(/Presenting complaint/i), 'chest pain for one hour');
    await user.click(screen.getByRole('button', { name: 'Begin intake' }));
    await screen.findByText('intake:sess-1');

    // Straight to results without passing through 'reasoning' — the failure lands in a phase
    // that has no page-level retry to offer.
    await user.click(screen.getByRole('button', { name: 'finish intake' }));
    await user.click(screen.getByRole('button', { name: 'finish reasoning' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Briefly unavailable.');
  });
});

describe('EncounterPage results error boundary', () => {
  it('reports crashed results as missing rather than rendering a short list', async () => {
    // The boundary that matters most in this app. These cards render agent output, so their
    // shape is the least predictable thing here — and what a crash would hide is a can't-miss
    // flag or a hard block. Silently showing fewer cards than the Verifier passed is the
    // automation-bias failure rule #5 exists to prevent, arrived at by accident.
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.mocked(api.startReasoning)
      .mockReset()
      .mockResolvedValue(intakeState({ intake_complete: true, pending_questions: [] }));
    vi.mocked(api.listSuggestions)
      .mockReset()
      .mockResolvedValue([
        suggestion({ id: 'sug-1', title: 'Dengue fever', cant_miss_flag: true }),
        suggestion({ id: 'sug-2', title: CRASHING_TITLE }),
      ]);

    const user = userEvent.setup();
    render(<EncounterPage />);
    await user.type(screen.getByLabelText(/Presenting complaint/i), 'chest pain for one hour');
    await user.click(screen.getByRole('button', { name: 'Begin intake' }));
    await screen.findByText('theatre:sess-1');
    await user.click(screen.getByRole('button', { name: 'finish reasoning' }));

    expect(
      await screen.findByText(/The reasoning results could not be displayed/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/treat this section as missing, not as empty/i)).toBeInTheDocument();
    // The card that rendered fine is gone too — React unmounts the whole boundary subtree —
    // which is exactly why the fallback has to say the section is missing. A list showing
    // only "Dengue fever", with the failure invisible, would be the dangerous outcome.
    expect(screen.queryByText('Dengue fever')).not.toBeInTheDocument();

    consoleError.mockRestore();
  });
});

describe('EncounterPage when the results boundary’s own retry fails', () => {
  it('states the failure without a second retry beside the boundary’s', async () => {
    // The one path that reaches the page's plain, retry-less banner: the results section
    // crashed, the boundary offered "Try again", its onReset re-fetched, and the re-fetch
    // failed. The boundary's own button is still there, so a second one alongside it would
    // just be two controls for the same action.
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.mocked(api.startReasoning)
      .mockReset()
      .mockResolvedValue(intakeState({ intake_complete: true, pending_questions: [] }));
    vi.mocked(api.listSuggestions)
      .mockReset()
      .mockResolvedValueOnce([suggestion({ title: CRASHING_TITLE })])
      .mockRejectedValue(new ApiError(503, 'unavailable', 'Briefly unavailable.'));

    const user = userEvent.setup();
    render(<EncounterPage />);
    await user.type(screen.getByLabelText(/Presenting complaint/i), 'chest pain for one hour');
    await user.click(screen.getByRole('button', { name: 'Begin intake' }));
    await screen.findByText('theatre:sess-1');
    await user.click(screen.getByRole('button', { name: 'finish reasoning' }));
    await screen.findByText(/The reasoning results could not be displayed/i);

    await user.click(screen.getByRole('button', { name: 'Try again' }));

    const banner = await screen.findByText('Briefly unavailable.');
    expect(banner).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Fetch results again' })).not.toBeInTheDocument();

    consoleError.mockRestore();
  });
});
