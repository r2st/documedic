'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api';
import type { Citation } from '@/lib/types';
import { Button, Card } from '@/components/ui';

export default function GuidelinesPage() {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<Citation[]>([]);
  const [info, setInfo] = useState<{ corpus_version: string; chunk_count: number } | null>(null);

  useEffect(() => {
    void api.corpusInfo().then(setInfo);
  }, []);

  async function search(e: React.FormEvent) {
    e.preventDefault();
    if (query.trim().length < 2) return;
    setResults(await api.searchGuidelines(query.trim()));
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-bold">Clinical guideline corpus</h1>
        {info && (
          <p className="text-sm text-slate-500">
            {info.chunk_count} chunks · corpus {info.corpus_version} · ICMR Standard Treatment
            Workflows + WHO/NICE
          </p>
        )}
      </div>

      <form onSubmit={search} className="flex gap-2">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search guidelines (e.g. management of dengue)"
          className="flex-1 rounded-md border border-slate-300 px-3 py-2 text-sm"
        />
        <Button type="submit">Search</Button>
      </form>

      <div className="space-y-2">
        {results.map((c, i) => (
          <Card key={i}>
            <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
              <p className="min-w-0 break-words text-sm font-semibold">
                <span className="uppercase text-blue-700">{c.source}</span> · {c.document_title}
              </p>
              {c.score != null && (
                <span className="flex-shrink-0 text-xs text-slate-400">
                  relevance {(c.score * 100).toFixed(0)}%
                </span>
              )}
            </div>
            {c.heading && <p className="text-sm font-medium text-slate-700">{c.heading}</p>}
            {c.snippet && <p className="mt-1 text-sm text-slate-600">{c.snippet}</p>}
            <p className="mt-1 text-xs text-slate-400">
              {c.section_id}
              {c.page_range ? ` · pp. ${c.page_range}` : ''}
            </p>
          </Card>
        ))}
        {results.length === 0 && (
          <p className="py-6 text-center text-sm text-slate-400">
            Search the corpus to see grounded, citable guidance.
          </p>
        )}
      </div>
    </div>
  );
}
