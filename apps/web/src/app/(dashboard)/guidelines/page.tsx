'use client';

import { useCallback, useEffect, useState } from 'react';
import { api } from '@/lib/api';
import { requestErrorMessage } from '@/lib/errors';
import type { Citation } from '@/lib/types';
import { Button, Card, ErrorBanner } from '@/components/ui';
import { LoadingBlock, SkeletonList } from '@/components/Skeleton';

export default function GuidelinesPage() {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<Citation[]>([]);
  const [info, setInfo] = useState<{ corpus_version: string; chunk_count: number } | null>(null);
  const [hasSearched, setHasSearched] = useState(false);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The corpus header is a nice-to-have, so its failure gets a line rather than a banner: not
  // knowing the corpus version does not stop anyone searching. It is still worth saying,
  // because "no version shown" and "version withheld" look identical otherwise.
  const [infoFailed, setInfoFailed] = useState(false);

  useEffect(() => {
    api
      .corpusInfo()
      .then((data) => {
        setInfo(data);
        setInfoFailed(false);
      })
      .catch(() => setInfoFailed(true));
  }, []);

  // Kept in a callback so the retry button and the form submit run the same search rather
  // than two paths that can drift.
  const runSearch = useCallback(async (term: string) => {
    setError(null);
    setSearching(true);
    try {
      setResults(await api.searchGuidelines(term));
      setHasSearched(true);
    } catch (err) {
      // Previously this rejection was unhandled, which on this page was worse than it sounds:
      // the results list simply kept showing the *previous* query's citations, under the new
      // query still sitting in the search box. A clinician reading guidance for one condition
      // while the box named another is exactly the mix-up a cited corpus exists to prevent,
      // so the stale results are cleared before the banner goes up.
      setResults([]);
      setHasSearched(false);
      setError(requestErrorMessage(err, 'the guideline search'));
    } finally {
      setSearching(false);
    }
  }, []);

  async function search(e: React.FormEvent) {
    e.preventDefault();
    if (query.trim().length < 2) return;
    await runSearch(query.trim());
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-slate-900">
          Clinical guideline corpus
        </h1>
        {info && (
          <p className="mt-1 text-sm text-slate-500">
            {info.chunk_count} chunks · corpus {info.corpus_version} · ICMR Standard Treatment
            Workflows + WHO/NICE
          </p>
        )}
        {infoFailed && (
          <p className="mt-1 text-sm text-slate-500">
            Corpus details are unavailable — searching still works, and every result carries its own
            citation.
          </p>
        )}
      </div>

      <form onSubmit={search} className="flex gap-2" role="search">
        <div className="relative flex-1">
          <label htmlFor="guideline-search" className="sr-only">
            Search the clinical guideline corpus
          </label>
          <svg
            aria-hidden="true"
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400"
            fill="none"
            viewBox="0 0 24 24"
            strokeWidth={2}
            stroke="currentColor"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z"
            />
          </svg>
          <input
            id="guideline-search"
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search guidelines (e.g. management of dengue)"
            className="w-full pl-10"
          />
        </div>
        <Button type="submit" disabled={searching} aria-busy={searching}>
          {searching ? 'Searching…' : 'Search'}
        </Button>
      </form>

      {error && (
        <ErrorBanner
          message={error}
          onRetry={() => void runSearch(query.trim())}
          retrying={searching}
        />
      )}

      <div className="space-y-3" aria-live="polite">
        {searching && (
          <LoadingBlock label="Searching the guideline corpus">
            <SkeletonList rows={3} />
          </LoadingBlock>
        )}

        {!searching && results.length > 0 && (
          <div className="animate-fade-in space-y-3">
            {results.map((c, i) => (
              <Card key={i} className="transition-shadow hover:shadow-md">
                <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="inline-flex items-center rounded-md bg-brand-50 px-2 py-0.5 text-xs font-semibold uppercase tracking-wider text-brand-700 ring-1 ring-inset ring-brand-200">
                        {c.source}
                      </span>
                      <span className="text-sm font-semibold text-slate-900">
                        {c.document_title}
                      </span>
                    </div>
                    {c.heading && (
                      <p className="mt-1.5 text-sm font-medium text-slate-700">{c.heading}</p>
                    )}
                    {c.snippet && (
                      <p className="mt-1 text-sm leading-relaxed text-slate-600">{c.snippet}</p>
                    )}
                    <p className="mt-2 text-xs text-slate-400">
                      {c.section_id}
                      {c.page_range ? ` · pp. ${c.page_range}` : ''}
                    </p>
                  </div>
                  {c.score != null && (
                    <span className="flex-shrink-0 rounded-full bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-600">
                      {(c.score * 100).toFixed(0)}% match
                    </span>
                  )}
                </div>
              </Card>
            ))}
          </div>
        )}

        {/* Both empty states are suppressed while searching and after a failure. "No results
            found" is a claim about the corpus, and a search that never completed supports no
            such claim — on a guideline corpus that is the difference between "no guidance
            exists for this" and "we could not look". */}
        {!searching && !error && hasSearched && results.length === 0 && (
          <div className="animate-fade-in rounded-2xl border-2 border-dashed border-slate-200 px-6 py-12 text-center">
            <svg
              aria-hidden="true"
              className="mx-auto h-10 w-10 text-slate-300"
              fill="none"
              viewBox="0 0 24 24"
              strokeWidth={1.5}
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M12 6.042A8.967 8.967 0 006 3.75c-1.052 0-2.062.18-3 .512v14.25A8.987 8.987 0 016 18c2.305 0 4.408.867 6 2.292m0-14.25a8.966 8.966 0 016-2.292c1.052 0 2.062.18 3 .512v14.25A8.987 8.987 0 0018 18a8.967 8.967 0 00-6 2.292m0-14.25v14.25"
              />
            </svg>
            <h3 className="mt-3 text-sm font-semibold text-slate-900">No results found</h3>
            <p className="mt-1 text-sm text-slate-500">
              Try a different search term or broaden your query.
            </p>
          </div>
        )}

        {!searching && !error && !hasSearched && results.length === 0 && (
          <div className="rounded-2xl border-2 border-dashed border-slate-200 px-6 py-12 text-center">
            <svg
              aria-hidden="true"
              className="mx-auto h-10 w-10 text-slate-300"
              fill="none"
              viewBox="0 0 24 24"
              strokeWidth={1.5}
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M12 6.042A8.967 8.967 0 006 3.75c-1.052 0-2.062.18-3 .512v14.25A8.987 8.987 0 016 18c2.305 0 4.408.867 6 2.292m0-14.25a8.966 8.966 0 016-2.292c1.052 0 2.062.18 3 .512v14.25A8.987 8.987 0 0018 18a8.967 8.967 0 00-6 2.292m0-14.25v14.25"
              />
            </svg>
            <h3 className="mt-3 text-sm font-semibold text-slate-900">
              Search the guideline corpus
            </h3>
            <p className="mt-1 text-sm text-slate-500">
              Enter a clinical query to retrieve grounded, citable guidance from ICMR, WHO, and NICE
              sources.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
