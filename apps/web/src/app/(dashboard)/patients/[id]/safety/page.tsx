'use client';

import { useState } from 'react';
import Link from 'next/link';
import { api } from '@/lib/api';
import { requestErrorMessage } from '@/lib/errors';
import type { SafetyCheckResponse } from '@/lib/types';
import { Button, Card, ErrorBanner, SafetyFlagCard } from '@/components/ui';

export default function SafetyPage({ params }: { params: { id: string } }) {
  const { id } = params;
  const [drug, setDrug] = useState('');
  const [result, setResult] = useState<SafetyCheckResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function check(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setResult(null);
    setBusy(true);
    try {
      setResult(await api.checkDrugSafety(id, { drug_name: drug }));
    } catch (err) {
      setError(requestErrorMessage(err, 'the drug safety check'));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-5">
      <Link
        href={`/patients/${id}`}
        className="group mb-2 inline-flex items-center gap-1 text-sm font-medium text-brand-600 hover:text-brand-700"
      >
        <svg
          aria-hidden="true"
          className="h-4 w-4 transition-transform group-hover:-translate-x-0.5"
          fill="none"
          viewBox="0 0 24 24"
          strokeWidth={2}
          stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5L8.25 12l7.5-7.5" />
        </svg>
        Back to patient
      </Link>

      <div>
        <h1 className="text-2xl font-bold tracking-tight text-slate-900">Drug safety check</h1>
        <p className="mt-1 text-sm text-slate-500">
          Deterministic, offline-capable checks against this patient&apos;s allergies, current
          medications, conditions, and renal function. Hard blocks cannot be overridden.
        </p>
      </div>

      <Card>
        <form onSubmit={check} className="flex gap-2">
          <div className="relative flex-1">
            <label htmlFor="proposed-drug" className="sr-only">
              Proposed drug (brand or generic)
            </label>
            <svg
              aria-hidden="true"
              className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400"
              fill="none"
              viewBox="0 0 24 24"
              strokeWidth={2}
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M9.75 3.104v5.714a2.25 2.25 0 01-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 014.5 0m0 0v5.714c0 .597.237 1.17.659 1.591L19.8 15.3M14.25 3.104c.251.023.501.05.75.082M19.8 15.3l-1.57.393A9.065 9.065 0 0112 15a9.065 9.065 0 00-6.23.693L5 14.5m14.8.8l1.402 1.402c1.232 1.232.65 3.318-1.067 3.611A48.309 48.309 0 0112 21c-2.773 0-5.491-.235-8.135-.687-1.718-.293-2.3-2.379-1.067-3.61L5 14.5"
              />
            </svg>
            <input
              id="proposed-drug"
              required
              placeholder="Proposed drug (brand or generic, e.g. Brufen)"
              value={drug}
              onChange={(e) => setDrug(e.target.value)}
              className="w-full pl-10"
            />
          </div>
          <Button type="submit" disabled={busy} aria-busy={busy}>
            {busy ? 'Checking…' : 'Check'}
          </Button>
        </form>
        {error && <ErrorBanner message={error} className="mt-3" />}
      </Card>

      {/* The verdict arrives without moving focus, so it has to be announced. Polite rather
          than assertive: the clinician asked for this answer and is waiting on it. */}
      <div aria-live="polite">
        {result && (
          <Card className="animate-fade-in">
            <div className="mb-4 flex items-center justify-between">
              <div>
                <h2 className="text-lg font-semibold text-slate-900">
                  {result.proposed_drug_name}
                </h2>
                <p className="text-xs font-mono text-slate-400">
                  {result.proposed_drug_reference_id}
                </p>
              </div>
              {result.is_blocked ? (
                <span className="inline-flex items-center gap-1 rounded-full bg-red-700 px-3 py-1 text-xs font-bold text-white shadow-sm">
                  <svg aria-hidden="true" className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" strokeWidth={2.5} stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636" />
                  </svg>
                  BLOCKED
                </span>
              ) : (
                <span className="inline-flex items-center gap-1 rounded-full bg-green-100 px-3 py-1 text-xs font-semibold text-green-800 ring-1 ring-inset ring-green-200">
                  <svg aria-hidden="true" className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" strokeWidth={2.5} stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                  No hard block
                </span>
              )}
            </div>

            {result.flags.length === 0 ? (
              <div className="rounded-lg bg-green-50 p-4 text-sm text-green-700 ring-1 ring-green-200">
                No interactions, contraindications, or allergy conflicts detected.
              </div>
            ) : (
              <div className="space-y-2">
                {result.flags.map((f, i) => (
                  <SafetyFlagCard
                    key={i}
                    severity={f.severity}
                    isHardBlock={f.is_hard_block}
                    summary={f.summary}
                  />
                ))}
              </div>
            )}

            <div className="mt-4 rounded-lg bg-slate-50 p-3 text-xs text-slate-500 ring-1 ring-slate-100">
              Checked against {String(result.checked_against.current_medications ?? 0)} current
              medication(s), {String(result.checked_against.allergies ?? 0)} allergy(ies),{' '}
              {String(result.checked_against.conditions ?? 0)} condition(s).
              {result.checked_against.egfr_available ? ' eGFR available.' : ''}
            </div>
          </Card>
        )}
      </div>
    </div>
  );
}
