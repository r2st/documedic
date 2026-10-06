const CROSS_LINKS: Record<string, { href: string; label: string; text: string }[]> = {
  tools: [
    {
      href: 'https://comply.doaide.com',
      label: 'Compliance Tracker',
      text: 'Ensure compliance with healthcare regulations and track deadlines',
    },
    {
      href: 'https://write.doaide.com',
      label: 'DoAide Write',
      text: 'Generate professional medical documents and clinical reports',
    },
    {
      href: 'https://fincalc.doaide.com',
      label: 'Financial Calculators',
      text: 'Healthcare cost estimators and financial planning tools',
    },
  ],
  blog: [
    {
      href: 'https://comply.doaide.com',
      label: 'Compliance Tracker',
      text: 'Track healthcare compliance requirements and regulatory deadlines',
    },
    {
      href: 'https://contracts.doaide.com',
      label: 'Contract Generator',
      text: 'Draft healthcare provider agreements and patient consent forms',
    },
  ],
};

export function CrossProductLinks({ page }: { page: string }) {
  const links = CROSS_LINKS[page];
  if (!links) return null;

  return (
    <aside className="mt-12 border-t border-[var(--doaide-border)] pt-8" aria-label="Explore more DoAide tools">
      <h3
        className="mb-4 text-lg font-semibold text-[var(--doaide-text)]"
        style={{ fontFamily: 'var(--doaide-font-display)' }}
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
            className="group relative flex flex-col gap-1 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)] p-4 no-underline transition-all hover:border-[var(--doaide-gold)] hover:shadow-[var(--doaide-shadow-gold)]"
          >
            <strong className="text-sm text-[var(--doaide-gold)] group-hover:text-[var(--doaide-gold-light)]">
              {link.label}
            </strong>
            <span className="text-xs leading-relaxed text-[var(--doaide-text-secondary)]">
              {link.text}
            </span>
            <span className="absolute right-3 top-3 text-xs text-[var(--doaide-text-muted)]" aria-hidden="true">
              ↗
            </span>
          </a>
        ))}
      </div>
    </aside>
  );
}
