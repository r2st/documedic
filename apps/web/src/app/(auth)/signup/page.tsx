'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { api, ApiError } from '@/lib/api';
import { useAuth } from '@/lib/auth';
import { Button } from '@/components/ui';

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
      setError(err instanceof ApiError ? err.message : 'Sign up failed');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-2xl border border-slate-200/80 bg-white p-6 shadow-elevated sm:p-8">
      <form onSubmit={onSubmit} className="space-y-5">
        <div>
          <h2 className="text-lg font-semibold text-slate-900">Create clinician account</h2>
          <p className="mt-1 text-sm text-slate-500">Set up your credentials to get started</p>
        </div>

        {error && (
          <div className="flex items-start gap-2 rounded-lg bg-red-50 p-3 text-sm text-red-700 ring-1 ring-red-200">
            <svg className="mt-0.5 h-4 w-4 flex-shrink-0" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
            </svg>
            <span>{error}</span>
          </div>
        )}

        <div className="space-y-4">
          <div>
            <label htmlFor="display-name" className="mb-1.5 block text-sm font-medium text-slate-700">
              Display name
              <span className="ml-1 text-xs font-normal text-slate-400">(optional)</span>
            </label>
            <input
              id="display-name"
              placeholder="Dr. Jane Smith"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              className="w-full"
              autoComplete="name"
            />
          </div>
          <div>
            <label htmlFor="email" className="mb-1.5 block text-sm font-medium text-slate-700">
              Email
            </label>
            <input
              id="email"
              type="email"
              required
              placeholder="you@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full"
              autoComplete="email"
            />
          </div>
          <div>
            <label htmlFor="password" className="mb-1.5 block text-sm font-medium text-slate-700">
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
              className="w-full"
              autoComplete="new-password"
            />
          </div>
        </div>

        <Button type="submit" disabled={busy} className="w-full" size="lg">
          {busy ? (
            <span className="flex items-center gap-2">
              <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              Creating account…
            </span>
          ) : (
            'Create account'
          )}
        </Button>

        <p className="text-center text-sm text-slate-500">
          Already have an account?{' '}
          <Link href="/login" className="font-medium text-brand-600 hover:text-brand-700 hover:underline">
            Sign in
          </Link>
        </p>
      </form>
    </div>
  );
}
