'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { api } from '@/lib/api';
import { requestErrorMessage } from '@/lib/errors';
import type { PatientSummary } from '@/lib/types';
import { Button, Card, ErrorBanner, LoadingBlock, SkeletonList } from '@aether/ui';

export default function PatientsPage() {
  const [patients, setPatients] = useState<PatientSummary[]>([]);
  const [search, setSearch] = useState('');
  const [showForm, setShowForm] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // The failure this catches used to be silent, and silence was the wrong answer twice over.
  // A rejected fetch left `patients` at [] with `loading` false, so the page rendered its
  // empty state: "No patients yet. Create your first patient to get started." A clinician
  // whose panel had failed to load was told, in as many words, that they had no patients —
  // and the obvious next action was to re-register someone who was already in the system.
  async function load(q?: string) {
    setError(null);
    setLoading(true);
    try {
      const res = await api.listPatients(q);
      setPatients(res.items);
    } catch (err) {
      // The stale list stays on screen behind the banner rather than being cleared: it is
      // still the last thing the server actually said, and it is more use than nothing.
      setError(requestErrorMessage(err, 'the patient list'));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-2xl font-bold tracking-tight text-slate-900">Patients</h1>
          {/* Suppressed while an error is up: the count would be of a list we know is stale,
              and "0 patients registered" is the exact claim the banner is there to deny. */}
          {!loading && !error && (
            <p className="mt-1 text-sm text-slate-500">
              {patients.length} patient{patients.length !== 1 ? 's' : ''} registered
            </p>
          )}
        </div>
        <Button
          className="flex-shrink-0"
          onClick={() => setShowForm((s) => !s)}
          aria-expanded={showForm}
          aria-controls="new-patient-form"
        >
          {showForm ? (
            'Cancel'
          ) : (
            <span className="flex items-center gap-1.5 whitespace-nowrap">
              <svg
                aria-hidden="true"
                className="h-4 w-4"
                fill="none"
                viewBox="0 0 24 24"
                strokeWidth={2}
                stroke="currentColor"
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
              </svg>
              New patient
            </span>
          )}
        </Button>
      </div>

      {showForm && (
        <CreatePatientForm
          onCreated={() => {
            setShowForm(false);
            void load(search);
          }}
        />
      )}

      {/* Search bar */}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void load(search);
        }}
        className="flex gap-2"
        role="search"
      >
        <div className="relative flex-1">
          <label htmlFor="patient-search" className="sr-only">
            Search patients by name or phone
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
              d="m21 21-5.197-5.197m0 0A7.5 7.5 0 1 0 5.196 5.196a7.5 7.5 0 0 0 10.607 10.607Z"
            />
          </svg>
          <input
            id="patient-search"
            type="search"
            placeholder="Search by name or phone"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full pl-10"
          />
        </div>
        <Button variant="secondary" type="submit">
          Search
        </Button>
      </form>

      {error && (
        <ErrorBanner message={error} onRetry={() => void load(search)} retrying={loading} />
      )}

      {/* Patient list */}
      {loading ? (
        <LoadingBlock label="Loading patients">
          <SkeletonList rows={3} />
        </LoadingBlock>
      ) : error ? (
        // Deliberately no empty state under an error. "No patients yet" is a statement about
        // the panel; all we know is that we could not read it.
        patients.length > 0 && <PatientList patients={patients} />
      ) : patients.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-slate-300 bg-white py-16 text-center">
          <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-brand-50">
            <svg
              aria-hidden="true"
              className="h-7 w-7 text-brand-500"
              fill="none"
              viewBox="0 0 24 24"
              strokeWidth={1.5}
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M18 18.72a9.094 9.094 0 0 0 3.741-.479 3 3 0 0 0-4.682-2.72m.94 3.198.001.031c0 .225-.012.447-.037.666A11.944 11.944 0 0 1 12 21c-2.17 0-4.207-.576-5.963-1.584A6.062 6.062 0 0 1 6 18.719m12 0a5.971 5.971 0 0 0-.941-3.197m0 0A5.995 5.995 0 0 0 12 12.75a5.995 5.995 0 0 0-5.058 2.772m0 0a3 3 0 0 0-4.681 2.72 8.986 8.986 0 0 0 3.74.477m.94-3.197a5.971 5.971 0 0 0-.94 3.197M15 6.75a3 3 0 1 1-6 0 3 3 0 0 1 6 0Zm6 3a2.25 2.25 0 1 1-4.5 0 2.25 2.25 0 0 1 4.5 0Zm-13.5 0a2.25 2.25 0 1 1-4.5 0 2.25 2.25 0 0 1 4.5 0Z"
              />
            </svg>
          </div>
          <h3 className="text-base font-semibold text-slate-900">No patients yet</h3>
          <p className="mt-1 text-sm text-slate-500">Create your first patient to get started.</p>
        </div>
      ) : (
        <PatientList patients={patients} />
      )}
    </div>
  );
}

/** The roster itself. Extracted so the error branch can keep showing the last good list. */
function PatientList({ patients }: { patients: PatientSummary[] }) {
  return (
    <ul className="space-y-3">
      {patients.map((p) => (
        <li key={p.id} className="animate-fade-in">
          <Link href={`/patients/${p.id}`} className="block group">
            <Card className="transition-all duration-200 group-hover:border-brand-300 group-hover:shadow-card-hover">
              <div className="flex items-center gap-4">
                {/* Decorative: the initial is the patient's own name, read out below. */}
                <div
                  aria-hidden="true"
                  className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-full bg-brand-50 text-sm font-semibold text-brand-700 group-hover:bg-brand-100 transition-colors"
                >
                  {p.full_name?.[0]?.toUpperCase() ?? '?'}
                </div>
                <div className="flex-1 min-w-0">
                  <p className="font-semibold text-slate-900 group-hover:text-brand-700 transition-colors">
                    {p.full_name}
                  </p>
                  <p className="text-sm text-slate-500">
                    {p.sex ?? 'unknown'} · {p.date_of_birth ?? 'DOB unknown'} ·{' '}
                    {p.phone ?? 'no phone'}
                  </p>
                </div>
                <svg
                  aria-hidden="true"
                  className="h-5 w-5 flex-shrink-0 text-slate-300 group-hover:text-brand-500 transition-colors"
                  fill="none"
                  viewBox="0 0 24 24"
                  strokeWidth={2}
                  stroke="currentColor"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="m8.25 4.5 7.5 7.5-7.5 7.5"
                  />
                </svg>
              </div>
            </Card>
          </Link>
        </li>
      ))}
    </ul>
  );
}

function CreatePatientForm({ onCreated }: { onCreated: () => void }) {
  const [fullName, setFullName] = useState('');
  const [sex, setSex] = useState('unknown');
  const [dob, setDob] = useState('');
  const [phone, setPhone] = useState('');
  const [consent, setConsent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api.createPatient({
        full_name: fullName,
        sex,
        date_of_birth: dob || null,
        phone: phone || null,
        consent_given: consent,
      });
      onCreated();
    } catch (err) {
      setError(requestErrorMessage(err, 'the new patient record'));
    }
  }

  return (
    <Card className="animate-slide-up">
      <h3 id="new-patient-heading" className="mb-4 text-base font-semibold text-slate-900">
        Register new patient
      </h3>
      <form
        id="new-patient-form"
        aria-labelledby="new-patient-heading"
        onSubmit={submit}
        className="space-y-4"
      >
        {error && <ErrorBanner message={error} />}
        <div>
          <label
            htmlFor="new-patient-full-name"
            className="mb-1.5 block text-sm font-medium text-slate-700"
          >
            Full name
          </label>
          <input
            id="new-patient-full-name"
            required
            placeholder="Enter patient's full name"
            value={fullName}
            onChange={(e) => setFullName(e.target.value)}
            className="w-full"
          />
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <div>
            <label
              htmlFor="new-patient-sex"
              className="mb-1.5 block text-sm font-medium text-slate-700"
            >
              Sex
            </label>
            <select
              id="new-patient-sex"
              value={sex}
              onChange={(e) => setSex(e.target.value)}
              className="w-full"
            >
              <option value="unknown">Unknown</option>
              <option value="male">Male</option>
              <option value="female">Female</option>
              <option value="other">Other</option>
            </select>
          </div>
          <div>
            <label
              htmlFor="new-patient-dob"
              className="mb-1.5 block text-sm font-medium text-slate-700"
            >
              Date of birth
            </label>
            <input
              id="new-patient-dob"
              type="date"
              value={dob}
              onChange={(e) => setDob(e.target.value)}
              className="w-full"
            />
          </div>
          <div>
            <label
              htmlFor="new-patient-phone"
              className="mb-1.5 block text-sm font-medium text-slate-700"
            >
              Phone
            </label>
            <input
              id="new-patient-phone"
              placeholder="Phone number"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              className="w-full"
            />
          </div>
        </div>
        <label className="flex items-center gap-3 rounded-lg border border-slate-200 bg-slate-50 p-3 text-sm cursor-pointer hover:bg-slate-100 transition-colors">
          <input
            type="checkbox"
            checked={consent}
            onChange={(e) => setConsent(e.target.checked)}
            className="h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500"
          />
          <span className="text-slate-700">
            Patient consent obtained for data processing (required — DPDP Act)
          </span>
        </label>
        {/* A disabled control announces only "dimmed"; without this the reason is invisible
            to anyone not seeing the unticked box above it. */}
        <p id="consent-required-hint" className="text-xs text-slate-500">
          {consent ? 'Consent recorded.' : 'Confirm patient consent above to enable registration.'}
        </p>
        <Button type="submit" disabled={!consent} aria-describedby="consent-required-hint">
          Create patient
        </Button>
      </form>
    </Card>
  );
}
