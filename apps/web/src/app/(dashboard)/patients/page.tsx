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
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">Patients</h1>
        <Button onClick={() => setShowForm((s) => !s)}>{showForm ? 'Cancel' : 'New patient'}</Button>
      </div>

      {showForm && <CreatePatientForm onCreated={() => { setShowForm(false); void load(search); }} />}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          void load(search);
        }}
        className="flex gap-2"
      >
        <input
          placeholder="Search by name or phone"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="flex-1 rounded-md border border-slate-300 px-3 py-2 text-sm"
        />
        <Button variant="secondary" type="submit">
          Search
        </Button>
      </form>

      {loading ? (
        <div className="space-y-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <Card key={i}>
              <div className="flex items-center justify-between">
                <div>
                  <div className="mb-2 h-5 w-32 animate-pulse rounded bg-slate-200" />
                  <div className="h-4 w-48 animate-pulse rounded bg-slate-100" />
                </div>
              </div>
            </Card>
          ))}
        </div>
      ) : patients.length === 0 ? (
        <p className="py-8 text-center text-sm text-slate-500">No patients yet.</p>
      ) : (
        <ul className="space-y-2">
          {patients.map((p) => (
            <li key={p.id}>
              <Link href={`/patients/${p.id}`}>
                <Card className="transition hover:border-blue-400">
                  <div className="flex items-center justify-between">
                    <div>
                      <p className="font-medium">{p.full_name}</p>
                      <p className="text-sm text-slate-500">
                        {p.sex ?? 'unknown'} · {p.date_of_birth ?? 'DOB unknown'} ·{' '}
                        {p.phone ?? 'no phone'}
                      </p>
                    </div>
                    <span className="text-xs text-slate-400">→</span>
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
    <Card>
      <form onSubmit={submit} className="space-y-3">
        {error && <p className="rounded bg-red-50 p-2 text-sm text-red-700">{error}</p>}
        <input
          required
          placeholder="Full name"
          value={fullName}
          onChange={(e) => setFullName(e.target.value)}
          className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
        />
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
          <select
            value={sex}
            onChange={(e) => setSex(e.target.value)}
            className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
          >
            <option value="unknown">Sex: unknown</option>
            <option value="male">Male</option>
            <option value="female">Female</option>
            <option value="other">Other</option>
          </select>
          <input
            type="date"
            value={dob}
            onChange={(e) => setDob(e.target.value)}
            className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
          />
          <input
            placeholder="Phone"
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
          />
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
          Patient consent obtained for data processing (required — DPDP Act)
        </label>
        <Button type="submit" disabled={!consent}>
          Create patient
        </Button>
      </form>
    </Card>
  );
}
