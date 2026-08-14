import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { PerformanceMetrics, SafetyReport, ValidationRun } from '@/lib/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      performanceMetrics: vi.fn(),
      listSafetyReports: vi.fn(),
      pilotStatus: vi.fn(),
      runValidation: vi.fn(),
      fileSafetyReport: vi.fn(),
      downloadSamdDossier: vi.fn(),
    },
  };
});

import { api, ApiError } from '@/lib/api';
import MetricsPage from './page';

function metrics(overrides: Partial<PerformanceMetrics> = {}): PerformanceMetrics {
  return {
    pilot_mode: false,
    total_sessions: 42,
    completed_sessions: 40,
    awaiting_review: 2,
    autonomy_tier_distribution: { suggestive: 30, flag_for_review: 10 },
    hard_blocks_total: 5,
    cant_miss_total: 8,
    verifier_disagreement_rate: 0.125,
    degraded_rate: 0.05,
    mean_citation_faithfulness: 0.964,
    citation_faithfulness_target: 0.95,
    open_safety_reports: 1,
    ...overrides,
  };
}

const REPORT: SafetyReport = {
  id: 'rep-1',
  category: 'missed_diagnosis',
  severity: 'sentinel_event',
  status: 'investigating',
  description: 'Sepsis not surfaced in the can’t-miss list.',
  patient_id: null,
  session_id: null,
  detail: {},
  created_at: '2026-01-01T00:00:00Z',
};

const RUN: ValidationRun = {
  id: 'run-1',
  vignette_count: 25,
  corpus_version: 'icmr-2024.1',
  metrics: { top1_accuracy: 0.72, cant_miss_recall: 0.96, vignettes_run: 25 },
  results: [],
  notes: null,
  created_at: '2026-01-01T00:00:00Z',
};

describe('MetricsPage', () => {
  beforeEach(() => {
    vi.mocked(api.performanceMetrics).mockReset().mockResolvedValue(metrics());
    vi.mocked(api.listSafetyReports).mockReset().mockResolvedValue([]);
    vi.mocked(api.pilotStatus).mockReset().mockResolvedValue({ pilot_mode: false, message: '' });
    vi.mocked(api.runValidation).mockReset().mockResolvedValue(RUN);
    vi.mocked(api.fileSafetyReport).mockReset().mockResolvedValue(REPORT);
    vi.mocked(api.downloadSamdDossier).mockReset().mockResolvedValue(undefined);
  });

  it('renders the headline safety counters once loaded', async () => {
    render(<MetricsPage />);

    expect(await screen.findByText('42')).toBeInTheDocument();
    expect(screen.getByText('Hard blocks')).toBeInTheDocument();
    expect(screen.getByText("Can't-miss flags")).toBeInTheDocument();
    expect(screen.getByText('Awaiting review')).toBeInTheDocument();
  });

  it('formats rate metrics as whole percentages', async () => {
    render(<MetricsPage />);
    await screen.findByText('42');

    expect(screen.getByText('13%')).toBeInTheDocument(); // verifier disagreement 0.125
    expect(screen.getByText('5%')).toBeInTheDocument(); // degraded rate
    expect(screen.getByText('96%')).toBeInTheDocument(); // citation faithfulness
  });

  it('renders an em dash rather than 0% when citation faithfulness is unmeasured', async () => {
    vi.mocked(api.performanceMetrics).mockResolvedValue(
      metrics({ mean_citation_faithfulness: null }),
    );
    render(<MetricsPage />);
    await screen.findByText('42');
    expect(screen.getByText('—')).toBeInTheDocument();
  });

  it('breaks sessions down by autonomy tier', async () => {
    render(<MetricsPage />);
    expect(await screen.findByText(/suggestive:/)).toBeInTheDocument();
    expect(screen.getByText(/flag for review:/)).toBeInTheDocument();
  });

  it('says so explicitly when no session has completed yet', async () => {
    vi.mocked(api.performanceMetrics).mockResolvedValue(
      metrics({ autonomy_tier_distribution: {} }),
    );
    render(<MetricsPage />);
    expect(await screen.findByText('No completed sessions yet.')).toBeInTheDocument();
  });

  it('shows the monitored-pilot banner only when the pilot is active', async () => {
    vi.mocked(api.pilotStatus).mockResolvedValue({
      pilot_mode: true,
      message: 'all outputs are being reviewed',
    });
    render(<MetricsPage />);
    expect(
      await screen.findByText(/Monitored pilot active — all outputs are being reviewed/),
    ).toBeInTheDocument();
  });

  it('treats an unavailable pilot-status endpoint as non-critical', async () => {
    vi.mocked(api.pilotStatus).mockRejectedValue(new ApiError(503, 'unavailable', 'nope'));
    render(<MetricsPage />);
    expect(await screen.findByText('42')).toBeInTheDocument();
    expect(screen.queryByText(/Monitored pilot active/)).not.toBeInTheDocument();
  });

  it('reports a metrics load failure instead of rendering an empty dashboard', async () => {
    vi.mocked(api.performanceMetrics).mockRejectedValue(
      new ApiError(500, 'server_error', 'metrics store unreachable'),
    );
    render(<MetricsPage />);
    expect(await screen.findByText('metrics store unreachable')).toBeInTheDocument();
  });

  it('runs the validation harness, shows its metrics, and refreshes the dashboard', async () => {
    const user = userEvent.setup();
    render(<MetricsPage />);
    await screen.findByText('42');

    await user.click(screen.getByRole('button', { name: 'Run validation harness' }));

    expect(await screen.findByText(/Latest validation run — 25 vignettes/)).toBeInTheDocument();
    expect(screen.getByText('72%')).toBeInTheDocument();
    expect(screen.getByText('25')).toBeInTheDocument(); // counts stay raw, not percentages
    expect(screen.getByText('Validation harness completed successfully.')).toBeInTheDocument();
    expect(api.performanceMetrics).toHaveBeenCalledTimes(2);
  });

  it('reports a validation-harness failure', async () => {
    vi.mocked(api.runValidation).mockRejectedValue(
      new ApiError(500, 'harness_error', 'vignette corpus missing'),
    );
    const user = userEvent.setup();
    render(<MetricsPage />);
    await screen.findByText('42');

    await user.click(screen.getByRole('button', { name: 'Run validation harness' }));
    expect(await screen.findByText('vignette corpus missing')).toBeInTheDocument();
  });

  it('downloads the CDSCO dossier in markdown', async () => {
    const user = userEvent.setup();
    render(<MetricsPage />);
    await screen.findByText('42');

    await user.click(screen.getByRole('button', { name: 'Download CDSCO dossier' }));

    await waitFor(() => expect(api.downloadSamdDossier).toHaveBeenCalledWith('markdown'));
    expect(await screen.findByText('Dossier download started.')).toBeInTheDocument();
  });

  it('reports a dossier download failure', async () => {
    vi.mocked(api.downloadSamdDossier).mockRejectedValue(new TypeError('Failed to fetch'));
    const user = userEvent.setup();
    render(<MetricsPage />);
    await screen.findByText('42');

    await user.click(screen.getByRole('button', { name: 'Download CDSCO dossier' }));
    expect(
      await screen.findByText(/Could not reach the server, so the regulatory dossier download/),
    ).toBeInTheDocument();
  });

  it('files a safety report with the selected category and severity, then refreshes', async () => {
    const user = userEvent.setup();
    render(<MetricsPage />);
    await screen.findByText('42');

    await user.selectOptions(screen.getByLabelText('Category'), 'missed_diagnosis');
    await user.selectOptions(screen.getByLabelText('Severity'), 'sentinel_event');
    await user.type(screen.getByLabelText('Description'), 'Sepsis was not flagged.');
    await user.click(screen.getByRole('button', { name: 'Submit report' }));

    await waitFor(() =>
      expect(api.fileSafetyReport).toHaveBeenCalledWith({
        category: 'missed_diagnosis',
        severity: 'sentinel_event',
        description: 'Sepsis was not flagged.',
      }),
    );
    expect(await screen.findByText('Safety report filed successfully.')).toBeInTheDocument();
    expect(screen.getByLabelText('Description')).toHaveValue('');
    expect(api.performanceMetrics).toHaveBeenCalledTimes(2);
  });

  it('will not submit an empty safety report', async () => {
    render(<MetricsPage />);
    await screen.findByText('42');
    expect(screen.getByRole('button', { name: 'Submit report' })).toBeDisabled();
  });

  it('reports a failure to file a safety report', async () => {
    vi.mocked(api.fileSafetyReport).mockRejectedValue(new TypeError('Failed to fetch'));
    const user = userEvent.setup();
    render(<MetricsPage />);
    await screen.findByText('42');

    await user.type(screen.getByLabelText('Description'), 'Something went wrong.');
    await user.click(screen.getByRole('button', { name: 'Submit report' }));

    expect(
      await screen.findByText(/Could not reach the server, so filing the safety report/),
    ).toBeInTheDocument();
  });

  it('lists filed safety reports with their severity and status', async () => {
    vi.mocked(api.listSafetyReports).mockResolvedValue([REPORT]);
    render(<MetricsPage />);

    const item = await screen.findByRole('listitem');
    expect(within(item).getByText('sentinel event')).toBeInTheDocument();
    expect(within(item).getByText('missed diagnosis')).toBeInTheDocument();
    expect(within(item).getByText('investigating')).toBeInTheDocument();
  });
});

describe('MetricsPage error and badge fallbacks', () => {
  beforeEach(() => {
    vi.mocked(api.performanceMetrics).mockReset().mockResolvedValue(metrics());
    vi.mocked(api.listSafetyReports).mockReset().mockResolvedValue([]);
    vi.mocked(api.pilotStatus).mockReset().mockResolvedValue({ pilot_mode: false, message: '' });
    vi.mocked(api.runValidation).mockReset().mockResolvedValue(RUN);
    vi.mocked(api.fileSafetyReport).mockReset().mockResolvedValue(REPORT);
    vi.mocked(api.downloadSamdDossier).mockReset().mockResolvedValue(undefined);
  });

  it('shows a generic message when the metrics load fails outside the API contract', async () => {
    vi.mocked(api.performanceMetrics).mockRejectedValue(new TypeError('Failed to fetch'));
    render(<MetricsPage />);
    expect(
      await screen.findByText(/Could not reach the server, so loading the metrics/),
    ).toBeInTheDocument();
  });

  it('shows a generic message when the validation harness fails outside the API contract', async () => {
    vi.mocked(api.runValidation).mockRejectedValue(new TypeError('Failed to fetch'));
    const user = userEvent.setup();
    render(<MetricsPage />);
    await screen.findByText('42');

    await user.click(screen.getByRole('button', { name: 'Run validation harness' }));
    expect(
      await screen.findByText(/Could not reach the server, so the validation run/),
    ).toBeInTheDocument();
  });

  it('surfaces the server message when the dossier download is refused', async () => {
    vi.mocked(api.downloadSamdDossier).mockRejectedValue(
      new ApiError(403, 'forbidden', 'Dossier export requires a regulatory role'),
    );
    const user = userEvent.setup();
    render(<MetricsPage />);
    await screen.findByText('42');

    await user.click(screen.getByRole('button', { name: 'Download CDSCO dossier' }));
    expect(
      await screen.findByText('Dossier export requires a regulatory role'),
    ).toBeInTheDocument();
  });

  it('surfaces the server message when a safety report is rejected', async () => {
    vi.mocked(api.fileSafetyReport).mockRejectedValue(
      new ApiError(422, 'invalid', 'Description must name the affected workflow'),
    );
    const user = userEvent.setup();
    render(<MetricsPage />);
    await screen.findByText('42');

    await user.type(screen.getByLabelText('Description'), 'Something went wrong.');
    await user.click(screen.getByRole('button', { name: 'Submit report' }));

    expect(
      await screen.findByText('Description must name the affected workflow'),
    ).toBeInTheDocument();
  });

  it('drops a whitespace-only report even if the form is submitted directly', async () => {
    // The button is disabled for blank input, but implicit form submission can bypass it, so
    // the handler keeps its own guard. Nothing should reach the safety-report endpoint.
    const user = userEvent.setup();
    const { container } = render(<MetricsPage />);
    await screen.findByText('42');

    await user.type(screen.getByLabelText('Description'), '   ');
    const form = screen.getByLabelText('Description').closest('form');
    expect(form).not.toBeNull();
    fireEvent.submit(form as HTMLFormElement);

    await waitFor(() => expect(container).toBeTruthy());
    expect(api.fileSafetyReport).not.toHaveBeenCalled();
  });

  it('styles safety reports whose severity and status are outside the known sets', async () => {
    // These come from the backend enum; the badges must render new values rather than crash
    // or show an unstyled fragment.
    vi.mocked(api.listSafetyReports).mockResolvedValue([
      { ...REPORT, severity: 'catastrophic', status: 'escalated' },
    ]);
    render(<MetricsPage />);

    const item = await screen.findByRole('listitem');
    expect(within(item).getByText('catastrophic')).toBeInTheDocument();
    expect(within(item).getByText('escalated')).toBeInTheDocument();
  });
});

describe('MetricsPage recovery', () => {
  beforeEach(() => {
    vi.mocked(api.performanceMetrics).mockReset();
    vi.mocked(api.listSafetyReports).mockReset().mockResolvedValue([]);
    vi.mocked(api.pilotStatus).mockReset().mockResolvedValue({ pilot_mode: false, message: '' });
    vi.mocked(api.runValidation).mockReset().mockResolvedValue(RUN);
    vi.mocked(api.fileSafetyReport).mockReset().mockResolvedValue(REPORT);
    vi.mocked(api.downloadSamdDossier).mockReset().mockResolvedValue(undefined);
  });

  it('offers a retry when the metrics could not be read', async () => {
    // A metrics read changes nothing, so a retry here is unambiguously safe.
    vi.mocked(api.performanceMetrics)
      .mockRejectedValueOnce(new ApiError(503, 'unavailable', 'Briefly unavailable.'))
      .mockResolvedValue(metrics());
    render(<MetricsPage />);
    await screen.findByRole('alert');

    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));

    expect(await screen.findByText('42')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('offers no retry on a failed safety report, which is a write', async () => {
    // Re-submitting on one button press is how a single incident becomes two rows in the
    // register — and the register is a regulatory record, not a scratchpad.
    vi.mocked(api.performanceMetrics).mockResolvedValue(metrics());
    vi.mocked(api.fileSafetyReport).mockRejectedValue(
      new ApiError(500, 'internal_error', 'That did not complete.'),
    );
    const user = userEvent.setup();
    render(<MetricsPage />);
    await screen.findByText('42');

    await user.type(screen.getByLabelText('Description'), 'A near miss.');
    await user.click(screen.getByRole('button', { name: 'Submit report' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('That did not complete.');
    expect(within(alert).queryByRole('button')).not.toBeInTheDocument();
  });

  it('offers no retry on a failed validation run, which is also a write', async () => {
    vi.mocked(api.performanceMetrics).mockResolvedValue(metrics());
    vi.mocked(api.runValidation).mockRejectedValue(
      new ApiError(500, 'internal_error', 'The harness did not finish.'),
    );
    const user = userEvent.setup();
    render(<MetricsPage />);
    await screen.findByText('42');

    await user.click(screen.getByRole('button', { name: 'Run validation harness' }));

    const alert = await screen.findByRole('alert');
    expect(within(alert).queryByRole('button')).not.toBeInTheDocument();
  });

  it('names the metrics it is waiting on while the first read is in flight', async () => {
    vi.mocked(api.performanceMetrics).mockResolvedValue(metrics());
    render(<MetricsPage />);

    expect(
      screen.getByRole('status', { name: 'Loading performance metrics' }),
    ).toHaveAttribute('aria-busy', 'true');

    await screen.findByText('42');
  });
});
