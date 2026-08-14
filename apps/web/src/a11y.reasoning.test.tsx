/**
 * Accessibility contracts for the reasoning and clinical-display surface.
 *
 * `a11y.test.tsx` states these properties for the screens a clinician types into. This file
 * states them for the screens a clinician *reads* — the Reasoning Theatre, suggestion cards,
 * the timeline, the connectivity banner and the dashboard shell. Those pages were built around
 * icons, colour and content that streams in on its own, which is exactly where the same four
 * rules are easiest to miss:
 *
 *   * every interactive control has an accessible name — a placeholder is not one
 *   * anything that arrives without focus moving is announced, politely or (for a can't-miss
 *     condition or a hard block) assertively
 *   * meaning carried by colour or shape alone is also carried by text
 *   * decorative iconography is hidden from the accessibility tree rather than read aloud
 *
 * The last one matters more here than the count of icons suggests: the reasoning screens are
 * roughly one icon per line of real content, so an unhidden set turns a verdict into a recital
 * of unlabelled graphics.
 */

import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Account, ClinicalSuggestion, LongitudinalRecord } from '@/lib/types';

let pathname = '/patients';
vi.mock('next/navigation', () => ({
  usePathname: () => pathname,
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), refresh: vi.fn(), back: vi.fn() }),
}));

const authState: { account: Account | null; loading: boolean } = {
  account: {
    id: 'acc-1',
    email: 'jane@clinic.in',
    display_name: 'Dr. Jane Smith',
    created_at: '2026-01-01T00:00:00Z',
  },
  loading: false,
};
vi.mock('@/lib/auth', () => ({
  useAuth: () => ({ ...authState, logout: vi.fn(), refresh: vi.fn() }),
  useRequireAuth: () => authState,
}));

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    api: { ...actual.api, reasoningStreamUrl: vi.fn(), recordDecision: vi.fn() },
  };
});

import { api } from '@/lib/api';
import DashboardLayout from '@/app/(dashboard)/layout';
import { OfflineBanner } from '@/components/Banners';
import { Timeline } from '@/components/patient/Timeline';
import { AutonomyBadge } from '@/components/reasoning/badges';
import { ReasoningTheatre } from '@/components/reasoning/ReasoningTheatre';
import { SuggestionCard } from '@/components/reasoning/SuggestionCard';

// ------------------------------------------------------------------ stream test double

type Listener = (event: { data: string }) => void;

class MockEventSource {
  static instances: MockEventSource[] = [];
  listeners: Record<string, Listener[]> = {};
  onerror: (() => void) | null = null;

  constructor(public url: string) {
    MockEventSource.instances.push(this);
  }

  addEventListener(name: string, cb: Listener) {
    (this.listeners[name] ??= []).push(cb);
  }

  close() {}

  emit(name: string, data: unknown) {
    for (const cb of this.listeners[name] ?? []) cb({ data: JSON.stringify(data) });
  }
}

/** The stream opens only after the session-scoped token round trip resolves. */
async function latestSource(): Promise<MockEventSource> {
  await waitFor(() => expect(MockEventSource.instances.length).toBeGreaterThan(0));
  return MockEventSource.instances.at(-1) as MockEventSource;
}

beforeEach(() => {
  pathname = '/patients';
  MockEventSource.instances = [];
  vi.stubGlobal('EventSource', MockEventSource);
  vi.mocked(api.reasoningStreamUrl).mockReset().mockResolvedValue('http://api/stream?token=t');
  vi.mocked(api.recordDecision).mockReset().mockResolvedValue(undefined as never);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

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
  } as ClinicalSuggestion;
}

/**
 * Every `<svg>` must be out of the accessibility tree — either hidden itself or sitting under
 * something hidden, since `aria-hidden` on an ancestor removes the whole subtree. An icon that
 * genuinely carries meaning is exempt, but it has to say so with a label.
 */
function expectNoUnhiddenIcons(container: HTMLElement) {
  const hiddenBySelfOrAncestor = (el: Element): boolean =>
    el.closest('[aria-hidden="true"]') !== null;

  const exposed = Array.from(container.querySelectorAll('svg')).filter(
    (svg) => !hiddenBySelfOrAncestor(svg) && !svg.getAttribute('aria-label'),
  );
  expect(exposed.map((s) => s.outerHTML.slice(0, 80))).toEqual([]);
}

// --------------------------------------------------------------- accessible names

describe('every interactive control on the reasoning surface has an accessible name', () => {
  it('labels the hard-block override reason, which otherwise has only a placeholder', () => {
    render(<SuggestionCard suggestion={suggestion({ is_hard_block: true })} sessionId="s1" />);

    const field = screen.getByLabelText(/reason for overriding this hard block/i);
    expect(field.tagName).toBe('TEXTAREA');
  });

  it('says why the override button is disabled rather than leaving it silently dimmed', async () => {
    render(<SuggestionCard suggestion={suggestion({ is_hard_block: true })} sessionId="s1" />);

    const override = screen.getByRole('button', { name: /override with documented reason/i });
    expect(override).toBeDisabled();
    expect(override).toHaveAccessibleDescription(/enter a reason above/i);

    await userEvent.type(
      screen.getByLabelText(/reason for overriding this hard block/i),
      'Benefit outweighs risk; patient monitored.',
    );

    expect(override).toBeEnabled();
    expect(override).toHaveAccessibleDescription(/a reason is recorded/i);
  });
});

// --------------------------------------------------------------- announcements

describe('what arrives on its own is announced', () => {
  it('reports run progress through a live region rather than a colour change', async () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    const source = await latestSource();

    expect(screen.getByRole('status')).toHaveTextContent(/reasoning/i);

    act(() => source.emit('done', {}));
    expect(screen.getByRole('status')).toHaveTextContent(/complete/i);
  });

  it('spells out each agent lane’s state, which is otherwise a coloured dot', async () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    const source = await latestSource();

    act(() => source.emit('reasoning_start', { sequence: ['hypothesis_panel'] }));
    const lane = screen.getByText('hypothesis panel').closest('li') as HTMLElement;
    expect(lane).toHaveTextContent(/waiting/i);

    act(() => source.emit('agent_start', { agent: 'hypothesis_panel' }));
    expect(lane).toHaveTextContent(/running/i);

    act(() => source.emit('agent_complete', { agent: 'hypothesis_panel' }));
    expect(lane).toHaveTextContent(/done/i);
  });

  it('interrupts with can’t-miss conditions instead of queueing them politely', async () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    const source = await latestSource();

    act(() =>
      source.emit('cant_miss', {
        items: [{ diagnosis_name: 'Acute coronary syndrome', why: 'Chest pain with risk factors' }],
      }),
    );

    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('Acute coronary syndrome');
  });

  it('delivers the verifier verdict into a polite live region', async () => {
    const { container } = render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    const source = await latestSource();

    act(() => source.emit('verifier', { status: 'approved', autonomy_tier: 'flag_for_review' }));

    const verdict = screen.getByText('Verifier verdict');
    expect(container.querySelector('[aria-live="polite"]')).toContainElement(verdict);
  });

  it('announces a stream failure as an alert', async () => {
    render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    const source = await latestSource();

    act(() => source.emit('error', { message: 'The reasoning stream dropped.' }));

    // Previously a bare red div: visible, and completely silent to a screen reader.
    expect(screen.getByRole('alert')).toHaveTextContent('The reasoning stream dropped.');
  });

  it('announces a hard block, which stops the clinician from proceeding', () => {
    render(<SuggestionCard suggestion={suggestion({ is_hard_block: true })} sessionId="s1" />);

    expect(screen.getByRole('alert')).toHaveTextContent(/hard block/i);
  });

  it('confirms a recorded decision without moving focus off the button that caused it', async () => {
    render(<SuggestionCard suggestion={suggestion({ is_hard_block: true })} sessionId="s1" />);

    await userEvent.click(screen.getByRole('button', { name: /acknowledge/i }));

    expect(await screen.findByRole('status')).toHaveTextContent('Recorded decision: acknowledged');
  });

  it('keeps the offline region mounted so the message it later carries is announced', () => {
    Object.defineProperty(window.navigator, 'onLine', { value: true, configurable: true });
    render(<OfflineBanner />);

    // Present and empty while online: a live region inserted together with its text is not
    // reliably announced, so it has to pre-exist the connectivity drop.
    const region = screen.getByRole('status');
    expect(region).toBeEmptyDOMElement();

    act(() => {
      Object.defineProperty(window.navigator, 'onLine', { value: false, configurable: true });
      window.dispatchEvent(new Event('offline'));
    });

    expect(screen.getByRole('status')).toHaveTextContent(/offline mode/i);
  });

  it('names the patient-record skeleton, which is otherwise silent grey boxes', async () => {
    const { default: PatientDetailPage } = await import('@/app/(dashboard)/patients/[id]/page');
    vi.mocked(api.reasoningStreamUrl).mockReturnValue(new Promise(() => {}) as never);
    render(<PatientDetailPage params={{ id: 'pat-1' }} />);

    expect(screen.getByRole('status', { name: /loading patient record/i })).toHaveAttribute(
      'aria-busy',
      'true',
    );
  });
});

// --------------------------------------------------------------- decorative content

describe('decorative content stays out of the accessibility tree', () => {
  it('hides every icon in the Reasoning Theatre, including the streamed panels', async () => {
    const { container } = render(<ReasoningTheatre sessionId="s1" onComplete={vi.fn()} />);
    const source = await latestSource();

    act(() => {
      source.emit('agent_complete', { agent: 'hypothesis_panel' });
      source.emit('hypotheses', { hypotheses: [{ diagnosis_name: 'CAP', probability_band: 'high' }] });
      source.emit('cant_miss', { items: [{ diagnosis_name: 'ACS', why: 'risk factors' }] });
      source.emit('devils_advocate', { critique: { summary: 'Consider viral cause.' } });
      source.emit('verifier', { status: 'approved', autonomy_tier: 'flag_for_review' });
    });

    expectNoUnhiddenIcons(container);
  });

  it('hides every icon on a suggestion card, in both the normal and hard-block layouts', () => {
    const normal = render(
      <SuggestionCard
        suggestion={suggestion({ cant_miss_flag: true, devils_advocate: { summary: 'Weak.' } })}
        sessionId="s1"
      />,
    );
    expectNoUnhiddenIcons(normal.container);

    const blocked = render(
      <SuggestionCard suggestion={suggestion({ is_hard_block: true })} sessionId="s1" />,
    );
    expectNoUnhiddenIcons(blocked.container);
  });

  it('hides the evidence tick and cross, which only repeat the heading above them', () => {
    render(<SuggestionCard suggestion={suggestion()} sessionId="s1" />);

    const supporting = screen.getByText('Productive cough and fever for 3 days')
      .closest('li') as HTMLElement;
    expect(supporting).toHaveTextContent('Productive cough and fever for 3 days');
    expect(within(supporting).getByText('✓')).toHaveAttribute('aria-hidden', 'true');
  });

  it('hides the flag-for-review warning icon inside the autonomy badge', () => {
    const { container } = render(<AutonomyBadge tier="flag_for_review" />);

    expect(screen.getByText('Flag for review')).toBeInTheDocument();
    expectNoUnhiddenIcons(container);
  });

  it('hides the per-entry timeline icons', () => {
    const record = {
      medications: [
        { id: 'm1', generic_name: 'Amoxicillin', event_date: '2026-01-04', dose: '500mg' },
      ],
      lab_results: [{ id: 'l1', marker_name: 'CRP', sample_date: '2026-01-05', value_numeric: 40 }],
      conditions: [],
      derived_markers: [],
      allergies: [],
    } as unknown as LongitudinalRecord;

    const { container } = render(<Timeline record={record} />);

    expect(screen.getByText('Amoxicillin')).toBeInTheDocument();
    expectNoUnhiddenIcons(container);
  });
});

// --------------------------------------------------------------- keyboard operation

describe('keyboard operation of the dashboard shell', () => {
  function renderShell() {
    return render(
      <DashboardLayout>
        <p>chart</p>
      </DashboardLayout>,
    );
  }

  it('offers a skip link ahead of the header that lands on the main region', async () => {
    renderShell();

    const skip = screen.getByRole('link', { name: /skip to main content/i });
    // First in the tab order: everything before it in the DOM is the skip link itself.
    skip.focus();
    expect(skip).toHaveFocus();
    expect(skip).toHaveAttribute('href', '#main-content');

    const main = screen.getByRole('main');
    expect(main).toHaveAttribute('id', 'main-content');
    // Without a tabindex the fragment jump moves the viewport but leaves focus in the header,
    // so the next Tab walks the nav again — the exact thing the link exists to avoid.
    expect(main).toHaveAttribute('tabindex', '-1');
  });

  it('is visually hidden until it takes focus, so it does not disturb the header', () => {
    renderShell();

    const skip = screen.getByRole('link', { name: /skip to main content/i });
    expect(skip.className).toContain('sr-only');
    expect(skip.className).toContain('focus:not-sr-only');
  });

  it('points the mobile toggle at the panel it opens', async () => {
    renderShell();

    const toggle = screen.getByRole('button', { name: /open menu/i });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveAttribute('aria-controls', 'mobile-menu');

    await userEvent.click(toggle);

    expect(screen.getByRole('button', { name: /close menu/i })).toHaveAttribute(
      'aria-expanded',
      'true',
    );
  });

  it('closes the mobile menu on Escape rather than only on a second click of the toggle', async () => {
    renderShell();

    await userEvent.click(screen.getByRole('button', { name: /open menu/i }));
    expect(screen.getByRole('button', { name: /close menu/i })).toBeInTheDocument();

    await userEvent.keyboard('{Escape}');

    expect(screen.getByRole('button', { name: /open menu/i })).toHaveAttribute(
      'aria-expanded',
      'false',
    );
  });
});
