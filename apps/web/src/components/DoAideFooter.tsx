const TOOLS = [
  { name: 'GST', url: 'https://gst.doaide.com', desc: 'GST filing & compliance' },
  { name: '409A', url: 'https://409a.doaide.com', desc: '409A valuations' },
  { name: 'Pulse', url: 'https://pulse.doaide.com', desc: 'Email marketing' },
  { name: 'Med', url: 'https://med.doaide.com', desc: 'AI clinical decision support' },
  { name: 'Jobs', url: 'https://job.doaide.com', desc: 'Job applications' },
  { name: 'Desk', url: 'https://desk.doaide.com', desc: 'Help desk' },
  { name: 'Realty', url: 'https://realty.doaide.com', desc: 'Real estate tools' },
];

export function DoAideFooter() {
  return (
    <footer
      className="border-t border-[var(--da-border)] bg-[var(--da-bg)] px-4 py-6 mt-8"
      aria-label="More tools from DoAide"
    >
      <div className="max-w-5xl mx-auto">
        <p className="text-xs font-semibold uppercase tracking-wider text-[var(--da-text-muted)] mb-4">
          More tools from DoAide
        </p>
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-7 gap-3">
          {TOOLS.map((t) => (
            <a
              key={t.url}
              href={t.url}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-start gap-2 p-3 rounded-lg border border-[var(--da-border)] bg-[var(--da-surface)] hover:border-[var(--da-primary)] transition-colors no-underline group"
            >
              <span>
                <strong className="block text-xs font-semibold text-[var(--da-text)] group-hover:text-[var(--da-primary)] transition-colors">
                  {t.name}
                </strong>
                <span className="text-[10px] leading-tight text-[var(--da-text-muted)]">
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
            className="text-[var(--da-primary)] hover:text-[var(--da-primary-hover)] no-underline transition-colors"
          >
            View all tools &rarr;
          </a>
        </p>
      </div>
    </footer>
  );
}
