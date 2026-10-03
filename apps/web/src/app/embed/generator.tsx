'use client';

import { useState } from 'react';
import { ShareButtons } from '@/components/ShareButtons';

const POSITIONS = ['bottom-right', 'bottom-left'] as const;
const THEMES = ['dark', 'light', 'auto'] as const;

export function EmbedGenerator() {
  const [position, setPosition] = useState<(typeof POSITIONS)[number]>('bottom-right');
  const [theme, setTheme] = useState<(typeof THEMES)[number]>('dark');
  const [brandColor, setBrandColor] = useState('#F0B429');
  const [copied, setCopied] = useState(false);

  const snippet = `<script
  src="https://med.doaide.com/widget.js"
  data-position="${position}"
  data-theme="${theme}"
  data-color="${brandColor}"
  async
><\/script>`;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(snippet);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch { /* noop */ }
  };

  return (
    <div>
      <h1 className="text-3xl font-bold text-[var(--doaide-text)] mb-2" style={{ fontFamily: 'var(--doaide-font-display)' }}>Embed Widget Generator</h1>
      <p className="text-[var(--doaide-text-secondary)] mb-8">Add AI-powered clinical decision support to your healthcare application.</p>
      <div className="space-y-6">
        <div className="p-6 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)] space-y-4">
          <div>
            <label className="block text-sm font-medium text-[var(--doaide-text-secondary)] mb-2">Position</label>
            <div className="flex gap-2">
              {POSITIONS.map((p) => (
                <button key={p} onClick={() => setPosition(p)} className={`px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${position === p ? 'bg-[var(--doaide-gold)] text-[var(--doaide-text-on-gold)]' : 'bg-[var(--doaide-bg)] text-[var(--doaide-text-secondary)] border border-[var(--doaide-border)]'}`}>{p.replace('-', ' ')}</button>
              ))}
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium text-[var(--doaide-text-secondary)] mb-2">Theme</label>
            <div className="flex gap-2">
              {THEMES.map((t) => (
                <button key={t} onClick={() => setTheme(t)} className={`px-3 py-1.5 rounded-md text-sm font-medium capitalize transition-colors ${theme === t ? 'bg-[var(--doaide-gold)] text-[var(--doaide-text-on-gold)]' : 'bg-[var(--doaide-bg)] text-[var(--doaide-text-secondary)] border border-[var(--doaide-border)]'}`}>{t}</button>
              ))}
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium text-[var(--doaide-text-secondary)] mb-2">Brand Color</label>
            <div className="flex items-center gap-3">
              <input type="color" value={brandColor} onChange={(e) => setBrandColor(e.target.value)} className="w-10 h-10 rounded cursor-pointer border-0 p-0" />
              <input type="text" value={brandColor} onChange={(e) => setBrandColor(e.target.value)} className="w-28 px-3 py-2 rounded-md text-sm font-mono" />
            </div>
          </div>
        </div>
        <div className="p-6 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)]">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-medium text-[var(--doaide-text-secondary)]">Embed Code</h2>
            <button onClick={copy} className="px-3 py-1.5 rounded-md text-sm font-medium bg-[var(--doaide-gold)] text-[var(--doaide-text-on-gold)] hover:bg-[var(--doaide-gold-hover)]">{copied ? 'Copied!' : 'Copy Code'}</button>
          </div>
          <pre className="p-4 rounded-lg bg-[var(--doaide-bg)] text-sm text-[var(--doaide-text-secondary)] overflow-x-auto font-mono whitespace-pre-wrap">{snippet}</pre>
        </div>
        <ShareButtons url="https://med.doaide.com/embed" title="DoAide Med Embed Widget Generator" />
      </div>
    </div>
  );
}
