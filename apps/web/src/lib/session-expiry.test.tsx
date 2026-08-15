/**
 * What happens to a half-written clinical note when the session ends.
 *
 * Fifteen minutes with no mouse, keyboard or touch signs a clinician out. That is right for a
 * shared clinical workstation, and it used to happen with no notice and no recovery: the
 * encounter screen unmounted and the presenting complaint — composed but not yet sent to any
 * server, because it is not sent until "Begin intake" — went with it.
 *
 * The clinician this happens to is never the one at the keyboard. Typing resets the idle timer,
 * so the only way to reach it is to stop typing: turn away to examine the patient, take a call,
 * and come back to a login form with the history gone.
 *
 * Two changes, tested here. The timeout announces itself two minutes ahead with a countdown and
 * a way to cancel it, and the draft survives the sign-out regardless — because a warning nobody
 * was in the room to read must not still cost the note.
 */

import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>();
  return {
    ...actual,
    api: { ...actual.api, me: vi.fn(), logout: vi.fn(), refreshAccessToken: vi.fn() },
  };
});

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: vi.fn() }),
}));

import { api, tokenStore } from './api';
import { AuthProvider, useAuth } from './auth';
import { clearDrafts, readDraft, saveDraft } from './drafts';
import { SessionExpiryBanner } from '@/components/Banners';

const ACCOUNT = {
  id: 'acc-1',
  email: 'doc@example.com',
  display_name: null,
  created_at: '2026-01-01T00:00:00Z',
};
const OTHER_ACCOUNT = { ...ACCOUNT, id: 'acc-2', email: 'other@example.com' };

const IDLE_MS = 15 * 60 * 1000;
const WARNING_MS = 2 * 60 * 1000;

const DRAFT = 'encounter:p1:complaint';
const NOTE = '54-year-old with central chest pain radiating to the left arm for 1 hour';

function Probe() {
  const { account, idleSecondsRemaining, logout } = useAuth();
  return (
    <div>
      <span data-testid="who">{account?.email ?? 'anon'}</span>
      <span data-testid="countdown">{idleSecondsRemaining ?? 'none'}</span>
      <button onClick={() => void logout()}>Sign out</button>
      <SessionExpiryBanner />
    </div>
  );
}

async function mount() {
  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  );
  await act(async () => {
    await Promise.resolve();
  });
}

describe('the idle-timeout warning', () => {
  beforeEach(() => {
    localStorage.clear();
    clearDrafts();
    vi.mocked(api.me).mockReset().mockResolvedValue(ACCOUNT);
    vi.mocked(api.logout).mockReset().mockResolvedValue(undefined);
    vi.mocked(api.refreshAccessToken).mockReset().mockResolvedValue(true);
    tokenStore.set({
      access_token: 'a',
      refresh_token: 'r',
      token_type: 'bearer',
      expires_in: 900,
    });
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('shows nothing when the context has no countdown to report', () => {
    // The banner reads whatever the auth context gives it, and it is rendered from the dashboard
    // shell on every screen. A context with no such field — an older build, a test stub — must
    // leave it absent rather than put "signing out in undefined" over the whole app.
    vi.doMock('./auth', () => ({ useAuth: () => ({ account: ACCOUNT, refresh: vi.fn() }) }));
    render(<SessionExpiryBanner />);
    expect(screen.queryByRole('alert')).toBeNull();
    vi.doUnmock('./auth');
  });

  it('says nothing while the clinician is still well inside the window', async () => {
    await mount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(IDLE_MS - WARNING_MS - 1000);
    });

    expect(screen.getByTestId('countdown')).toHaveTextContent('none');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('warns before signing out, not after', async () => {
    await mount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(IDLE_MS - WARNING_MS + 100);
    });

    expect(screen.getByRole('alert')).toHaveTextContent(/signing out in/i);
    expect(api.logout).not.toHaveBeenCalled();
    expect(screen.getByTestId('who')).toHaveTextContent('doc@example.com');
  });

  it('counts down while the warning is up', async () => {
    await mount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(IDLE_MS - WARNING_MS + 100);
    });
    expect(screen.getByTestId('countdown')).toHaveTextContent('120');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(screen.getByTestId('countdown')).toHaveTextContent('115');
  });

  it('never counts below zero', async () => {
    // The countdown and the logout are separate timers and can drift by a tick. A negative
    // number beside a session that is still alive reads as a bug in the thing being trusted to
    // hold the clinical record.
    await mount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(IDLE_MS + 5_000);
    });

    const shown = screen.getByTestId('countdown').textContent ?? '';
    expect(Number(shown) >= 0 || shown === 'none').toBe(true);
  });

  it('cancels the warning when the clinician comes back', async () => {
    await mount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(IDLE_MS - WARNING_MS + 100);
    });
    expect(screen.getByRole('alert')).toBeTruthy();

    await act(async () => {
      window.dispatchEvent(new Event('mousemove'));
      await Promise.resolve();
    });

    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByTestId('countdown')).toHaveTextContent('none');

    // And the full window is available again from that moment, not the remainder of the old one.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(IDLE_MS - 1000);
    });
    expect(api.logout).not.toHaveBeenCalled();
  });

  it('lets the clinician answer the warning from the banner itself', async () => {
    // The button is the only part of the warning a clinician who is reading it can act on. Any
    // activity cancels the timeout, so the click already does the job — but it also re-checks the
    // session, so that "Stay signed in" is confirmed by the server rather than only by the banner
    // disappearing. A button that silently did nothing but hide the alert would be worse than no
    // button, because it would still read as an answer.
    await mount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(IDLE_MS - WARNING_MS + 100);
    });
    expect(screen.getByRole('alert')).toBeTruthy();
    vi.mocked(api.me).mockClear();

    // mousedown before click, because that is the order a browser produces and mousedown is the
    // activity event the idle timer listens for. Firing only `click` would test a sequence no
    // browser generates, and would miss that the timeout is cancelled by the click being a click
    // at all rather than by anything the handler does.
    await act(async () => {
      const button = screen.getByRole('button', { name: 'Stay signed in' });
      fireEvent.mouseDown(button);
      fireEvent.click(button);
      await Promise.resolve();
    });

    expect(api.me).toHaveBeenCalled();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByTestId('countdown')).toHaveTextContent('none');

    // And the session is genuinely extended, not just visually cleared.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(IDLE_MS - 1000);
    });
    expect(api.logout).not.toHaveBeenCalled();
    expect(screen.getByTestId('who')).toHaveTextContent('doc@example.com');
  });

  it('still signs the clinician out when nobody answers the warning', async () => {
    await mount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(IDLE_MS + 1000);
    });

    expect(api.logout).toHaveBeenCalledOnce();
    expect(screen.getByTestId('who')).toHaveTextContent('anon');
  });
});

describe('clinical text across the end of a session', () => {
  beforeEach(() => {
    localStorage.clear();
    clearDrafts();
    vi.mocked(api.me).mockReset().mockResolvedValue(ACCOUNT);
    vi.mocked(api.logout).mockReset().mockResolvedValue(undefined);
    vi.mocked(api.refreshAccessToken).mockReset().mockResolvedValue(true);
    tokenStore.set({
      access_token: 'a',
      refresh_token: 'r',
      token_type: 'bearer',
      expires_in: 900,
    });
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('survives a timeout the clinician was not in the room for', async () => {
    await mount();
    saveDraft(DRAFT, NOTE);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(IDLE_MS + 1000);
    });

    expect(api.logout).toHaveBeenCalledOnce();
    expect(readDraft(DRAFT)).toBe(NOTE);
  });

  it('does not survive a deliberate sign-out', async () => {
    // Pressing "Sign out" is a decision to stop working; being timed out is not. Keeping the
    // note past an explicit sign-out would leave patient history in a form on a shared machine
    // for whoever sits down next.
    await mount();
    saveDraft(DRAFT, NOTE);

    await act(async () => {
      screen.getByText('Sign out').click();
      await Promise.resolve();
    });

    expect(readDraft(DRAFT)).toBe('');
  });

  it('does not survive into a different clinician’s session', async () => {
    await mount();
    saveDraft(DRAFT, NOTE);

    // Timed out with the draft kept, then someone else signs in on the same tab.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(IDLE_MS + 1000);
    });
    expect(readDraft(DRAFT)).toBe(NOTE);

    vi.mocked(api.me).mockResolvedValue(OTHER_ACCOUNT);
    tokenStore.set({
      access_token: 'b',
      refresh_token: 'r2',
      token_type: 'bearer',
      expires_in: 900,
    });
    await mount();

    expect(screen.getAllByTestId('who')[1]).toHaveTextContent('other@example.com');
    expect(readDraft(DRAFT)).toBe('');
  });
});
