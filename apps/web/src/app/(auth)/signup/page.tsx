'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { api } from '@/lib/api';
import { requestErrorMessage } from '@/lib/errors';
import { useAuth } from '@/lib/auth';

export default function SignupPage() {
  const router = useRouter();
  const { refresh } = useAuth();
  const [displayName, setDisplayName] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await api.signup(email, password, displayName || undefined);
      await refresh();
      router.replace('/patients');
    } catch (err) {
      setError(requestErrorMessage(err, 'your sign-up'));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-card">
      <form onSubmit={onSubmit}>
        <h2 className="auth-card-heading">Create clinician account</h2>

        {error && (
          <div role="alert" className="auth-error">
            {error}
          </div>
        )}

        <div className="auth-field">
          <label htmlFor="display-name" className="auth-label">
            Display name{' '}
            <span className="auth-label-hint">(optional)</span>
          </label>
          <input
            id="display-name"
            placeholder="Dr. Jane Smith"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            className="auth-input"
            autoComplete="name"
          />
        </div>
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
            minLength={8}
            placeholder="Minimum 8 characters"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="auth-input"
            autoComplete="new-password"
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
              Creating account…
            </span>
          ) : (
            'Create account'
          )}
        </button>

        <p className="auth-alt-link">
          Already have an account?{' '}
          <Link href="/login">Sign in</Link>
        </p>
      </form>
    </div>
  );
}
