'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { api, ApiError } from '@/lib/api';
import type { ExtractionResult } from '@/lib/types';
import { Button, Card, ConfidenceBadge } from '@/components/ui';

export default function UploadPage({ params }: { params: { id: string } }) {
  const { id } = params;
  const router = useRouter();
  const [docId, setDocId] = useState<string | null>(null);
  const [extraction, setExtraction] = useState<ExtractionResult | null>(null);
  const [rejected, setRejected] = useState<Set<number>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onUpload(file: File) {
    setError(null);
    setBusy(true);
    try {
      const doc = await api.uploadDocument(id, file);
      setDocId(doc.id);
      setExtraction(await api.getExtraction(id, doc.id));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Upload failed');
    } finally {
      setBusy(false);
    }
  }

  async function approve() {
    if (!docId) return;
    setBusy(true);
    try {
      await api.approveExtraction(id, docId, [...rejected]);
      router.push(`/patients/${id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Approval failed');
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
    <div className="space-y-4">
      <Link href={`/patients/${id}`} className="text-sm text-blue-600 hover:underline">
        ← Back to patient
      </Link>
      <h1 className="text-xl font-bold">Upload document</h1>
      {error && <p className="rounded bg-red-50 p-2 text-sm text-red-700">{error}</p>}

      {!extraction && (
        <Card>
          <p className="mb-3 text-sm text-slate-600">
            Upload a prescription, lab report, or discharge summary (PDF / JPEG / PNG). Extraction
            runs automatically; you review and confirm before anything enters the record.
          </p>
          <input
            type="file"
            accept=".pdf,image/*"
            disabled={busy}
            onChange={(e) => e.target.files?.[0] && onUpload(e.target.files[0])}
            className="text-sm"
          />
          {busy && <p className="mt-2 text-sm text-slate-500">Extracting…</p>}
        </Card>
      )}

      {extraction && (
        <Card>
          <div className="mb-3 flex items-center justify-between">
            <h2 className="font-semibold">
              Review extraction{' '}
              <span className="text-sm font-normal text-slate-500">
                ({extraction.document_type ?? 'unknown'},{' '}
                {extraction.confirmation_required_count} field(s) need confirmation
                {extraction.ocr_fallback_used ? ', OCR fallback used' : ''})
              </span>
            </h2>
          </div>
          <p className="mb-3 text-xs text-slate-500">
            Evidence is shown for clinician confirmation. Uncheck any entity to exclude it.
          </p>
          <ul className="space-y-2">
            {extraction.entities.map((ent, i) => (
              <li
                key={i}
                className={`rounded-md border p-3 ${
                  rejected.has(i) ? 'border-slate-200 bg-slate-50 opacity-50' : 'border-slate-300'
                }`}
              >
                <div className="mb-1 flex items-center justify-between">
                  <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                    {ent.entity_type}
                  </span>
                  <label className="flex items-center gap-1 text-xs text-slate-500">
                    <input
                      type="checkbox"
                      checked={!rejected.has(i)}
                      onChange={() => toggleReject(i)}
                    />
                    include
                  </label>
                </div>
                <div className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
                  {ent.fields.map((f) => (
                    <span key={f.name} className="flex items-center gap-1">
                      <span className="text-slate-500">{f.name}:</span>
                      <span className="font-medium">{String(f.value)}</span>
                      <ConfidenceBadge band={f.confidence_band} value={f.confidence} />
                    </span>
                  ))}
                </div>
              </li>
            ))}
          </ul>
          <div className="mt-4 flex flex-col gap-2 sm:flex-row">
            <Button onClick={approve} disabled={busy}>
              {busy ? 'Saving…' : 'Confirm & merge into record'}
            </Button>
            <Link href={`/patients/${id}`}>
              <Button variant="secondary">Discard</Button>
            </Link>
          </div>
        </Card>
      )}
    </div>
  );
}
