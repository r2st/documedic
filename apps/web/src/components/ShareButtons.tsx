'use client';

import { useState } from 'react';

interface ShareButtonsProps {
  url: string;
  title: string;
}

export function ShareButtons({ url, title }: ShareButtonsProps) {
  const [copied, setCopied] = useState(false);
  const encoded = encodeURIComponent(url);
  const encodedTitle = encodeURIComponent(title);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard API unavailable */
    }
  };

  return (
    <div className="flex items-center gap-3 flex-wrap">
      <span className="text-sm text-[var(--doaide-text-muted)]">Share:</span>
      <a
        href={`https://wa.me/?text=${encodedTitle}%20${encoded}`}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm font-medium bg-[#25D366] text-white hover:opacity-90 transition-opacity"
      >
        WhatsApp
      </a>
      <a
        href={`https://twitter.com/intent/tweet?text=${encodedTitle}&url=${encoded}`}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm font-medium bg-[var(--doaide-surface)] text-[var(--doaide-text)] border border-[var(--doaide-border)] hover:bg-[var(--doaide-surface-hover)] transition-colors"
      >
        X / Twitter
      </a>
      <button
        onClick={copy}
        className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm font-medium bg-[var(--doaide-surface)] text-[var(--doaide-text)] border border-[var(--doaide-border)] hover:bg-[var(--doaide-surface-hover)] transition-colors"
      >
        {copied ? 'Copied!' : 'Copy link'}
      </button>
    </div>
  );
}
