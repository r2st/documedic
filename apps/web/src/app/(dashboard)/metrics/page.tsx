'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api';
import { requestErrorMessage } from '@/lib/errors';
import type { PerformanceMetrics, SafetyReport, ValidationRun } from '@/lib/types';
import { Button, Card, ErrorBanner } from '@/components/ui';
import { LoadingBlock, Skeleton } from '@/components/Skeleton';

// `as const` (rather than Record<string, string>) makes the key set a closed union, so
// TypeScript rejects an unknown metric label at the call site instead of silently
// falling back to a default colour at runtime.
const METRIC_COLORS = {
  Sessions: 'border-t-brand-500',
  'Hard blocks': 'border-t-red-500',
  "Can't-miss flags": 'border-t-amber-500',
  'Awaiting review': 'border-t-purple-500',
  'Verifier disagreement': 'border-t-orange-500',
  'Degraded rate': 'border-t-slate-400',
  'Citation faithfulness': 'border-t-emerald-500',
  'Open safety reports': 'border-t-rose-500',
} as const;

function Metric({ label, value }: { label: keyof typeof METRIC_COLORS; value: string | number }) {
  return (
    <Card className={`border-t-4 ${METRIC_COLORS[label]} text-center`}>
      <p className="text-3xl font-bold tracking-tight text-slate-900">{value}</p>
      <p className="mt-1 text-xs uppercase tracking-wide text-slate-500">{label}</p>
    </Card>
  );
}

function SeverityBadge({ severity }: { severity: string }) {
  const styles: Record<string, string> = {
    sentinel_event: 'bg-red-100 text-red-800 ring-1 ring-inset ring-red-200',
    serious: 'bg-orange-100 text-orange-800 ring-1 ring-inset ring-orange-200',
    non_serious: 'bg-yellow-100 text-yellow-800 ring-1 ring-inset ring-yellow-200',
    near_miss: 'bg-slate-100 text-slate-700 ring-1 ring-inset ring-slate-200',
  };
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${styles[severity] || styles.near_miss}`}
    >
      {severity.replace(/_/g, ' ')}
    </span>
  );
}

function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    open: 'bg-blue-100 text-blue-700 ring-1 ring-inset ring-blue-200',
    investigating: 'bg-amber-100 text-amber-700 ring-1 ring-inset ring-amber-200',
    resolved: 'bg-green-100 text-green-700 ring-1 ring-inset ring-green-200',
    closed: 'bg-slate-100 text-slate-600 ring-1 ring-inset ring-slate-200',
  };
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${styles[status] || styles.open}`}
    >
      {status}
    </span>
  );
}

export default function MetricsPage() {
  const [metrics, setMetrics] = useState<PerformanceMetrics | null>(null);
  const [pilot, setPilot] = useState<{ pilot_mode: boolean; message: string } | null>(null);
  const [run, setRun] = useState<ValidationRun | null>(null);
  const [reports, setReports] = useState<SafetyReport[]>([]);
  const [running, setRunning] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionSuccess, setActionSuccess] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  function clearFeedback() {
    setActionError(null);
    setActionSuccess(null);
  }

  async function downloadDossier() {
    clearFeedback();
    setDownloading(true);
    try {
      await api.downloadSamdDossier('markdown');
      setActionSuccess('Dossier download started.');
    } catch (err) {
      setActionError(requestErrorMessage(err, 'the regulatory dossier download'));
    } finally {
      setDownloading(false);
    }
  }

  async function refresh() {
    setLoadError(null);
    setRefreshing(true);
    try {
      const [metricsData, reportsData] = await Promise.all([
        api.performanceMetrics(),
        api.listSafetyReports(),
      ]);
      setMetrics(metricsData);
      setReports(reportsData);
    } catch (err) {
      setLoadError(requestErrorMessage(err, 'loading the metrics'));
    } finally {
      setInitialLoading(false);
      setRefreshing(false);
    }
  }

  useEffect(() => {
    void refresh();
    void api
      .pilotStatus()
      .then(setPilot)
      .catch(() => {
        /* pilot status is non-critical; ignore */
      });
  }, []);

  async function runValidation() {
    clearFeedback();
    setRunning(true);
    try {
      setRun(await api.runValidation());
      await refresh();
      setActionSuccess('Validation harness completed successfully.');
    } catch (err) {
      setActionError(requestErrorMessage(err, 'the validation run'));
    } finally {
      setRunning(false);
    }
  }

  const pct = (v: number | null | undefined) => (v == null ? '—' : `${(v * 100).toFixed(0)}%`);

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <h1 className="text-2xl font-bold tracking-tight text-slate-900">
          Performance & validation
        </h1>
        <div className="flex flex-col gap-2 sm:flex-row">
          <Button
            className="w-full sm:w-auto"
            variant="secondary"
            onClick={() => void downloadDossier()}
            disabled={downloading}
          >
            {downloading ? 'Downloading…' : 'Download CDSCO dossier'}
          </Button>
          <Button
            className="w-full sm:w-auto"
            onClick={() => void runValidation()}
            disabled={running}
          >
            {running ? 'Running…' : 'Run validation harness'}
          </Button>
        </div>
      </div>

      {/* A metrics read changes nothing, so a retry is unambiguously safe here — unlike the
          action errors below, where re-running a validation harness or a dossier download is
          the clinician's call rather than a button we should offer twice. */}
      {loadError && (
        <ErrorBanner message={loadError} onRetry={() => void refresh()} retrying={refreshing} />
      )}

      {actionError && <ErrorBanner message={actionError} />}

      {actionSuccess && (
        <div className="flex items-start gap-2 rounded-lg bg-green-50 p-3 text-sm text-green-700 ring-1 ring-green-200">
          <svg
            aria-hidden="true"
            className="mt-0.5 h-4 w-4 flex-shrink-0"
            fill="none"
            viewBox="0 0 24 24"
            strokeWidth={2}
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
            />
          </svg>
          <span>{actionSuccess}</span>
        </div>
      )}

      {pilot?.pilot_mode && (
        <div className="flex items-start gap-2 rounded-lg border border-purple-200 bg-purple-50 p-3 text-sm text-purple-900">
          <svg
            aria-hidden="true"
            className="mt-0.5 h-4 w-4 flex-shrink-0"
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
          <span>Monitored pilot active — {pilot.message}</span>
        </div>
      )}

      {initialLoading && (
        <LoadingBlock
          label="Loading performance metrics"
          className="grid grid-cols-2 gap-3 sm:grid-cols-4"
        >
          {Array.from({ length: 8 }).map((_, i) => (
            <Card key={i} className="border-t-4 border-t-slate-200 text-center">
              <Skeleton className="mx-auto mb-2 h-8 w-20 bg-slate-200" />
              <Skeleton className="mx-auto h-3 w-24" />
            </Card>
          ))}
        </LoadingBlock>
      )}

      {metrics && (
        <div className="animate-fade-in">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Metric label="Sessions" value={metrics.total_sessions} />
            <Metric label="Hard blocks" value={metrics.hard_blocks_total} />
            <Metric label="Can't-miss flags" value={metrics.cant_miss_total} />
            <Metric label="Awaiting review" value={metrics.awaiting_review} />
            <Metric label="Verifier disagreement" value={pct(metrics.verifier_disagreement_rate)} />
            <Metric label="Degraded rate" value={pct(metrics.degraded_rate)} />
            <Metric label="Citation faithfulness" value={pct(metrics.mean_citation_faithfulness)} />
            <Metric label="Open safety reports" value={metrics.open_safety_reports} />
          </div>

          <Card className="mt-4">
            <p className="mb-2 text-sm font-semibold text-slate-900">Autonomy tier distribution</p>
            <div className="flex flex-wrap gap-2 text-sm">
              {Object.entries(metrics.autonomy_tier_distribution).map(([tier, n]) => (
                <span
                  key={tier}
                  className="inline-flex items-center rounded-full bg-slate-100 px-3 py-1 text-xs font-medium text-slate-700 ring-1 ring-inset ring-slate-200"
                >
                  {tier.replace(/_/g, ' ')}: <b className="ml-1">{n}</b>
                </span>
              ))}
              {Object.keys(metrics.autonomy_tier_distribution).length === 0 && (
                <span className="text-sm text-slate-400">No completed sessions yet.</span>
              )}
            </div>
          </Card>
        </div>
      )}

      {run && (
        <Card className="animate-slide-up">
          <p className="mb-3 text-sm font-semibold text-slate-900">
            Latest validation run — {run.vignette_count} vignettes
          </p>
          <div className="grid grid-cols-2 gap-2 text-sm sm:grid-cols-3">
            {Object.entries(run.metrics)
              .filter(([, v]) => typeof v === 'number')
              .map(([k, v]) => (
                <div key={k} className="rounded-lg bg-slate-50 p-3 ring-1 ring-slate-100">
                  <p className="text-xs font-medium text-slate-500">{k.replace(/_/g, ' ')}</p>
                  <p className="mt-0.5 text-lg font-semibold text-slate-900">
                    {typeof v === 'number' && v <= 1 ? pct(v as number) : String(v)}
                  </p>
                </div>
              ))}
          </div>
        </Card>
      )}

      <SafetyReportForm onFiled={() => void refresh()} />

      {reports.length > 0 && (
        <Card>
          <p className="mb-3 text-sm font-semibold text-slate-900">Safety reports</p>
          <ul className="divide-y divide-slate-100 text-sm">
            {reports.map((r) => (
              <li
                key={r.id}
                className="flex flex-col gap-2 py-3 sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <SeverityBadge severity={r.severity} />
                  <span className="inline-flex items-center rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600">
                    {r.category.replace(/_/g, ' ')}
                  </span>
                  <span className="min-w-0 break-words text-slate-700">{r.description}</span>
                </div>
                <StatusBadge status={r.status} />
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}

function SafetyReportForm({ onFiled }: { onFiled: () => void }) {
  const [category, setCategory] = useState('incorrect_suggestion');
  const [severity, setSeverity] = useState('near_miss');
  const [description, setDescription] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!description.trim()) return;
    setError(null);
    setSuccess(null);
    setSubmitting(true);
    try {
      await api.fileSafetyReport({ category, severity, description });
      setDescription('');
      setSuccess('Safety report filed successfully.');
      onFiled();
    } catch (err) {
      setError(requestErrorMessage(err, 'filing the safety report'));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card>
      <p className="mb-4 text-sm font-semibold text-slate-900">File a safety report</p>
      <form onSubmit={submit} className="space-y-4">
        {/* No retry: this is a write, and resubmitting a safety report on one button press is
            how a single incident becomes two rows in the register. */}
        {error && <ErrorBanner message={error} />}
        {success && (
          <div className="flex items-start gap-2 rounded-lg bg-green-50 p-3 text-sm text-green-700 ring-1 ring-green-200">
            <svg
              aria-hidden="true"
              className="mt-0.5 h-4 w-4 flex-shrink-0"
              fill="none"
              viewBox="0 0 24 24"
              strokeWidth={2}
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
              />
            </svg>
            <span>{success}</span>
          </div>
        )}
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <label
              htmlFor="report-category"
              className="mb-1.5 block text-sm font-medium text-slate-700"
            >
              Category
            </label>
            <select
              id="report-category"
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              className="w-full"
            >
              <option value="incorrect_suggestion">Incorrect suggestion</option>
              <option value="missed_diagnosis">Missed diagnosis</option>
              <option value="safety_check_failure">Safety-check failure</option>
              <option value="usability">Usability</option>
              <option value="other">Other</option>
            </select>
          </div>
          <div>
            <label
              htmlFor="report-severity"
              className="mb-1.5 block text-sm font-medium text-slate-700"
            >
              Severity
            </label>
            <select
              id="report-severity"
              value={severity}
              onChange={(e) => setSeverity(e.target.value)}
              className="w-full"
            >
              <option value="near_miss">Near miss</option>
              <option value="non_serious">Non-serious</option>
              <option value="serious">Serious</option>
              <option value="sentinel_event">Sentinel event</option>
            </select>
          </div>
        </div>
        <div>
          <label
            htmlFor="report-description"
            className="mb-1.5 block text-sm font-medium text-slate-700"
          >
            Description
          </label>
          <textarea
            id="report-description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Describe what happened (no patient identifiers)."
            rows={3}
            className="w-full"
          />
        </div>
        <Button type="submit" disabled={!description.trim() || submitting}>
          {submitting ? 'Submitting...' : 'Submit report'}
        </Button>
      </form>
    </Card>
  );
}
