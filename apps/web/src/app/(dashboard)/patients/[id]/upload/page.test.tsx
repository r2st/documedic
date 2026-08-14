import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { DocumentResponse, ExtractionResult } from '@/lib/types';

const push = vi.fn();
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push, replace: vi.fn(), refresh: vi.fn(), back: vi.fn() }),
}));

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      uploadDocument: vi.fn(),
      getExtraction: vi.fn(),
      approveExtraction: vi.fn(),
    },
  };
});

import { api, ApiError } from '@/lib/api';
import UploadPage from './page';

const DOC: DocumentResponse = {
  id: 'doc-1',
  patient_id: 'pat-1',
  file_name: 'prescription.pdf',
  file_type: 'application/pdf',
  file_size_bytes: 1024,
  document_type: 'prescription',
  extraction_status: 'completed',
  ocr_fallback_used: false,
  created_at: '2026-01-01T00:00:00Z',
};

function extraction(overrides: Partial<ExtractionResult> = {}): ExtractionResult {
  return {
    document_id: 'doc-1',
    document_type: 'prescription',
    model: 'anthropic/claude-sonnet-4',
    ocr_fallback_used: false,
    confirmation_required_count: 1,
    entities: [
      {
        entity_type: 'medication',
        region: null,
        fields: [
          { name: 'brand_name', value: 'Crocin', confidence: 0.94, confidence_band: 'high', needs_confirmation: false },
          { name: 'dose', value: '500mg', confidence: 0.55, confidence_band: 'low', needs_confirmation: true },
        ],
      },
      {
        entity_type: 'lab_result',
        region: null,
        fields: [
          { name: 'marker_name', value: 'HbA1c', confidence: 0.88, confidence_band: 'high', needs_confirmation: false },
        ],
      },
    ],
    ...overrides,
  };
}

function fileInput(container: HTMLElement): HTMLInputElement {
  const input = container.querySelector('input[type="file"]');
  if (!input) throw new Error('file input not rendered');
  return input as HTMLInputElement;
}

const PDF = new File(['%PDF-1.4'], 'prescription.pdf', { type: 'application/pdf' });

describe('UploadPage', () => {
  beforeEach(() => {
    push.mockReset();
    vi.mocked(api.uploadDocument).mockReset().mockResolvedValue(DOC);
    vi.mocked(api.getExtraction).mockReset().mockResolvedValue(extraction());
    vi.mocked(api.approveExtraction).mockReset().mockResolvedValue({ merged: {} });
  });

  it('explains that nothing enters the record before clinician review', () => {
    render(<UploadPage params={{ id: 'pat-1' }} />);
    expect(screen.getByText(/you review and confirm before anything enters the record/)).toBeInTheDocument();
  });

  it('uploads the chosen file and then fetches its extraction', async () => {
    const user = userEvent.setup();
    const { container } = render(<UploadPage params={{ id: 'pat-1' }} />);

    await user.upload(fileInput(container), PDF);

    await waitFor(() => expect(api.uploadDocument).toHaveBeenCalledWith('pat-1', PDF));
    expect(api.getExtraction).toHaveBeenCalledWith('pat-1', 'doc-1');
    expect(await screen.findByText('Review extraction')).toBeInTheDocument();
  });

  it('accepts a dropped file as well as a browsed one', async () => {
    const { container } = render(<UploadPage params={{ id: 'pat-1' }} />);
    const dropZone = container.querySelector('.border-dashed') as HTMLElement;

    fireEvent.dragOver(dropZone);
    fireEvent.drop(dropZone, { dataTransfer: { files: [PDF] } });

    await waitFor(() => expect(api.uploadDocument).toHaveBeenCalledWith('pat-1', PDF));
  });

  it('ignores a drop that carries no file', async () => {
    const { container } = render(<UploadPage params={{ id: 'pat-1' }} />);
    const dropZone = container.querySelector('.border-dashed') as HTMLElement;

    fireEvent.dragOver(dropZone);
    fireEvent.dragLeave(dropZone);
    fireEvent.drop(dropZone, { dataTransfer: { files: [] } });

    expect(api.uploadDocument).not.toHaveBeenCalled();
  });

  it('renders every extracted field with its confidence band', async () => {
    const user = userEvent.setup();
    const { container } = render(<UploadPage params={{ id: 'pat-1' }} />);
    await user.upload(fileInput(container), PDF);

    expect(await screen.findByText('Crocin')).toBeInTheDocument();
    expect(screen.getByText('HbA1c')).toBeInTheDocument();
    expect(screen.getByText('94%')).toBeInTheDocument();
    expect(screen.getByText('55%')).toBeInTheDocument();
    expect(screen.getByText(/1 field\(s\) need confirmation/)).toBeInTheDocument();
  });

  it('flags when Tesseract OCR fallback produced the extraction', async () => {
    vi.mocked(api.getExtraction).mockResolvedValue(
      extraction({ ocr_fallback_used: true, document_type: null }),
    );
    const user = userEvent.setup();
    const { container } = render(<UploadPage params={{ id: 'pat-1' }} />);
    await user.upload(fileInput(container), PDF);

    expect(await screen.findByText('OCR fallback')).toBeInTheDocument();
    expect(screen.getByText('unknown')).toBeInTheDocument();
  });

  it('excludes de-selected entities by index when merging into the record', async () => {
    const user = userEvent.setup();
    const { container } = render(<UploadPage params={{ id: 'pat-1' }} />);
    await user.upload(fileInput(container), PDF);
    await screen.findByText('Review extraction');

    // Drop the second entity, keep the first.
    await user.click(screen.getAllByRole('checkbox')[1]);
    await user.click(screen.getByRole('button', { name: /Confirm & merge/ }));

    await waitFor(() =>
      expect(api.approveExtraction).toHaveBeenCalledWith('pat-1', 'doc-1', [1]),
    );
    expect(push).toHaveBeenCalledWith('/patients/pat-1');
  });

  it('re-includes an entity when its checkbox is toggled back on', async () => {
    const user = userEvent.setup();
    const { container } = render(<UploadPage params={{ id: 'pat-1' }} />);
    await user.upload(fileInput(container), PDF);
    await screen.findByText('Review extraction');

    const checkbox = screen.getAllByRole('checkbox')[0];
    await user.click(checkbox);
    await user.click(checkbox);
    await user.click(screen.getByRole('button', { name: /Confirm & merge/ }));

    await waitFor(() => expect(api.approveExtraction).toHaveBeenCalledWith('pat-1', 'doc-1', []));
  });

  it('surfaces upload failures without entering the review step', async () => {
    vi.mocked(api.uploadDocument).mockRejectedValue(
      new ApiError(413, 'file_too_large', 'File exceeds the 10 MB limit'),
    );
    const user = userEvent.setup();
    const { container } = render(<UploadPage params={{ id: 'pat-1' }} />);

    await user.upload(fileInput(container), PDF);

    expect(await screen.findByText('File exceeds the 10 MB limit')).toBeInTheDocument();
    expect(screen.queryByText('Review extraction')).not.toBeInTheDocument();
  });

  it('keeps the review open and reports the error when approval fails', async () => {
    vi.mocked(api.approveExtraction).mockRejectedValue(
      new ApiError(409, 'conflict', 'Document already merged'),
    );
    const user = userEvent.setup();
    const { container } = render(<UploadPage params={{ id: 'pat-1' }} />);
    await user.upload(fileInput(container), PDF);
    await screen.findByText('Review extraction');

    await user.click(screen.getByRole('button', { name: /Confirm & merge/ }));

    expect(await screen.findByText('Document already merged')).toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
    expect(screen.getByText('Review extraction')).toBeInTheDocument();
  });

  it('clears a failed approval before retrying it', async () => {
    vi.mocked(api.approveExtraction)
      .mockRejectedValueOnce(new ApiError(409, 'conflict', 'Document already merged'))
      .mockResolvedValueOnce({ merged: {} });
    const user = userEvent.setup();
    const { container } = render(<UploadPage params={{ id: 'pat-1' }} />);
    await user.upload(fileInput(container), PDF);
    await screen.findByText('Review extraction');

    await user.click(screen.getByRole('button', { name: /Confirm & merge/ }));
    expect(await screen.findByText('Document already merged')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /Confirm & merge/ }));

    // The banner tells the clinician the approval "may not have completed" and to reload before
    // retrying. Left up during the retry it describes the previous attempt as the current one.
    await waitFor(() => expect(push).toHaveBeenCalledWith('/patients/pat-1'));
    expect(screen.queryByText('Document already merged')).not.toBeInTheDocument();
  });

  it('falls back to a generic message for non-API upload failures', async () => {
    vi.mocked(api.uploadDocument).mockRejectedValue(new TypeError('Failed to fetch'));
    const user = userEvent.setup();
    const { container } = render(<UploadPage params={{ id: 'pat-1' }} />);

    await user.upload(fileInput(container), PDF);

    expect(
      await screen.findByText(/Could not reach the server, so the upload may not have completed/),
    ).toBeInTheDocument();
  });
});

describe('UploadPage approval guards', () => {
  beforeEach(() => {
    push.mockReset();
    vi.mocked(api.uploadDocument).mockReset().mockResolvedValue(DOC);
    vi.mocked(api.getExtraction).mockReset().mockResolvedValue(extraction());
    vi.mocked(api.approveExtraction).mockReset();
  });

  it('reports a generic message when approval fails outside the API contract', async () => {
    vi.mocked(api.approveExtraction).mockRejectedValue(new TypeError('Failed to fetch'));
    const user = userEvent.setup();
    const { container } = render(<UploadPage params={{ id: 'pat-1' }} />);

    await user.upload(fileInput(container), PDF);
    await screen.findByText('Review extraction');
    await user.click(screen.getByRole('button', { name: /Confirm & merge/ }));

    expect(
      await screen.findByText(/Could not reach the server, so the approval of these extracted details/),
    ).toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
  });
});
