import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Citation } from '@/lib/types';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    api: { ...actual.api, corpusInfo: vi.fn(), searchGuidelines: vi.fn() },
  };
});

import { api } from '@/lib/api';
import GuidelinesPage from './page';

const CORPUS = {
  corpus_version: 'icmr-2024.1',
  chunk_count: 412,
  retrieval_threshold: 0.35,
  citation_faithfulness_target: 0.95,
};

function citation(overrides: Partial<Citation> = {}): Citation {
  return {
    section_id: 'ICMR-STW-DENGUE-3.2',
    source: 'ICMR',
    document_title: 'Standard Treatment Workflow: Dengue',
    heading: 'Fluid management in dengue with warning signs',
    snippet: 'Isotonic crystalloid at 5–7 mL/kg/hr is the recommended starting rate.',
    score: 0.87,
    page_range: '12-13',
    ...overrides,
  };
}

describe('GuidelinesPage', () => {
  beforeEach(() => {
    vi.mocked(api.corpusInfo).mockReset().mockResolvedValue(CORPUS);
    vi.mocked(api.searchGuidelines).mockReset().mockResolvedValue([]);
  });

  it('reports corpus provenance once loaded', async () => {
    render(<GuidelinesPage />);
    expect(await screen.findByText(/412 chunks · corpus icmr-2024\.1/)).toBeInTheDocument();
  });

  it('shows the pre-search prompt rather than a false "no results"', async () => {
    render(<GuidelinesPage />);
    expect(await screen.findByText('Search the guideline corpus')).toBeInTheDocument();
    expect(screen.queryByText('No results found')).not.toBeInTheDocument();
  });

  it('renders retrieved citations with source, section, pages, and match score', async () => {
    vi.mocked(api.searchGuidelines).mockResolvedValue([citation()]);
    const user = userEvent.setup();
    render(<GuidelinesPage />);

    await user.type(screen.getByPlaceholderText(/Search guidelines/), 'dengue fluids');
    await user.click(screen.getByRole('button', { name: 'Search' }));

    expect(await screen.findByText('Standard Treatment Workflow: Dengue')).toBeInTheDocument();
    expect(screen.getByText('ICMR')).toBeInTheDocument();
    expect(screen.getByText(/ICMR-STW-DENGUE-3\.2 · pp\. 12-13/)).toBeInTheDocument();
    expect(screen.getByText('87% match')).toBeInTheDocument();
    expect(api.searchGuidelines).toHaveBeenCalledWith('dengue fluids');
  });

  it('trims whitespace before querying so padded input still retrieves', async () => {
    vi.mocked(api.searchGuidelines).mockResolvedValue([citation()]);
    const user = userEvent.setup();
    render(<GuidelinesPage />);

    await user.type(screen.getByPlaceholderText(/Search guidelines/), '  dengue  ');
    await user.click(screen.getByRole('button', { name: 'Search' }));

    await waitFor(() => expect(api.searchGuidelines).toHaveBeenCalledWith('dengue'));
  });

  it('does not fire a retrieval for a query below the minimum length', async () => {
    const user = userEvent.setup();
    render(<GuidelinesPage />);

    await user.type(screen.getByPlaceholderText(/Search guidelines/), 'a');
    await user.click(screen.getByRole('button', { name: 'Search' }));

    expect(api.searchGuidelines).not.toHaveBeenCalled();
    expect(screen.getByText('Search the guideline corpus')).toBeInTheDocument();
  });

  it('distinguishes an empty result set from the initial state', async () => {
    const user = userEvent.setup();
    render(<GuidelinesPage />);

    await user.type(screen.getByPlaceholderText(/Search guidelines/), 'nonexistent condition');
    await user.click(screen.getByRole('button', { name: 'Search' }));

    expect(await screen.findByText('No results found')).toBeInTheDocument();
    expect(screen.queryByText('Search the guideline corpus')).not.toBeInTheDocument();
  });

  it('omits the page range and score when the retriever does not supply them', async () => {
    vi.mocked(api.searchGuidelines).mockResolvedValue([
      citation({ page_range: null, score: null, heading: null, snippet: null }),
    ]);
    const user = userEvent.setup();
    render(<GuidelinesPage />);

    await user.type(screen.getByPlaceholderText(/Search guidelines/), 'dengue');
    await user.click(screen.getByRole('button', { name: 'Search' }));

    expect(await screen.findByText('ICMR-STW-DENGUE-3.2')).toBeInTheDocument();
    expect(screen.queryByText(/% match/)).not.toBeInTheDocument();
  });
});
