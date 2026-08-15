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
import { clearDrafts, readDraft } from '@/lib/drafts';
import EncounterPage from './page';

// The complaint field now writes to a module-scoped draft store (lib/drafts.ts), which by design
// outlives an unmount. That lifetime is longer than a test, so every test in this file starts
// from an empty store — otherwise one test's typing pre-fills the next one's form.
beforeEach(clearDrafts);

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
      run_in_progress: false,
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

  it('lifts a flag-for-review management option above the differentials it was buried under', async () => {
    vi.mocked(api.startReasoning).mockResolvedValue(
      intakeState({ intake_complete: true, pending_questions: [] }),
    );
    // The shape the synthesis agent emits for a guideline option that conflicts with this
    // patient's record without hard-blocking: escalated tier, retitled, not a hard block.
    // Arrives in the order the synthesis agent builds it: differentials, then management last.
    vi.mocked(api.listSuggestions).mockResolvedValue([
      suggestion({ id: 's1', title: 'Leading differential', output_type: 'differential' }),
      suggestion({ id: 's2', title: 'Second differential', output_type: 'differential' }),
      suggestion({
        id: 's3',
        title: 'Conflicting management option',
        output_type: 'management',
        autonomy_tier: 'flag_for_review',
      }),
    ]);
    const user = await enterComplaint();
    await user.click(await screen.findByRole('button', { name: 'finish reasoning' }));

    const rendered = (await screen.findAllByRole('listitem')).slice(4);
    expect(rendered.map((li) => li.textContent?.replace(/Review evidence.*/, '').trim())).toEqual([
      'Conflicting management option',
      'Leading differential',
      'Second differential',
    ]);
  });

  it('keeps a hard block and a cant-miss above a flag-for-review output', async () => {
    vi.mocked(api.startReasoning).mockResolvedValue(
      intakeState({ intake_complete: true, pending_questions: [] }),
    );
    vi.mocked(api.listSuggestions).mockResolvedValue([
      suggestion({
        id: 's3',
        title: 'Flagged management option',
        output_type: 'management',
        autonomy_tier: 'flag_for_review',
      }),
      suggestion({
        id: 's2',
        title: 'Aortic dissection',
        output_type: 'cant_miss',
        cant_miss_flag: true,
        autonomy_tier: 'flag_for_review',
      }),
      suggestion({
        id: 's1',
        title: 'Allergy hard block',
        output_type: 'safety',
        is_hard_block: true,
        autonomy_tier: 'flag_for_review',
      }),
    ]);
    const user = await enterComplaint();
    await user.click(await screen.findByRole('button', { name: 'finish reasoning' }));

    const rendered = (await screen.findAllByRole('listitem')).slice(4);
    const titles = rendered.map((li) => li.textContent ?? '');
    expect(titles[0]).toContain('Allergy hard block');
    expect(titles[1]).toContain('Aortic dissection');
    expect(titles[2]).toContain('Flagged management option');
  });

  it('sorts an output_type this build does not recognise last rather than dropping it', async () => {
    vi.mocked(api.startReasoning).mockResolvedValue(
      intakeState({ intake_complete: true, pending_questions: [] }),
    );
    vi.mocked(api.listSuggestions).mockResolvedValue([
      suggestion({ id: 's2', title: 'Novel output', output_type: 'prognosis' as never }),
      suggestion({ id: 's1', title: 'Leading differential', output_type: 'differential' }),
    ]);
    const user = await enterComplaint();
    await user.click(await screen.findByRole('button', { name: 'finish reasoning' }));

    const rendered = (await screen.findAllByRole('listitem')).slice(4);
    expect(rendered.map((li) => li.textContent)).toEqual(['Leading differential', 'Novel output']);
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

describe('EncounterPage results loading', () => {
  beforeEach(() => {
    vi.mocked(api.startReasoning).mockReset().mockResolvedValue(intakeState());
    vi.mocked(api.listSuggestions).mockReset();
  });

  it('names the wait between the run finishing and the cards arriving', async () => {
    // A finished run with no cards on screen is indistinguishable from a run that produced
    // nothing, which is the automation-bias failure in reverse.
    let release: ((value: ClinicalSuggestion[]) => void) | undefined;
    vi.mocked(api.listSuggestions).mockReturnValue(
      new Promise((resolve) => {
        release = resolve;
      }),
    );
    const user = await enterComplaint();

    await user.click(await screen.findByRole('button', { name: 'finish intake' }));
    await user.click(await screen.findByRole('button', { name: 'finish reasoning' }));

    const block = await screen.findByRole('status', { name: 'Fetching the reasoning results' });
    expect(block).toHaveAttribute('aria-busy', 'true');
    // Still on the reasoning step: the results heading must not appear before the results do.
    expect(screen.queryByRole('heading', { name: 'Results' })).not.toBeInTheDocument();

    release?.([suggestion()]);
    expect(await screen.findByRole('heading', { name: 'Results' })).toBeInTheDocument();
    expect(
      screen.queryByRole('status', { name: 'Fetching the reasoning results' }),
    ).not.toBeInTheDocument();
  });

  it('clears the placeholder when the results cannot be read', async () => {
    vi.mocked(api.listSuggestions).mockRejectedValue(new TypeError('Failed to fetch'));
    const user = await enterComplaint();

    await user.click(await screen.findByRole('button', { name: 'finish intake' }));
    await user.click(await screen.findByRole('button', { name: 'finish reasoning' }));

    await screen.findByRole('alert');
    expect(
      screen.queryByRole('status', { name: 'Fetching the reasoning results' }),
    ).not.toBeInTheDocument();
    // The retry is still the one that re-reads rather than re-running the agents.
    expect(screen.getByRole('button', { name: 'Fetch results again' })).toBeInTheDocument();
  });

  it('shows no placeholder while the agents are still deliberating', async () => {
    vi.mocked(api.listSuggestions).mockResolvedValue([]);
    const user = await enterComplaint();

    await user.click(await screen.findByRole('button', { name: 'finish intake' }));
    await screen.findByText('theatre:sess-1');

    expect(
      screen.queryByRole('status', { name: 'Fetching the reasoning results' }),
    ).not.toBeInTheDocument();
  });
});

/**
 * The presenting complaint is the one field on this screen that can be lost.
 *
 * It is composed at length — a paragraph of history — and it is not sent anywhere until "Begin
 * intake". Everything else on the screen either belongs to the server already (the intake
 * answers, once submitted; the suggestions) or is derived from it. So a session that ends while
 * the clinician is away from the keyboard, a navigation to the chart to check a lab value, or
 * any remount at all took the note with it.
 *
 * These tests are about the draft store closing that (see lib/drafts.ts), and about not closing
 * it too eagerly: a complaint the server has accepted must not come back the next time the
 * screen opens, or the next encounter starts pre-filled with the last one's history.
 */
describe('the presenting complaint survives losing the screen', () => {
  const NOTE = '54-year-old with central chest pain radiating to the left arm for 1 hour';
  const DRAFT_KEY = 'encounter:pat-1:complaint';

  beforeEach(() => {
    vi.mocked(api.startReasoning)
      .mockReset()
      .mockResolvedValue(intakeState({ intake_complete: true, pending_questions: [] }));
    vi.mocked(api.listSuggestions).mockReset().mockResolvedValue([]);
  });

  it('keeps what was typed when the screen is unmounted and opened again', async () => {
    const user = userEvent.setup();
    const first = render(<EncounterPage />);
    await user.type(screen.getByLabelText('Presenting complaint'), NOTE);

    // Standing in for everything that unmounts this screen: an idle sign-out, a route change to
    // check the chart, a re-login.
    first.unmount();
    render(<EncounterPage />);

    expect(screen.getByLabelText('Presenting complaint')).toHaveValue(NOTE);
  });

  it('forgets it once the server has it', async () => {
    const user = userEvent.setup();
    render(<EncounterPage />);
    await user.type(screen.getByLabelText('Presenting complaint'), NOTE);

    await user.click(screen.getByRole('button', { name: 'Begin intake' }));

    await waitFor(() => expect(api.startReasoning).toHaveBeenCalledWith('pat-1', NOTE));
    expect(readDraft(DRAFT_KEY)).toBe('');
  });

  it('keeps it when the run fails to start', async () => {
    // The retry and the timeout want the same thing: the text still there. A draft discarded on
    // the way out rather than on success would clear it on exactly the failure that makes the
    // clinician need it.
    const user = userEvent.setup();
    vi.mocked(api.startReasoning).mockRejectedValue(
      new ApiError(503, 'service_unavailable', 'Briefly unavailable.'),
    );
    render(<EncounterPage />);
    await user.type(screen.getByLabelText('Presenting complaint'), NOTE);

    await user.click(screen.getByRole('button', { name: 'Begin intake' }));

    await screen.findByRole('alert');
    expect(readDraft(DRAFT_KEY)).toBe(NOTE);
    expect(screen.getByLabelText('Presenting complaint')).toHaveValue(NOTE);
  });

  it('does not carry one chart’s history into another', async () => {
    // The draft key is namespaced by patient. A complaint restored onto the wrong chart would be
    // worse than a lost one.
    const user = userEvent.setup();
    render(<EncounterPage />);
    await user.type(screen.getByLabelText('Presenting complaint'), NOTE);

    expect(readDraft('encounter:pat-2:complaint')).toBe('');
  });

  it('starts empty when nothing was ever typed', () => {
    render(<EncounterPage />);
    expect(screen.getByLabelText('Presenting complaint')).toHaveValue('');
  });
});
