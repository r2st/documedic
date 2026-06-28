'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { api, ApiError } from '@/lib/api';
import type { PatientSummary } from '@/lib/types';
import { Button, Card } from '@/components/ui';

export default function PatientsPage() {
  const [patients, setPatients] = useState<PatientSummary[]>([]);
  const [search, setSearch] = useState('');
  const [showForm, setShowForm] = useState(false);
  const [loading, setLoading] = useState(true);

  async function load(q?: string) {
    try {
      const res = await api.listPatients(q);
      setPatients(res.items);
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
          {!loading && (
            <p className="mt-1 text-sm text-slate-500">
              {patients.length} patient{patients.length !== 1 ? 's' : ''} registered
            </p>
          )}
        </div>
        <Button className="flex-shrink-0" onClick={() => setShowForm((s) => !s)}>
          {showForm ? (
            'Cancel'
          ) : (
            <span className="flex items-center gap-1.5 whitespace-nowrap">
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
              </svg>
              New patient
            </span>
          )}
        </Button>
      </div>

      {showForm && <CreatePatientForm onCreated={() => { setShowForm(false); void load(search); }} />}

      {/* Search bar */}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void load(search);
        }}
        className="flex gap-2"
      >
        <div className="relative flex-1">
          <svg className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" d="m21 21-5.197-5.197m0 0A7.5 7.5 0 1 0 5.196 5.196a7.5 7.5 0 0 0 10.607 10.607Z" />
          </svg>
          <input
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

      {/* Patient list */}
      {loading ? (
        <div className="space-y-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="rounded-xl border border-slate-200/80 bg-white p-5 shadow-card">
              <div className="flex items-center gap-4">
                <div className="h-10 w-10 animate-pulse rounded-full bg-slate-100" />
                <div className="flex-1">
                  <div className="mb-2 h-5 w-36 animate-pulse rounded-md bg-slate-100" />
                  <div className="h-4 w-52 animate-pulse rounded-md bg-slate-50" />
                </div>
              </div>
            </div>
          ))}
        </div>
      ) : patients.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-slate-300 bg-white py-16 text-center">
          <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-brand-50">
            <svg className="h-7 w-7 text-brand-500" fill="none" viewBox="0 0 24 24" strokeWidth={1.5} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="M18 18.72a9.094 9.094 0 0 0 3.741-.479 3 3 0 0 0-4.682-2.72m.94 3.198.001.031c0 .225-.012.447-.037.666A11.944 11.944 0 0 1 12 21c-2.17 0-4.207-.576-5.963-1.584A6.062 6.062 0 0 1 6 18.719m12 0a5.971 5.971 0 0 0-.941-3.197m0 0A5.995 5.995 0 0 0 12 12.75a5.995 5.995 0 0 0-5.058 2.772m0 0a3 3 0 0 0-4.681 2.72 8.986 8.986 0 0 0 3.74.477m.94-3.197a5.971 5.971 0 0 0-.94 3.197M15 6.75a3 3 0 1 1-6 0 3 3 0 0 1 6 0Zm6 3a2.25 2.25 0 1 1-4.5 0 2.25 2.25 0 0 1 4.5 0Zm-13.5 0a2.25 2.25 0 1 1-4.5 0 2.25 2.25 0 0 1 4.5 0Z" />
            </svg>
          </div>
          <h3 className="text-base font-semibold text-slate-900">No patients yet</h3>
          <p className="mt-1 text-sm text-slate-500">Create your first patient to get started.</p>
        </div>
      ) : (
        <ul className="space-y-3">
          {patients.map((p) => (
            <li key={p.id} className="animate-fade-in">
              <Link href={`/patients/${p.id}`} className="block group">
                <Card className="transition-all duration-200 group-hover:border-brand-300 group-hover:shadow-card-hover">
                  <div className="flex items-center gap-4">
                    <div className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-full bg-brand-50 text-sm font-semibold text-brand-700 group-hover:bg-brand-100 transition-colors">
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
                    <svg className="h-5 w-5 flex-shrink-0 text-slate-300 group-hover:text-brand-500 transition-colors" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" />
                    </svg>
                  </div>
                </Card>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
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
      setError(err instanceof ApiError ? err.message : 'Failed to create patient');
    }
  }

  return (
    <Card className="animate-slide-up">
      <h3 className="mb-4 text-base font-semibold text-slate-900">Register new patient</h3>
      <form onSubmit={submit} className="space-y-4">
        {error && (
          <div className="flex items-start gap-2 rounded-lg bg-red-50 p-3 text-sm text-red-700 ring-1 ring-red-200">
            <svg className="mt-0.5 h-4 w-4 flex-shrink-0" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
            </svg>
            <span>{error}</span>
          </div>
        )}
        <div>
          <label className="mb-1.5 block text-sm font-medium text-slate-700">Full name</label>
          <input
            required
            placeholder="Enter patient's full name"
            value={fullName}
            onChange={(e) => setFullName(e.target.value)}
            className="w-full"
          />
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <div>
            <label className="mb-1.5 block text-sm font-medium text-slate-700">Sex</label>
            <select
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
            <label className="mb-1.5 block text-sm font-medium text-slate-700">Date of birth</label>
            <input
              type="date"
              value={dob}
              onChange={(e) => setDob(e.target.value)}
              className="w-full"
            />
          </div>
          <div>
            <label className="mb-1.5 block text-sm font-medium text-slate-700">Phone</label>
            <input
              placeholder="Phone number"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              className="w-full"
            />
          </div>
        </div>
        <label className="flex items-center gap-3 rounded-lg border border-slate-200 bg-slate-50 p-3 text-sm cursor-pointer hover:bg-slate-100 transition-colors">
          <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} className="h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500" />
          <span className="text-slate-700">Patient consent obtained for data processing (required — DPDP Act)</span>
        </label>
        <Button type="submit" disabled={!consent}>
          Create patient
        </Button>
      </form>
    </Card>
  );
}
