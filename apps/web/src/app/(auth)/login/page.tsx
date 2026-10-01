'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { api } from '@/lib/api';
import { requestErrorMessage } from '@/lib/errors';
import { useAuth } from '@/lib/auth';

export default function LoginPage() {
  const router = useRouter();
  const { refresh } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await api.login(email, password);
      await refresh();
      router.replace('/patients');
    } catch (err) {
      setError(requestErrorMessage(err, 'your sign-in'));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-card">
      <form onSubmit={onSubmit}>
        <h2 className="auth-card-heading">Sign in</h2>

        {error && (
          <div role="alert" className="auth-error">
            {error}
          </div>
        )}

        <div className="auth-field">
          <label htmlFor="email" className="auth-label">
            Email
          </label>
          <input
            id="email"
            type="email"
            required
            placeholder="you@example.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="auth-input"
            autoComplete="email"
          />
        </div>
        <div className="auth-field">
          <label htmlFor="password" className="auth-label">
            Password
          </label>
          <input
            id="password"
            type="password"
            required
            placeholder="Enter your password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="auth-input"
            autoComplete="current-password"
          />
        </div>

        <button type="submit" disabled={busy} className="auth-button">
          {busy ? (
            <span className="auth-button-busy">
              <svg
                aria-hidden="true"
                className="auth-spinner"
                viewBox="0 0 24 24"
                fill="none"
              >
                <circle
                  opacity="0.25"
                  cx="12"
                  cy="12"
                  r="10"
                  stroke="currentColor"
                  strokeWidth="4"
                />
                <path
                  opacity="0.75"
                  fill="currentColor"
                  d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
                />
              </svg>
              Signing in…
            </span>
          ) : (
            'Sign in'
          )}
        </button>

        <p className="auth-alt-link">
          No account?{' '}
          <Link href="/signup">Sign up</Link>
        </p>
      </form>
    </div>
  );
}
