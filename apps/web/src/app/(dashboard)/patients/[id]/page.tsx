'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { api } from '@/lib/api';
import type { AuditEntry, LongitudinalRecord, Patient } from '@/lib/types';
import { Button, Card } from '@/components/ui';

export default function PatientDetailPage({ params }: { params: { id: string } }) {
  const { id } = params;
  const [patient, setPatient] = useState<Patient | null>(null);
  const [record, setRecord] = useState<LongitudinalRecord | null>(null);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [chainValid, setChainValid] = useState<boolean | null>(null);

  useEffect(() => {
    void (async () => {
      const [patientData, recordData, auditData, verifyData] = await Promise.all([
        api.getPatient(id),
        api.getRecord(id),
        api.auditTrail(id),
        api.verifyAudit(id),
      ]);
      setPatient(patientData);
      setRecord(recordData);
      setAudit(auditData.items);
      setChainValid(verifyData.chain_valid);
    })();
  }, [id]);

  if (!patient) return (
    <div className="space-y-5">
      <div>
        <div className="mb-2 h-4 w-24 animate-pulse rounded bg-slate-200" />
        <div className="mb-1 h-6 w-48 animate-pulse rounded bg-slate-200" />
        <div className="h-4 w-32 animate-pulse rounded bg-slate-100" />
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        {Array.from({ length: 4 }).map((_, i) => (
          <Card key={i}>
            <div className="mb-2 h-5 w-24 animate-pulse rounded bg-slate-200" />
            <div className="h-4 w-full animate-pulse rounded bg-slate-100" />
          </Card>
        ))}
      </div>
    </div>
  );

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <Link href="/patients" className="text-sm text-blue-600 hover:underline">
            ← All patients
          </Link>
          <h1 className="text-xl font-bold">{patient.full_name}</h1>
          <p className="text-sm text-slate-500">
            {patient.sex ?? 'unknown'} · {patient.date_of_birth ?? 'DOB unknown'}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Link href={`/patients/${id}/upload`}>
            <Button variant="secondary">Upload document</Button>
          </Link>
          <Link href={`/patients/${id}/safety`}>
            <Button variant="secondary">Drug safety</Button>
          </Link>
          <Link href={`/patients/${id}/encounter`}>
            <Button>Start reasoning</Button>
          </Link>
        </div>
      </div>

      {record && (
        <div className="grid gap-4 md:grid-cols-2">
          <RecordSection title="Allergies" rows={record.allergies} fields={['allergen_name', 'severity', 'status']} empty="No allergies recorded" />
          <RecordSection title="Active medications" rows={record.medications} fields={['generic_name', 'dose', 'frequency']} empty="No medications" />
          <RecordSection title="Conditions" rows={record.conditions} fields={['condition_name', 'status']} empty="No conditions" />
          <RecordSection title="Lab results" rows={record.lab_results} fields={['marker_name', 'value_numeric', 'unit', 'abnormality_direction']} empty="No labs" />
          <RecordSection title="Derived markers" rows={record.derived_markers} fields={['marker_name', 'value_numeric', 'unit', 'formula_name']} empty="None computed" />
        </div>
      )}

      <Card>
        <div className="mb-2 flex items-center justify-between">
          <h2 className="font-semibold">Audit trail</h2>
          {chainValid !== null && (
            <span
              className={`rounded px-2 py-0.5 text-xs font-medium ${
                chainValid ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'
              }`}
            >
              {chainValid ? 'Hash chain verified ✓' : 'Chain INVALID'}
            </span>
          )}
        </div>
        <ul className="divide-y divide-slate-100 text-sm">
          {audit.map((e) => (
            <li key={e.id} className="flex flex-col gap-0.5 py-1.5 sm:flex-row sm:items-center sm:justify-between">
              <span className="min-w-0 break-all font-mono text-xs text-slate-700">
                #{e.sequence} {e.action}
              </span>
              <span className="flex-shrink-0 text-xs text-slate-400">
                {new Date(e.created_at).toLocaleString()}
              </span>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}

function RecordSection({
  title,
  rows,
  fields,
  empty,
}: {
  title: string;
  rows: Array<Record<string, unknown>>;
  fields: string[];
  empty: string;
}) {
  return (
    <Card>
      <h2 className="mb-2 font-semibold">
        {title} <span className="text-sm font-normal text-slate-400">({rows.length})</span>
      </h2>
      {rows.length === 0 ? (
        <p className="text-sm text-slate-400">{empty}</p>
      ) : (
        <ul className="space-y-1 text-sm">
          {rows.map((row, i) => (
            <li key={i} className="text-slate-700">
              {fields
                .map((f) => row[f])
                .filter((v) => v !== null && v !== undefined && v !== '')
                .join(' · ')}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
