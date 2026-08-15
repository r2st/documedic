'use client';

import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { api, tokenStore } from './api';
import { clearDrafts, retainDraftsFor } from './drafts';
import type { Account } from './types';

interface AuthState {
  account: Account | null;
  loading: boolean;
  /** Seconds until the idle timeout signs this clinician out, or null when it is not close. */
  idleSecondsRemaining: number | null;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState>({
  account: null,
  loading: true,
  idleSecondsRemaining: null,
  refresh: async () => {},
  logout: async () => {},
});

// Clinical-workstation-style idle timeout: no mouse/keyboard/touch activity for this long signs
// the clinician out, independent of the (longer-lived) refresh-token session on the backend.
const IDLE_LOGOUT_MS = 15 * 60 * 1000;
// How long before that the clinician is told it is about to happen.
//
// The sign-out itself is not the problem — it is what a shared workstation is supposed to do.
// The problem is that it arrives with no notice, in the middle of composing a note that has not
// been sent anywhere, and the clinician who triggers it is by definition the one not looking at
// the keyboard. Two minutes is long enough to finish a sentence and press a button, and short
// enough that the warning still means "now" rather than becoming background furniture.
//
// Any activity at all cancels it, because it is an *idle* warning: a clinician who comes back and
// moves the mouse has already given the only answer the warning was asking for.
const IDLE_WARNING_MS = 2 * 60 * 1000;
// Rotate the access/refresh pair this far ahead of expiry so an in-progress action never hits
// a 401 mid-request; the reactive retry-on-401 path in lib/api.ts still covers any gap.
const REFRESH_SKEW_MS = 60 * 1000;
const ACTIVITY_EVENTS = ['mousemove', 'keydown', 'mousedown', 'touchstart', 'scroll'] as const;

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [account, setAccount] = useState<Account | null>(null);
  const [loading, setLoading] = useState(true);
  const [idleSecondsRemaining, setIdleSecondsRemaining] = useState<number | null>(null);
  const idleTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const warningTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const countdown = useRef<ReturnType<typeof setInterval> | null>(null);
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearIdleWarning = useCallback(() => {
    if (warningTimer.current) clearTimeout(warningTimer.current);
    if (countdown.current) clearInterval(countdown.current);
    warningTimer.current = null;
    countdown.current = null;
    setIdleSecondsRemaining(null);
  }, []);

  async function refresh() {
    if (!tokenStore.access) {
      setAccount(null);
      setLoading(false);
      return;
    }
    try {
      setAccount(await api.me());
    } catch {
      setAccount(null);
    } finally {
      setLoading(false);
    }
  }

  /**
   * End the session.
   *
   * `keepDrafts` is the difference between the two ways a session ends. Pressing "Sign out" is a
   * deliberate end of work, so the composed-but-unsent clinical text goes with it. Being timed
   * out is not a decision the clinician made, and their draft is kept in memory for this tab so
   * that signing back in returns them to it — see lib/drafts.ts for why memory and not storage.
   */
  async function logout({ keepDrafts = false }: { keepDrafts?: boolean } = {}) {
    if (idleTimer.current) clearTimeout(idleTimer.current);
    if (refreshTimer.current) clearTimeout(refreshTimer.current);
    clearIdleWarning();
    if (!keepDrafts) clearDrafts();
    await api.logout();
    setAccount(null);
  }

  const resetIdleTimer = useCallback(() => {
    if (idleTimer.current) clearTimeout(idleTimer.current);
    clearIdleWarning();
    if (!tokenStore.access) return;
    warningTimer.current = setTimeout(
      () => {
        setIdleSecondsRemaining(Math.round(IDLE_WARNING_MS / 1000));
        countdown.current = setInterval(() => {
          // Counts to zero and stops there rather than going negative: the idle timer below is
          // what actually signs the clinician out, and the two are separate timers that can drift
          // by a tick. A countdown showing "-3 seconds" beside a session that is still alive is
          // worse than one that sits on zero for a moment.
          setIdleSecondsRemaining((seconds) =>
            seconds === null ? null : Math.max(seconds - 1, 0),
          );
        }, 1000);
      },
      Math.max(IDLE_LOGOUT_MS - IDLE_WARNING_MS, 0),
    );
    idleTimer.current = setTimeout(() => {
      void logout({ keepDrafts: true });
    }, IDLE_LOGOUT_MS);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clearIdleWarning]);

  const scheduleTokenRefresh = useCallback(() => {
    if (refreshTimer.current) clearTimeout(refreshTimer.current);
    const expiresAt = tokenStore.accessExpiresAt;
    if (!expiresAt) return;
    const delay = Math.max(expiresAt - Date.now() - REFRESH_SKEW_MS, 5_000);
    refreshTimer.current = setTimeout(async () => {
      const ok = await api.refreshAccessToken();
      if (ok) {
        scheduleTokenRefresh();
      } else {
        setAccount(null);
      }
    }, delay);
  }, []);

  useEffect(() => {
    void refresh();
  }, []);

  // Hand the draft store whoever is signed in, so it can tell a re-login by the same clinician
  // (drafts kept — this is the timeout case they exist for) from a different one sitting down at
  // the same tab (drafts dropped). The comparison lives in the store rather than here because
  // that is where the drafts live; see lib/drafts.ts.
  useEffect(() => {
    if (account) retainDraftsFor(account.id);
  }, [account]);

  useEffect(() => {
    if (!account || typeof window === 'undefined') return;
    resetIdleTimer();
    scheduleTokenRefresh();
    ACTIVITY_EVENTS.forEach((evt) => window.addEventListener(evt, resetIdleTimer));
    return () => {
      ACTIVITY_EVENTS.forEach((evt) => window.removeEventListener(evt, resetIdleTimer));
      if (idleTimer.current) clearTimeout(idleTimer.current);
      if (refreshTimer.current) clearTimeout(refreshTimer.current);
      clearIdleWarning();
    };
  }, [account, resetIdleTimer, scheduleTokenRefresh, clearIdleWarning]);

  return (
    <AuthContext.Provider value={{ account, loading, idleSecondsRemaining, refresh, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export const useAuth = () => useContext(AuthContext);

/** Redirect to /login when there is no authenticated account. */
export function useRequireAuth() {
  const { account, loading } = useAuth();
  const router = useRouter();
  useEffect(() => {
    if (!loading && !account) router.replace('/login');
  }, [account, loading, router]);
  return { account, loading };
}
