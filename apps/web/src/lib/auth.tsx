'use client';

import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { api, tokenStore } from './api';
import type { Account } from './types';

interface AuthState {
  account: Account | null;
  loading: boolean;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthState>({
  account: null,
  loading: true,
  refresh: async () => {},
  logout: async () => {},
});

// Clinical-workstation-style idle timeout: no mouse/keyboard/touch activity for this long signs
// the clinician out, independent of the (longer-lived) refresh-token session on the backend.
const IDLE_LOGOUT_MS = 15 * 60 * 1000;
// Rotate the access/refresh pair this far ahead of expiry so an in-progress action never hits
// a 401 mid-request; the reactive retry-on-401 path in lib/api.ts still covers any gap.
const REFRESH_SKEW_MS = 60 * 1000;
const ACTIVITY_EVENTS = ['mousemove', 'keydown', 'mousedown', 'touchstart', 'scroll'] as const;

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [account, setAccount] = useState<Account | null>(null);
  const [loading, setLoading] = useState(true);
  const idleTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

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

  async function logout() {
    if (idleTimer.current) clearTimeout(idleTimer.current);
    if (refreshTimer.current) clearTimeout(refreshTimer.current);
    await api.logout();
    setAccount(null);
  }

  const resetIdleTimer = useCallback(() => {
    if (idleTimer.current) clearTimeout(idleTimer.current);
    if (!tokenStore.access) return;
    idleTimer.current = setTimeout(() => {
      void logout();
    }, IDLE_LOGOUT_MS);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  useEffect(() => {
    if (!account || typeof window === 'undefined') return;
    resetIdleTimer();
    scheduleTokenRefresh();
    ACTIVITY_EVENTS.forEach((evt) => window.addEventListener(evt, resetIdleTimer));
    return () => {
      ACTIVITY_EVENTS.forEach((evt) => window.removeEventListener(evt, resetIdleTimer));
      if (idleTimer.current) clearTimeout(idleTimer.current);
      if (refreshTimer.current) clearTimeout(refreshTimer.current);
    };
  }, [account, resetIdleTimer, scheduleTokenRefresh]);

  return (
    <AuthContext.Provider value={{ account, loading, refresh, logout }}>
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
