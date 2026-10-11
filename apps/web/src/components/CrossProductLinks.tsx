const CROSS_LINKS: Record<string, { href: string; label: string; text: string }[]> = {
  tools: [
    {
      href: 'https://gst.doaide.com',
      label: 'DoAide GST',
      text: 'GST filing, compliance and tax management tools',
    },
    {
      href: 'https://pulse.doaide.com',
      label: 'DoAide Pulse',
      text: 'Email marketing and newsletter management platform',
    },
    {
      href: 'https://desk.doaide.com',
      label: 'DoAide Desk',
      text: 'Help desk and customer support tools',
    },
  ],
  blog: [
    {
      href: 'https://gst.doaide.com',
      label: 'DoAide GST',
      text: 'GST filing and compliance for Indian businesses',
    },
    {
      href: 'https://409a.doaide.com',
      label: 'DoAide 409A',
      text: '409A valuations for startups and private companies',
    },
  ],
};

export function CrossProductLinks({ page }: { page: string }) {
  const links = CROSS_LINKS[page];
  if (!links) return null;

  return (
    <aside className="mt-12 border-t border-[var(--da-border)] pt-8" aria-label="Explore more DoAide tools">
      <h3
        className="mb-4 text-lg font-semibold text-[var(--da-text)]"
        style={{ fontFamily: 'var(--da-font-display)' }}
      >
        Explore More Tools
      </h3>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {links.map((link) => (
          <a
            key={link.href}
            href={link.href}
            target="_blank"
            rel="noopener noreferrer"
            className="group relative flex flex-col gap-1 rounded-xl border border-[var(--da-border)] bg-[var(--da-surface)] p-4 no-underline transition-all hover:border-[var(--da-primary)] hover:shadow-[var(--da-shadow-brand)]"
          >
            <strong className="text-sm text-[var(--da-primary)] group-hover:text-[var(--da-primary-hover)]">
              {link.label}
            </strong>
            <span className="text-xs leading-relaxed text-[var(--da-text-secondary)]">
              {link.text}
            </span>
            <span className="absolute right-3 top-3 text-xs text-[var(--da-text-muted)]" aria-hidden="true">
              ↗
            </span>
          </a>
        ))}
      </div>
    </aside>
  );
}
