'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { api } from '@/lib/api';
import { requestErrorMessage } from '@/lib/errors';
import type { ExtractionResult } from '@/lib/types';
import { Button, Card, ConfidenceBadge, ErrorBanner } from '@/components/ui';

export default function UploadPage({ params }: { params: { id: string } }) {
  const { id } = params;
  const router = useRouter();
  // The document id and its extraction always arrive together from onUpload, so they
  // live in one state object — that makes "extraction present, id missing" unrepresentable
  // and lets the approve handler take a non-null id without a defensive guard.
  const [reviewed, setReviewed] = useState<{ docId: string; extraction: ExtractionResult } | null>(
    null,
  );
  const [rejected, setRejected] = useState<Set<number>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  // The file input is a transparent overlay on the drop zone, so its own focus ring is
  // invisible. Without this a keyboard user tabs onto the control and sees nothing change.
  const [inputFocused, setInputFocused] = useState(false);

  async function onUpload(file: File) {
    setError(null);
    setBusy(true);
    try {
      const doc = await api.uploadDocument(id, file);
      setReviewed({ docId: doc.id, extraction: await api.getExtraction(id, doc.id) });
    } catch (err) {
      setError(requestErrorMessage(err, 'the upload'));
    } finally {
      setBusy(false);
    }
  }

  function handleDragOver(e: React.DragEvent) {
    e.preventDefault();
    setIsDragging(true);
  }

  function handleDragLeave(e: React.DragEvent) {
    e.preventDefault();
    setIsDragging(false);
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setIsDragging(false);
    const file = e.dataTransfer.files?.[0];
    if (file) void onUpload(file);
  }

  async function approve(docId: string) {
    setBusy(true);
    try {
      await api.approveExtraction(id, docId, [...rejected]);
      router.push(`/patients/${id}`);
    } catch (err) {
      setError(requestErrorMessage(err, 'the approval of these extracted details'));
    } finally {
      setBusy(false);
    }
  }

  function toggleReject(i: number) {
    setRejected((prev) => {
      const next = new Set(prev);
      next.has(i) ? next.delete(i) : next.add(i);
      return next;
    });
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

      <h1 className="text-2xl font-bold tracking-tight text-slate-900">Upload document</h1>

      {error && <ErrorBanner message={error} />}

      {!reviewed && (
        <Card>
          <p className="mb-4 text-sm text-slate-600">
            Upload a prescription, lab report, or discharge summary (PDF / JPEG / PNG). Extraction
            runs automatically; you review and confirm before anything enters the record.
          </p>

          <div
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            className={`relative rounded-2xl border-2 border-dashed px-6 py-10 text-center transition-colors ${
              isDragging
                ? 'border-brand-400 bg-brand-50'
                : 'border-slate-300 bg-slate-50 hover:border-brand-300 hover:bg-brand-50/50'
            } ${inputFocused ? 'ring-2 ring-brand-500 ring-offset-2' : ''}`}
          >
            {busy ? (
              <div className="flex flex-col items-center" role="status">
                <svg aria-hidden="true" className="h-8 w-8 animate-spin text-brand-600" viewBox="0 0 24 24" fill="none">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
                <p className="mt-3 text-sm font-medium text-slate-700">Extracting document data...</p>
                <p className="mt-1 text-xs text-slate-500">This may take a moment</p>
              </div>
            ) : (
              <>
                <svg
                  aria-hidden="true"
                  className="mx-auto h-10 w-10 text-slate-400"
                  fill="none"
                  viewBox="0 0 24 24"
                  strokeWidth={1.5}
                  stroke="currentColor"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5"
                  />
                </svg>
                <label htmlFor="document-file" className="mt-3 block text-sm font-medium text-slate-700">
                  Drag and drop a file here, or click to browse
                </label>
                <p id="document-file-hint" className="mt-1 text-xs text-slate-500">
                  PDF, JPEG, or PNG up to 10 MB
                </p>
                <input
                  id="document-file"
                  type="file"
                  accept=".pdf,image/*"
                  disabled={busy}
                  aria-describedby="document-file-hint"
                  onChange={(e) => e.target.files?.[0] && onUpload(e.target.files[0])}
                  onFocus={() => setInputFocused(true)}
                  onBlur={() => setInputFocused(false)}
                  className="absolute inset-0 cursor-pointer opacity-0"
                />
              </>
            )}
          </div>
        </Card>
      )}

      {reviewed && (
        <Card className="animate-fade-in">
          <div className="mb-4">
            <div className="flex items-center justify-between">
              <h2 className="text-lg font-semibold text-slate-900">Review extraction</h2>
              <div className="flex items-center gap-2">
                {reviewed.extraction.ocr_fallback_used && (
                  <span className="inline-flex items-center rounded-full bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-700 ring-1 ring-inset ring-amber-200">
                    OCR fallback
                  </span>
                )}
                <span className="inline-flex items-center rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600 ring-1 ring-inset ring-slate-200">
                  {reviewed.extraction.document_type ?? 'unknown'}
                </span>
              </div>
            </div>
            <p className="mt-1 text-sm text-slate-500">
              {reviewed.extraction.confirmation_required_count} field(s) need confirmation. Uncheck any entity to exclude it.
            </p>
          </div>

          <ul className="space-y-3">
            {reviewed.extraction.entities.map((ent, i) => (
              <li
                key={i}
                className={`rounded-xl border p-4 transition-all ${
                  rejected.has(i)
                    ? 'border-slate-200 bg-slate-50/80 opacity-50'
                    : 'border-slate-200 bg-white shadow-sm'
                }`}
              >
                <div className="mb-2 flex items-center justify-between">
                  <span className="inline-flex items-center rounded-md bg-brand-50 px-2 py-0.5 text-xs font-semibold uppercase tracking-wider text-brand-700 ring-1 ring-inset ring-brand-200">
                    {ent.entity_type}
                  </span>
                  {/* Every row's visible label reads "Include", so the accessible name has to
                      say which entity — otherwise the list is N identical checkboxes. */}
                  <label className="flex cursor-pointer items-center gap-1.5 text-xs text-slate-500 hover:text-slate-700">
                    <input
                      type="checkbox"
                      checked={!rejected.has(i)}
                      onChange={() => toggleReject(i)}
                      aria-label={`Include ${ent.entity_type} ${i + 1} in the record`}
                      className="rounded border-slate-300 text-brand-600 focus:ring-brand-500"
                    />
                    Include
                  </label>
                </div>
                <div className="flex flex-wrap gap-x-4 gap-y-1.5 text-sm">
                  {ent.fields.map((f) => (
                    <span key={f.name} className="flex items-center gap-1">
                      <span className="text-slate-500">{f.name}:</span>
                      <span className="font-medium text-slate-900">{String(f.value)}</span>
                      <ConfidenceBadge band={f.confidence_band} value={f.confidence} />
                    </span>
                  ))}
                </div>
              </li>
            ))}
          </ul>

          <div className="mt-5 flex flex-col gap-2 border-t border-slate-100 pt-4 sm:flex-row">
            <Button className="w-full sm:w-auto" onClick={() => void approve(reviewed.docId)} disabled={busy} aria-busy={busy}>
              {busy ? 'Saving…' : 'Confirm & merge into record'}
            </Button>
            <Link href={`/patients/${id}`} className="w-full sm:w-auto">
              <Button className="w-full sm:w-auto" variant="secondary">Discard</Button>
            </Link>
          </div>
        </Card>
      )}
    </div>
  );
}
