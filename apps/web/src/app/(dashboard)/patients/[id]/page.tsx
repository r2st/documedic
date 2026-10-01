'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { api, ApiError } from '@/lib/api';
import type { AuditEntry, LongitudinalRecord, Patient } from '@/lib/types';
import { medicationDocumentedLabel } from '@/lib/medications';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { ReauthPrompt } from '@/components/ReauthPrompt';
import { Timeline } from '@/components/patient/Timeline';
import { Button, Card, LoadingBlock, Skeleton, SkeletonCards } from '@aether/ui';

const SECTION_ICONS: Record<string, JSX.Element> = {
  Allergies: (
    <svg
      aria-hidden="true"
      className="h-5 w-5 text-red-500"
      fill="none"
      viewBox="0 0 24 24"
      strokeWidth={1.5}
      stroke="currentColor"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z"
      />
    </svg>
  ),
  Medications: (
    <svg
      aria-hidden="true"
      className="h-5 w-5 text-blue-500"
      fill="none"
      viewBox="0 0 24 24"
      strokeWidth={1.5}
      stroke="currentColor"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="m20.893 13.393-1.135-1.135a2.252 2.252 0 0 1-.421-.585l-1.08-2.16a.414.414 0 0 0-.663-.107.827.827 0 0 1-.812.21l-1.273-.363a.89.89 0 0 0-.738 1.595l.587.39c.59.395.674 1.23.172 1.732l-.2.2c-.211.212-.33.498-.33.796v.41c0 .409-.11.809-.32 1.158l-1.315 2.191a2.11 2.11 0 0 1-1.81 1.025 1.055 1.055 0 0 1-1.055-1.055v-1.172c0-.92-.56-1.747-1.414-2.089l-.655-.261a2.25 2.25 0 0 1-1.383-2.46l.007-.042a2.25 2.25 0 0 1 .29-.787l.09-.15a2.25 2.25 0 0 1 2.37-1.048l1.178.236c.858.172 1.694-.384 1.694-1.257 0-.658-.537-1.193-1.195-1.193H5.25"
      />
    </svg>
  ),
  Conditions: (
    <svg
      aria-hidden="true"
      className="h-5 w-5 text-amber-500"
      fill="none"
      viewBox="0 0 24 24"
      strokeWidth={1.5}
      stroke="currentColor"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M9 12h3.75M9 15h3.75M9 18h3.75m3 .75H18a2.25 2.25 0 0 0 2.25-2.25V6.108c0-1.135-.845-2.098-1.976-2.192a48.424 48.424 0 0 0-1.123-.08m-5.801 0c-.065.21-.1.433-.1.664 0 .414.336.75.75.75h4.5a.75.75 0 0 0 .75-.75 2.25 2.25 0 0 0-.1-.664m-5.8 0A2.251 2.251 0 0 1 13.5 2.25H15c1.012 0 1.867.668 2.15 1.586m-5.8 0c-.376.023-.75.05-1.124.08C9.095 4.01 8.25 4.973 8.25 6.108V8.25m0 0H4.875c-.621 0-1.125.504-1.125 1.125v11.25c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125V9.375c0-.621-.504-1.125-1.125-1.125H8.25Z"
      />
    </svg>
  ),
  'Lab results': (
    <svg
      aria-hidden="true"
      className="h-5 w-5 text-emerald-500"
      fill="none"
      viewBox="0 0 24 24"
      strokeWidth={1.5}
      stroke="currentColor"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M9.75 3.104v5.714a2.25 2.25 0 0 1-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 0 1 4.5 0m0 0v5.714c0 .597.237 1.17.659 1.591L19.8 15.3M14.25 3.104c.251.023.501.05.75.082M19.8 15.3l-1.57.393A9.065 9.065 0 0 1 12 15a9.065 9.065 0 0 0-6.23.693L5 14.5m14.8.8 1.402 1.402c1.232 1.232.65 3.318-1.067 3.611A48.309 48.309 0 0 1 12 21c-2.773 0-5.491-.235-8.135-.687-1.718-.293-2.3-2.379-1.067-3.61L5 14.5"
      />
    </svg>
  ),
  'Derived markers': (
    <svg
      aria-hidden="true"
      className="h-5 w-5 text-purple-500"
      fill="none"
      viewBox="0 0 24 24"
      strokeWidth={1.5}
      stroke="currentColor"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M15.75 15.75V18m-7.5-6.75h.008v.008H8.25v-.008Zm0 2.25h.008v.008H8.25V13.5Zm0 2.25h.008v.008H8.25v-.008Zm0 2.25h.008v.008H8.25V18Zm2.498-6.75h.007v.008h-.007v-.008Zm0 2.25h.007v.008h-.007V13.5Zm0 2.25h.007v.008h-.007v-.008Zm0 2.25h.007v.008h-.007V18Zm2.504-6.75h.008v.008h-.008v-.008Zm0 2.25h.008v.008h-.008V13.5Zm0 2.25h.008v.008h-.008v-.008Zm0 2.25h.008v.008h-.008V18Zm2.498-6.75h.008v.008h-.008v-.008Zm0 2.25h.008v.008h-.008V13.5ZM8.25 6h7.5v2.25h-7.5V6ZM12 2.25c-1.892 0-3.758.11-5.593.322C5.307 2.7 4.5 3.65 4.5 4.757V19.5a2.25 2.25 0 0 0 2.25 2.25h10.5a2.25 2.25 0 0 0 2.25-2.25V4.757c0-1.108-.806-2.057-1.907-2.185A48.507 48.507 0 0 0 12 2.25Z"
      />
    </svg>
  ),
};

export default function PatientDetailPage({ params }: { params: { id: string } }) {
  const { id } = params;
  const [patient, setPatient] = useState<Patient | null>(null);
  const [record, setRecord] = useState<LongitudinalRecord | null>(null);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [chainValid, setChainValid] = useState<boolean | null>(null);
  const [loading, setLoading] = useState(true);
  const [exporting, setExporting] = useState<'fhir' | 'pdf' | null>(null);
  const [exportNeedingPassword, setExportNeedingPassword] = useState<'fhir' | 'pdf' | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    setLoading(true);
    try {
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
    } catch (err) {
      if (err instanceof ApiError) {
        setError(
          err.status === 404
            ? 'Patient not found. They may have been deleted or the link is invalid.'
            : `Failed to load patient data: ${err.message}`,
        );
      } else {
        setError('An unexpected error occurred while loading patient data.');
      }
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  const reloadChart = () => void load();

  const exportRecord = async (format: 'fhir' | 'pdf') => {
    setExporting(format);
    try {
      await api.exportPatientRecord(id, format);
      setExportNeedingPassword(null);
    } catch (err) {
      if (err instanceof ApiError && err.code === 'reauthentication_required') {
        setExportNeedingPassword(format);
      } else {
        setError(
          err instanceof ApiError
            ? `Could not export this record: ${err.message}`
            : 'Could not export this record. Check the connection and try again.',
        );
      }
    } finally {
      setExporting(null);
    }
  };

  if (error)
    return (
      <div role="alert" className="flex flex-col items-center justify-center py-20 text-center">
        <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-red-950/30">
          <svg
            aria-hidden="true"
            className="h-7 w-7 text-red-500"
            fill="none"
            viewBox="0 0 24 24"
            strokeWidth={1.5}
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z"
            />
          </svg>
        </div>
        <h2 className="text-lg font-semibold text-[#E5E7EB]">Unable to load patient</h2>
        <p className="mt-1 max-w-md text-sm text-[#9CA3AF]">{error}</p>
        <div className="mt-6 flex flex-col gap-2 sm:flex-row">
          <Button variant="secondary" size="sm" onClick={reloadChart}>
            Try again
          </Button>
          <Link href="/patients">
            <Button variant="secondary" size="sm">
              <svg
                aria-hidden="true"
                className="h-4 w-4"
                fill="none"
                viewBox="0 0 24 24"
                strokeWidth={2}
                stroke="currentColor"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M15.75 19.5 8.25 12l7.5-7.5"
                />
              </svg>
              Back to patients
            </Button>
          </Link>
        </div>
      </div>
    );

  if (loading)
    return (
      <LoadingBlock label="Loading patient record" className="space-y-6">
        <div>
          <Skeleton className="mb-2 h-4 w-20" />
          <Skeleton className="mb-1 h-7 w-52 bg-[#2A2A2D]" />
          <Skeleton className="h-4 w-36" />
        </div>
        <SkeletonCards count={4} />
      </LoadingBlock>
    );

  if (!patient) return null;

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <Link
            href="/patients"
            className="group mb-2 inline-flex items-center gap-1 text-sm font-medium text-[#F0B429] hover:text-[#F7CC5F]"
          >
            <svg
              aria-hidden="true"
              className="h-4 w-4 transition-transform group-hover:-translate-x-0.5"
              fill="none"
              viewBox="0 0 24 24"
              strokeWidth={2}
              stroke="currentColor"
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5 8.25 12l7.5-7.5" />
            </svg>
            All patients
          </Link>
          <div className="flex items-center gap-3">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-[#F0B429]/15 text-lg font-bold text-[#F0B429]">
              {patient.full_name?.[0]?.toUpperCase() ?? '?'}
            </div>
            <div>
              <h1 className="text-2xl font-bold tracking-tight text-[#E5E7EB]">
                {patient.full_name}
              </h1>
              <p className="text-sm text-[#9CA3AF]">
                {patient.sex ?? 'unknown'} · {patient.date_of_birth ?? 'DOB unknown'}
              </p>
            </div>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-2 sm:flex sm:flex-wrap">
          <Link href={`/patients/${id}/upload`} className="w-full sm:w-auto">
            <Button className="w-full sm:w-auto" variant="secondary" size="sm">
              <svg aria-hidden="true" className="h-4 w-4" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5m-13.5-9L12 3m0 0 4.5 4.5M12 3v13.5" />
              </svg>
              Upload document
            </Button>
          </Link>
          <Link href={`/patients/${id}/safety`} className="w-full sm:w-auto">
            <Button className="w-full sm:w-auto" variant="secondary" size="sm">
              <svg aria-hidden="true" className="h-4 w-4" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75 11.25 15 15 9.75m-3-7.036A11.959 11.959 0 0 1 3.598 6 11.99 11.99 0 0 0 3 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285Z" />
              </svg>
              Drug safety
            </Button>
          </Link>
          <Button className="w-full sm:w-auto" variant="secondary" size="sm" disabled={exporting !== null} onClick={() => void exportRecord('fhir')}>
            <svg aria-hidden="true" className="h-4 w-4" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5M16.5 12 12 16.5m0 0L7.5 12m4.5 4.5V3" />
            </svg>
            {exporting === 'fhir' ? 'Exporting…' : 'Export record (FHIR)'}
          </Button>
          <Button className="w-full sm:w-auto" variant="secondary" size="sm" disabled={exporting !== null} onClick={() => void exportRecord('pdf')}>
            <svg aria-hidden="true" className="h-4 w-4" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6.72 13.829c-.24.03-.48.062-.72.096m.72-.096a42.415 42.415 0 0 1 10.56 0m-10.56 0L6.34 18m10.94-4.171c.24.03.48.062.72.096m-.72-.096L17.66 18m0 0 .229 2.523a1.125 1.125 0 0 1-1.12 1.227H7.231c-.662 0-1.18-.568-1.12-1.227L6.34 18m11.318 0h1.091A2.25 2.25 0 0 0 21 15.75V9.456c0-1.081-.768-2.015-1.837-2.175a48.055 48.055 0 0 0-1.913-.247M6.34 18H5.25A2.25 2.25 0 0 1 3 15.75V9.456c0-1.081.768-2.015 1.837-2.175a48.041 48.041 0 0 1 1.913-.247m10.5 0a48.536 48.536 0 0 0-10.5 0m10.5 0V3.375c0-.621-.504-1.125-1.125-1.125h-8.25c-.621 0-1.125.504-1.125 1.125v3.659M18 10.5h.008v.008H18V10.5Zm-3 0h.008v.008H15V10.5Z" />
            </svg>
            {exporting === 'pdf' ? 'Exporting…' : 'Export record (PDF)'}
          </Button>
          <Link href={`/patients/${id}/encounter`} className="col-span-2 w-full sm:w-auto">
            <Button className="w-full sm:w-auto" size="sm">
              <svg aria-hidden="true" className="h-4 w-4" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09ZM18.259 8.715 18 9.75l-.259-1.035a3.375 3.375 0 0 0-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 0 0 2.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 0 0 2.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 0 0-2.455 2.456ZM16.894 20.567 16.5 21.75l-.394-1.183a2.25 2.25 0 0 0-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 0 0 1.423-1.423l.394-1.183.394 1.183a2.25 2.25 0 0 0 1.423 1.423l1.183.394-1.183.394a2.25 2.25 0 0 0-1.423 1.423Z" />
              </svg>
              Start reasoning
            </Button>
          </Link>
        </div>
      </div>

      {exportNeedingPassword && (
        <ReauthPrompt
          action={`export this record as ${exportNeedingPassword === 'pdf' ? 'a PDF' : 'FHIR'}`}
          onConfirmed={() => {
            const format = exportNeedingPassword;
            setExportNeedingPassword(null);
            void exportRecord(format);
          }}
          onCancel={() => setExportNeedingPassword(null)}
        />
      )}

      {record && (
        <ErrorBoundary section="The timeline" onReset={reloadChart}>
          <Card>
            <div className="mb-3 flex items-center gap-2">
              <svg
                aria-hidden="true"
                className="h-5 w-5 text-[#6B7280]"
                fill="none"
                viewBox="0 0 24 24"
                strokeWidth={1.5}
                stroke="currentColor"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M12 6v6h4.5m4.5 0a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z"
                />
              </svg>
              <h2 className="font-semibold text-[#E5E7EB]">Timeline</h2>
            </div>
            <Timeline record={record} />
          </Card>
        </ErrorBoundary>
      )}

      {record && (
        <ErrorBoundary section="The record sections" onReset={reloadChart}>
          <div className="grid gap-4 md:grid-cols-2">
            <RecordSection
              title="Allergies"
              icon={SECTION_ICONS['Allergies']}
              rows={record.allergies}
              fields={['allergen_name', 'severity', 'status']}
              empty="No allergies recorded"
            />
            <RecordSection
              title="Medications"
              icon={SECTION_ICONS['Medications']}
              rows={record.medications}
              fields={['generic_name', 'dose', 'frequency']}
              empty="No medications"
              badge={(row) => (row.is_current === false ? 'Stopped' : null)}
              meta={(row) => {
                const { text, stale } = medicationDocumentedLabel(row);
                return { text, caution: stale };
              }}
            />
            <RecordSection
              title="Conditions"
              icon={SECTION_ICONS['Conditions']}
              rows={record.conditions}
              fields={['condition_name', 'status']}
              empty="No conditions"
            />
            <RecordSection
              title="Lab results"
              icon={SECTION_ICONS['Lab results']}
              rows={record.lab_results}
              fields={['marker_name', 'value_numeric', 'unit', 'abnormality_direction']}
              empty="No labs"
            />
            <RecordSection
              title="Derived markers"
              icon={SECTION_ICONS['Derived markers']}
              rows={record.derived_markers}
              fields={['marker_name', 'value_numeric', 'unit', 'formula_name']}
              empty="None computed"
            />
          </div>
        </ErrorBoundary>
      )}

      <Card>
        <div className="mb-3 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <svg
              aria-hidden="true"
              className="h-5 w-5 text-[#6B7280]"
              fill="none"
              viewBox="0 0 24 24"
              strokeWidth={1.5}
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M16.5 10.5V6.75a4.5 4.5 0 1 0-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 0 0 2.25-2.25v-6.75a2.25 2.25 0 0 0-2.25-2.25H6.75a2.25 2.25 0 0 0-2.25 2.25v6.75a2.25 2.25 0 0 0 2.25 2.25Z"
              />
            </svg>
            <h2 className="font-semibold text-[#E5E7EB]">Audit trail</h2>
          </div>
          {chainValid !== null && (
            <span
              className={`inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-semibold ${
                chainValid
                  ? 'bg-emerald-950/30 text-emerald-400 ring-1 ring-emerald-800/50'
                  : 'bg-red-950/30 text-red-400 ring-1 ring-red-800/50'
              }`}
            >
              {chainValid ? (
                <>
                  <svg
                    aria-hidden="true"
                    className="h-3.5 w-3.5"
                    fill="none"
                    viewBox="0 0 24 24"
                    strokeWidth={2}
                    stroke="currentColor"
                  >
                    <path strokeLinecap="round" strokeLinejoin="round" d="m4.5 12.75 6 6 9-13.5" />
                  </svg>{' '}
                  Hash chain verified
                </>
              ) : (
                'Chain INVALID'
              )}
            </span>
          )}
        </div>
        <ul className="divide-y divide-[#2A2A2D] text-sm">
          {audit.map((e) => (
            <li
              key={e.id}
              className="flex flex-col gap-0.5 py-2.5 sm:flex-row sm:items-center sm:justify-between"
            >
              <span className="min-w-0 break-all font-mono text-xs text-[#9CA3AF]">
                <span className="mr-1 text-[#6B7280]">#{e.sequence}</span> {e.action}
              </span>
              <span className="flex-shrink-0 text-xs text-[#6B7280]">
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
  icon,
  rows,
  fields,
  empty,
  badge,
  meta,
}: {
  title: string;
  icon?: JSX.Element;
  rows: Array<Record<string, unknown>>;
  fields: string[];
  empty: string;
  badge?: (row: Record<string, unknown>) => string | null;
  meta?: (row: Record<string, unknown>) => { text: string; caution: boolean } | null;
}) {
  return (
    <Card>
      <div className="mb-3 flex items-center gap-2">
        {icon}
        <h2 className="font-semibold text-[#E5E7EB]">{title}</h2>
        <span
          aria-label={`${rows.length} recorded`}
          className="rounded-full bg-[#2A2A2D] px-2 py-0.5 text-xs font-medium text-[#9CA3AF]"
        >
          {rows.length}
        </span>
      </div>
      {rows.length === 0 ? (
        <p className="text-sm text-[#6B7280] italic">{empty}</p>
      ) : (
        <ul className="space-y-1.5">
          {rows.map((row, i) => {
            const label = badge?.(row) ?? null;
            const line = meta?.(row) ?? null;
            return (
              <li
                key={i}
                className={`flex items-center justify-between gap-2 rounded-md px-3 py-2 text-sm ${
                  label ? 'bg-[#111113]/60 text-[#9CA3AF]' : 'bg-[#111113] text-[#E5E7EB]'
                }`}
              >
                <span className="min-w-0">
                  <span className="block">
                    {fields
                      .map((f) => row[f])
                      .filter((v) => v !== null && v !== undefined && v !== '')
                      .join(' · ')}
                  </span>
                  {line && (
                    <span
                      className={`block text-xs ${
                        line.caution ? 'text-amber-400' : 'text-[#6B7280]'
                      }`}
                    >
                      {line.text}
                    </span>
                  )}
                </span>
                {label && (
                  <span className="shrink-0 rounded-full bg-[#2A2A2D] px-2 py-0.5 text-xs font-medium text-[#9CA3AF]">
                    {label}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}
