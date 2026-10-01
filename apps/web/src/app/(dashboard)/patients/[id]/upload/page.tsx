'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { api, ApiError } from '@/lib/api';
import { requestErrorMessage } from '@/lib/errors';
import type { ExtractionResult } from '@/lib/types';
import { Button, Card, ErrorBanner } from '@aether/ui';
import { ConfidenceBadge } from '@/components/ui';

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
  // How to retry whatever just failed, or null when a retry would not be safe.
  //
  // Which of the two calls behind "upload" failed decides this, and the difference matters
  // enough to be modelled rather than glossed:
  //
  //   - `uploadDocument` rejected with an ApiError. The server answered, so it decided not to
  //     store the file (too large, wrong type, a 500 on its side). Posting it again creates
  //     nothing that is not already meant to be there.
  //   - `uploadDocument` rejected without a response — `fetch` itself threw. The POST may well
  //     have landed and only the reply been lost, so a blind retry is how a patient's chart
  //     ends up with the same lab report twice. No retry here; the message already says to
  //     reload and look before trying again.
  //   - `getExtraction` failed. The document is stored and has an id; the extraction is an
  //     idempotent read of it. This is the case worth having, because without it the obvious
  //     move is to upload the file a second time — which is exactly the duplicate that had to
  //     be avoided above.
  const [retry, setRetry] = useState<(() => void) | null>(null);
  const [busy, setBusy] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  // The file input is a transparent overlay on the drop zone, so its own focus ring is
  // invisible. Without this a keyboard user tabs onto the control and sees nothing change.
  const [inputFocused, setInputFocused] = useState(false);

  function fail(err: unknown, what: string, onRetry: (() => void) | null) {
    setError(requestErrorMessage(err, what));
    // Stored via the updater form: React would otherwise call a bare function argument as a
    // state initialiser rather than storing it.
    setRetry(() => onRetry);
  }

  async function onUpload(file: File) {
    // Cleared before the retry: a failed upload says the work "may not have completed", so
    // leaving that banner up while the retry is in flight describes the previous attempt as
    // though it were the current one.
    setError(null);
    setRetry(null);
    setBusy(true);
    try {
      const doc = await api.uploadDocument(id, file);
      await loadExtraction(doc.id);
    } catch (err) {
      fail(err, 'the upload', err instanceof ApiError ? () => void onUpload(file) : null);
    } finally {
      setBusy(false);
    }
  }

  /**
   * Read the extraction for a document that is already stored.
   *
   * Split out of the upload so a failure here can be retried on its own, and deliberately
   * swallowing rather than rethrowing: it has already set the banner that names this step, and
   * letting it unwind into `onUpload`'s catch would relabel it as a failed upload and offer to
   * post the file a second time.
   */
  async function loadExtraction(docId: string) {
    try {
      setReviewed({ docId, extraction: await api.getExtraction(id, docId) });
    } catch (err) {
      fail(err, 'reading the extracted details', () => void retryExtraction(docId));
    }
  }

  async function retryExtraction(docId: string) {
    setError(null);
    setRetry(null);
    setBusy(true);
    try {
      await loadExtraction(docId);
    } finally {
      setBusy(false);
    }
  }

  /**
   * Read the stored document again, for a scan that produced nothing.
   *
   * Distinct from `retryExtraction` above, which re-fetches an extraction that already exists.
   * This re-runs the extraction itself, and it is the only way out of an empty review queue:
   * the causes are usually transient and nothing about them is the clinician's to fix — every
   * vision provider unreachable, the worker restarted mid-page. Without it the screen offers
   * an Approve button over an empty list, which the server now refuses, and the only remaining
   * move is to upload the same file again.
   */
  async function rereadDocument(docId: string) {
    setError(null);
    setRetry(null);
    setBusy(true);
    try {
      await api.retryDocumentExtraction(id, docId);
      await loadExtraction(docId);
    } catch (err) {
      fail(
        err,
        'reading the document again',
        err instanceof ApiError ? () => void rereadDocument(docId) : null,
      );
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
    // Cleared before the retry, as onUpload does. A failed approval says the work "may not have
    // completed" and to reload before retrying, so leaving that banner up while the retry is in
    // flight describes the previous attempt as though it were the current one.
    setError(null);
    setRetry(null);
    setBusy(true);
    try {
      await api.approveExtraction(id, docId, [...rejected]);
      router.push(`/patients/${id}`);
    } catch (err) {
      // Same split as the upload, for the same reason and with more at stake: an approval that
      // the server rejected merged nothing and can be sent again, but one that got no response
      // may already have merged these entities into the chart. Retrying that blind is how the
      // same prescription is written into a patient's record twice.
      fail(
        err,
        'the approval of these extracted details',
        err instanceof ApiError ? () => void approve(docId) : null,
      );
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
          <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5L8.25 12l7.5-7.5" />
        </svg>
        Back to patient
      </Link>

      <h1 className="text-2xl font-bold tracking-tight text-[#E5E7EB]">Upload document</h1>

      {error && <ErrorBanner message={error} onRetry={retry ?? undefined} retrying={busy} />}

      {!reviewed && (
        <Card>
          <p className="mb-4 text-sm text-[#9CA3AF]">
            Upload a prescription, lab report, or discharge summary (PDF / JPEG / PNG). Extraction
            runs automatically; you review and confirm before anything enters the record.
          </p>

          <div
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            className={`relative rounded-2xl border-2 border-dashed px-6 py-10 text-center transition-colors ${
              isDragging
                ? 'border-[#F0B429] bg-[#F0B429]/10'
                : 'border-[#2A2A2D] bg-[#111113] hover:border-[#F0B429]/30 hover:bg-[#F0B429]/5'
            } ${inputFocused ? 'ring-2 ring-[#F0B429] ring-offset-2 ring-offset-[#0A0A0B]' : ''}`}
          >
            {busy ? (
              <div className="flex flex-col items-center" role="status">
                <svg
                  aria-hidden="true"
                  className="h-8 w-8 animate-spin text-[#F0B429]"
                  viewBox="0 0 24 24"
                  fill="none"
                >
                  <circle
                    className="opacity-25"
                    cx="12"
                    cy="12"
                    r="10"
                    stroke="currentColor"
                    strokeWidth="4"
                  />
                  <path
                    className="opacity-75"
                    fill="currentColor"
                    d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
                  />
                </svg>
                <p className="mt-3 text-sm font-medium text-[#E5E7EB]">
                  Extracting document data...
                </p>
                <p className="mt-1 text-xs text-[#9CA3AF]">This may take a moment</p>
              </div>
            ) : (
              <>
                <svg
                  aria-hidden="true"
                  className="mx-auto h-10 w-10 text-[#6B7280]"
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
                <label
                  htmlFor="document-file"
                  className="mt-3 block text-sm font-medium text-[#E5E7EB]"
                >
                  Drag and drop a file here, or click to browse
                </label>
                <p id="document-file-hint" className="mt-1 text-xs text-[#9CA3AF]">
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
              <h2 className="text-lg font-semibold text-[#E5E7EB]">Review extraction</h2>
              <div className="flex items-center gap-2">
                {reviewed.extraction.ocr_fallback_used && (
                  <span className="inline-flex items-center rounded-full bg-amber-950/30 px-2 py-0.5 text-xs font-medium text-amber-400 ring-1 ring-inset ring-amber-800/50">
                    OCR fallback
                  </span>
                )}
                <span className="inline-flex items-center rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-[#9CA3AF] ring-1 ring-inset ring-slate-200">
                  {reviewed.extraction.document_type ?? 'unknown'}
                </span>
              </div>
            </div>
            <p className="mt-1 text-sm text-[#9CA3AF]">
              {reviewed.extraction.confirmation_required_count} field(s) need confirmation. Uncheck
              any entity to exclude it.
            </p>
            {/*
              An empty queue is not a document with nothing on it — it is a document nothing was
              read from, and the two look identical here. Left unsaid, the screen shows an empty
              list under an Approve button; the server refuses that approval, correctly, and the
              clinician is left with no move except uploading the same file again. The usual
              causes are transient and none of them are theirs to fix, so the remedy is on the
              screen with the diagnosis.
            */}
            {reviewed.extraction.entities.length === 0 && (
              <div
                role="alert"
                className="mt-2 rounded-lg bg-amber-950/30 px-3 py-2 text-sm text-amber-400 ring-1 ring-inset ring-amber-800/50"
              >
                <p>
                  Nothing could be read from this document, so there is nothing to approve into
                  the record. The file itself is stored and can still be opened. Try reading it
                  again, or enter the details by hand.
                </p>
                <Button
                  className="mt-2"
                  variant="secondary"
                  onClick={() => void rereadDocument(reviewed.docId)}
                  disabled={busy}
                  aria-busy={busy}
                >
                  {busy ? 'Reading…' : 'Read the document again'}
                </Button>
              </div>
            )}
            {/*
              The list below can only show what was read. Without this, a page whose drug line the
              scanner mangled reads as a complete, cleanly-extracted prescription — so the one
              thing the clinician needs to know is that the queue is short of the original.
              Phrased as what to do about it, since the remedy is to open the scan and compare.
            */}
            {(reviewed.extraction.unreadable_line_count ?? 0) > 0 && (
              <p
                role="alert"
                className="mt-2 rounded-lg bg-amber-950/30 px-3 py-2 text-sm text-amber-400 ring-1 ring-inset ring-amber-800/50"
              >
                {reviewed.extraction.unreadable_line_count} line(s) in a clinical section could not
                be read and are not listed below. Compare against the original before approving —
                anything missing here is missing from the chart.
              </p>
            )}
          </div>

          <ul className="space-y-3">
            {reviewed.extraction.entities.map((ent, i) => (
              <li
                key={i}
                className={`rounded-xl border p-4 transition-all ${
                  rejected.has(i)
                    ? 'border-[#2A2A2D] bg-[#111113]/80 opacity-50'
                    : 'border-[#2A2A2D] bg-[#1A1A1D] shadow-sm'
                }`}
              >
                <div className="mb-2 flex items-center justify-between">
                  <span className="inline-flex items-center rounded-md bg-[#F0B429]/10 px-2 py-0.5 text-xs font-semibold uppercase tracking-wider text-[#F0B429] ring-1 ring-inset ring-[#F0B429]/20">
                    {ent.entity_type}
                  </span>
                  {/* Every row's visible label reads "Include", so the accessible name has to
                      say which entity — otherwise the list is N identical checkboxes. */}
                  <label className="flex cursor-pointer items-center gap-1.5 text-xs text-[#9CA3AF] hover:text-[#E5E7EB]">
                    <input
                      type="checkbox"
                      checked={!rejected.has(i)}
                      onChange={() => toggleReject(i)}
                      aria-label={`Include ${ent.entity_type} ${i + 1} in the record`}
                      className="rounded border-slate-300 text-[#F0B429] focus:ring-brand-500"
                    />
                    Include
                  </label>
                </div>
                <div className="flex flex-wrap gap-x-4 gap-y-1.5 text-sm">
                  {ent.fields.map((f) => (
                    <span key={f.name} className="flex items-center gap-1">
                      <span className="text-[#9CA3AF]">{f.name}:</span>
                      <span className="font-medium text-[#E5E7EB]">{String(f.value)}</span>
                      <ConfidenceBadge band={f.confidence_band} value={f.confidence} />
                    </span>
                  ))}
                </div>
              </li>
            ))}
          </ul>

          <div className="mt-5 flex flex-col gap-2 border-t border-[#2A2A2D] pt-4 sm:flex-row">
            {/* Not rendered at all when there is nothing to merge: the server refuses that
                approval, and offering a button whose only outcome is an error reads as the
                system being broken rather than as the scan not having been read. */}
            {reviewed.extraction.entities.length > 0 && (
              <Button
                className="w-full sm:w-auto"
                onClick={() => void approve(reviewed.docId)}
                disabled={busy}
                aria-busy={busy}
              >
                {busy ? 'Saving…' : 'Confirm & merge into record'}
              </Button>
            )}
            <Link href={`/patients/${id}`} className="w-full sm:w-auto">
              <Button className="w-full sm:w-auto" variant="secondary">
                Discard
              </Button>
            </Link>
          </div>
        </Card>
      )}
    </div>
  );
}
