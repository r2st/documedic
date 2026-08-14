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

describe('GuidelinesPage when a search fails', () => {
  beforeEach(() => {
    vi.mocked(api.corpusInfo).mockReset().mockResolvedValue(CORPUS);
    vi.mocked(api.searchGuidelines).mockReset();
  });

  async function searchFor(term: string) {
    const user = userEvent.setup();
    await user.clear(screen.getByLabelText(/Search the clinical guideline corpus/i));
    await user.type(screen.getByLabelText(/Search the clinical guideline corpus/i), term);
    await user.click(screen.getByRole('button', { name: 'Search' }));
    return user;
  }

  it('says the search failed instead of leaving the rejection unhandled', async () => {
    vi.mocked(api.searchGuidelines).mockRejectedValue(new Error('network down'));
    render(<GuidelinesPage />);

    await searchFor('dengue');

    expect(await screen.findByRole('alert')).toHaveTextContent(/could not/i);
  });

  it('clears the previous query’s citations rather than showing them under the new query', async () => {
    // The dangerous shape of an unhandled rejection here: the results list kept rendering the
    // *previous* search's citations while the box named a different condition. Guidance for
    // one condition read under the heading of another is exactly the mix-up a cited corpus
    // exists to prevent.
    vi.mocked(api.searchGuidelines)
      .mockResolvedValueOnce([citation()])
      .mockRejectedValue(new Error('network down'));
    render(<GuidelinesPage />);

    await searchFor('dengue');
    expect(await screen.findByText(/Standard Treatment Workflow: Dengue/)).toBeInTheDocument();

    await searchFor('malaria');

    await screen.findByRole('alert');
    expect(screen.queryByText(/Standard Treatment Workflow: Dengue/)).not.toBeInTheDocument();
  });

  it('does not claim "no results found" for a search that never completed', async () => {
    // "No results" is a statement about the corpus. A failed search supports no such claim,
    // and here the difference is between "no guidance exists for this" and "we could not look".
    vi.mocked(api.searchGuidelines).mockRejectedValue(new Error('network down'));
    render(<GuidelinesPage />);

    await searchFor('dengue');

    await screen.findByRole('alert');
    expect(screen.queryByText('No results found')).not.toBeInTheDocument();
  });

  it('offers a retry that re-runs the same query', async () => {
    vi.mocked(api.searchGuidelines)
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValue([citation()]);
    render(<GuidelinesPage />);

    const user = await searchFor('dengue');
    await screen.findByRole('alert');

    await user.click(screen.getByRole('button', { name: 'Try again' }));

    expect(await screen.findByText(/Standard Treatment Workflow: Dengue/)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(api.searchGuidelines).toHaveBeenLastCalledWith('dengue');
  });

  it('announces the search while it is in flight', async () => {
    let release: (value: Citation[]) => void = () => {};
    vi.mocked(api.searchGuidelines).mockReturnValue(
      new Promise<Citation[]>((resolve) => {
        release = resolve;
      }),
    );
    render(<GuidelinesPage />);

    await searchFor('dengue');

    expect(
      screen.getByRole('status', { name: 'Searching the guideline corpus' }),
    ).toBeInTheDocument();

    release([citation()]);
    expect(await screen.findByText(/Standard Treatment Workflow: Dengue/)).toBeInTheDocument();
  });
});

describe('GuidelinesPage when the corpus header cannot be read', () => {
  beforeEach(() => {
    vi.mocked(api.corpusInfo).mockReset().mockRejectedValue(new Error('qdrant unreachable'));
    vi.mocked(api.searchGuidelines).mockReset().mockResolvedValue([]);
  });

  it('says the details are unavailable rather than showing nothing at all', async () => {
    // Downgraded to a line, not a banner: not knowing the corpus version stops nobody
    // searching. It is still said, because "no version shown" and "version withheld" are
    // indistinguishable to the reader otherwise.
    render(<GuidelinesPage />);

    expect(await screen.findByText(/Corpus details are unavailable/)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('leaves the search itself working', async () => {
    vi.mocked(api.searchGuidelines).mockResolvedValue([citation()]);
    const user = userEvent.setup();
    render(<GuidelinesPage />);
    await screen.findByText(/Corpus details are unavailable/);

    await user.type(screen.getByLabelText(/Search the clinical guideline corpus/i), 'dengue');
    await user.click(screen.getByRole('button', { name: 'Search' }));

    expect(await screen.findByText(/Standard Treatment Workflow: Dengue/)).toBeInTheDocument();
  });
});
