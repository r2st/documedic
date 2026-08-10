import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { IntakeQuestion } from '@/lib/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return { ...actual, api: { ...actual.api, submitIntakeAnswers: vi.fn() } };
});

import { api } from '@/lib/api';
import { IntakeFlow } from './IntakeFlow';

function question(overrides: Partial<IntakeQuestion> = {}): IntakeQuestion {
  return {
    id: 'q1',
    question_text: 'How long has the pain lasted?',
    question_type: 'clarifying',
    rationale: null,
    sequence_order: 1,
    info_gain_score: null,
    answered_at: null,
    ...overrides,
  };
}

describe('IntakeFlow', () => {
  beforeEach(() => {
    vi.mocked(api.submitIntakeAnswers).mockReset();
  });

  it('renders each pending question', () => {
    render(
      <IntakeFlow sessionId="s1" questions={[question(), question({ id: 'q2', question_text: 'Any fever?' })]} onComplete={vi.fn()} />,
    );
    expect(screen.getByText('How long has the pain lasted?')).toBeInTheDocument();
    expect(screen.getByText('Any fever?')).toBeInTheDocument();
  });

  it('submits only answered questions and calls onComplete when intake is complete', async () => {
    vi.mocked(api.submitIntakeAnswers).mockResolvedValue({
      session: {} as never,
      pending_questions: [],
      intake_complete: true,
    });
    const onComplete = vi.fn();
    render(<IntakeFlow sessionId="s1" questions={[question()]} onComplete={onComplete} />);

    await userEvent.type(screen.getByPlaceholderText(/Your answer/), '3 days');
    await userEvent.click(screen.getByRole('button', { name: 'Submit answers' }));

    expect(api.submitIntakeAnswers).toHaveBeenCalledWith('s1', [
      { question_id: 'q1', answer_text: '3 days' },
    ]);
    expect(onComplete).toHaveBeenCalledOnce();
  });

  it('replaces the question list with the next batch when intake is not yet complete', async () => {
    const nextQuestion = question({ id: 'q2', question_text: 'Any radiation of the pain?' });
    vi.mocked(api.submitIntakeAnswers).mockResolvedValue({
      session: {} as never,
      pending_questions: [nextQuestion],
      intake_complete: false,
    });
    const onComplete = vi.fn();
    render(<IntakeFlow sessionId="s1" questions={[question()]} onComplete={onComplete} />);

    await userEvent.type(screen.getByPlaceholderText(/Your answer/), '3 days');
    await userEvent.click(screen.getByRole('button', { name: 'Submit answers' }));

    expect(onComplete).not.toHaveBeenCalled();
    expect(screen.getByText('Any radiation of the pain?')).toBeInTheDocument();
    expect(screen.queryByText('How long has the pain lasted?')).not.toBeInTheDocument();
  });

  it('lets the clinician skip straight to reasoning without submitting', async () => {
    const onComplete = vi.fn();
    render(<IntakeFlow sessionId="s1" questions={[question()]} onComplete={onComplete} />);
    await userEvent.click(screen.getByRole('button', { name: 'Skip — proceed to reasoning' }));
    expect(onComplete).toHaveBeenCalledOnce();
    expect(api.submitIntakeAnswers).not.toHaveBeenCalled();
  });
});

describe('IntakeFlow question rendering', () => {
  beforeEach(() => {
    vi.mocked(api.submitIntakeAnswers).mockReset();
  });

  it('styles an unrecognised question type with the neutral badge instead of dropping it', () => {
    // Question types come from the Triage agent, so the UI must not assume a closed set.
    render(
      <IntakeFlow
        sessionId="s1"
        questions={[question({ question_type: 'social_history' })]}
        onComplete={vi.fn()}
      />,
    );

    const badge = screen.getByText('social history');
    expect(badge).toBeInTheDocument();
    expect(badge.className).toContain('bg-slate-50');
  });

  it('shows the agent rationale when one is given', () => {
    render(
      <IntakeFlow
        sessionId="s1"
        questions={[question({ rationale: 'Duration separates acute from chronic causes.' })]}
        onComplete={vi.fn()}
      />,
    );
    expect(
      screen.getByText('Duration separates acute from chronic causes.'),
    ).toBeInTheDocument();
  });

  it('renders the question alone when the agent gave no rationale', () => {
    // `questions` seeds component state, so the two cases need separate mounts.
    const { container } = render(
      <IntakeFlow sessionId="s1" questions={[question({ rationale: null })]} onComplete={vi.fn()} />,
    );

    expect(screen.getByText('How long has the pain lasted?')).toBeInTheDocument();
    expect(container.querySelector('p.italic')).toBeNull();
  });

  it('submits an empty payload when every answer was left blank or whitespace', async () => {
    vi.mocked(api.submitIntakeAnswers).mockResolvedValue({
      session: {} as never,
      pending_questions: [],
      intake_complete: true,
    });
    const user = userEvent.setup();
    render(
      <IntakeFlow sessionId="s1" questions={[question()]} onComplete={vi.fn()} />,
    );

    await user.type(screen.getByPlaceholderText(/Your answer/), '   ');
    await user.click(screen.getByRole('button', { name: /Submit/i }));

    expect(api.submitIntakeAnswers).toHaveBeenCalledWith('s1', []);
  });
});

describe('IntakeFlow partial answers', () => {
  beforeEach(() => {
    vi.mocked(api.submitIntakeAnswers).mockReset();
  });

  it('omits a question the clinician never touched from the payload', async () => {
    vi.mocked(api.submitIntakeAnswers).mockResolvedValue({
      session: {} as never,
      pending_questions: [],
      intake_complete: true,
    });
    const user = userEvent.setup();
    render(
      <IntakeFlow
        sessionId="s1"
        questions={[question(), question({ id: 'q2', question_text: 'Any fever?' })]}
        onComplete={vi.fn()}
      />,
    );

    const [first] = screen.getAllByPlaceholderText(/Your answer/);
    await user.type(first, '3 days');
    await user.click(screen.getByRole('button', { name: 'Submit answers' }));

    // q2 was never given a value at all, so it has no entry in the answer map.
    expect(api.submitIntakeAnswers).toHaveBeenCalledWith('s1', [
      { question_id: 'q1', answer_text: '3 days' },
    ]);
  });
});
