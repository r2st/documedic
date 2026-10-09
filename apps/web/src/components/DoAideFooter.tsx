const TOOLS = [
  { icon: '🩺', name: 'Med', url: 'https://med.doaide.com', desc: 'AI clinical decision support' },
  { icon: '📋', name: 'Comply', url: 'https://comply.doaide.com', desc: 'Track compliance deadlines' },
  { icon: '📝', name: 'Contracts', url: 'https://contracts.doaide.com', desc: 'Draft & manage contracts' },
  { icon: '🧾', name: 'Invoicer', url: 'https://invoicer.doaide.com', desc: 'Create GST invoices' },
  { icon: '🏷️', name: 'GST Bot', url: 'https://gst.doaide.com', desc: 'GST filing & compliance' },
  { icon: '✍️', name: 'Write', url: 'https://write.doaide.com', desc: 'AI writing assistant' },
];

export function DoAideFooter() {
  return (
    <footer
      className="border-t border-[var(--doaide-border)] bg-[var(--doaide-bg)] px-4 py-6 mt-8"
      aria-label="More free tools from DoAide"
    >
      <div className="max-w-5xl mx-auto">
        <p className="text-xs font-semibold uppercase tracking-wider text-[var(--doaide-text-muted)] mb-4">
          More free tools from DoAide
        </p>
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
          {TOOLS.map((t) => (
            <a
              key={t.url}
              href={t.url}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-start gap-2 p-3 rounded-lg border border-[var(--doaide-border)] bg-[var(--doaide-surface)] hover:border-[var(--doaide-gold)] transition-colors no-underline group"
            >
              <span className="text-lg leading-none flex-shrink-0">{t.icon}</span>
              <span>
                <strong className="block text-xs font-semibold text-[var(--doaide-text)] group-hover:text-[var(--doaide-gold)] transition-colors">
                  {t.name}
                </strong>
                <span className="text-[10px] leading-tight text-[var(--doaide-text-muted)]">
                  {t.desc}
                </span>
              </span>
            </a>
          ))}
        </div>
        <p className="mt-3 text-xs">
          <a
            href="https://doaide.com"
            target="_blank"
            rel="noopener noreferrer"
            className="text-[var(--doaide-gold)] hover:text-[var(--doaide-gold-light)] no-underline transition-colors"
          >
            View all 40+ tools &rarr;
          </a>
        </p>
      </div>
    </footer>
  );
}
