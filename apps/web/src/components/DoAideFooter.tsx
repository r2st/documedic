const TOOLS = [
  { icon: '✅', name: 'Comply', url: 'https://comply.doaide.com', desc: 'Track all compliance deadlines' },
  { icon: '\u{1F4DD}', name: 'Contracts', url: 'https://contracts.doaide.com', desc: 'Draft & manage business contracts' },
  { icon: '\u{1F9FE}', name: 'Invoicer', url: 'https://invoicer.doaide.com', desc: 'Create GST invoices in seconds' },
  { icon: '\u{1F3F7}️', name: 'GST Bot', url: 'https://gst.doaide.com', desc: 'GST filing, lookup & compliance' },
  { icon: '✍️', name: 'Write', url: 'https://write.doaide.com', desc: 'AI writing assistant' },
];

export function DoAideFooter() {
  return (
    <footer
      style={{
        background: '#f8f9fa', borderTop: '1px solid #e9ecef',
        padding: '2rem 1rem', marginTop: '3rem',
      }}
      aria-label="More free tools from DoAide"
    >
      <div style={{ maxWidth: 900, margin: '0 auto' }}>
        <p style={{ fontSize: '0.8rem', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.06em', color: '#6c757d', margin: '0 0 1rem' }}>
          More free tools from DoAide
        </p>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: '0.75rem' }}>
          {TOOLS.map((t) => (
            <a
              key={t.url}
              href={t.url}
              target="_blank"
              rel="noopener noreferrer"
              style={{
                display: 'flex', alignItems: 'flex-start', gap: '0.5rem',
                padding: '0.75rem', background: '#fff', border: '1px solid #e9ecef',
                borderRadius: '0.5rem', textDecoration: 'none', color: '#212529',
              }}
            >
              <span style={{ fontSize: '1.25rem', lineHeight: 1, flexShrink: 0 }}>{t.icon}</span>
              <span>
                <strong style={{ display: 'block', fontSize: '0.85rem' }}>{t.name}</strong>
                <span style={{ fontSize: '0.75rem', color: '#6c757d' }}>{t.desc}</span>
              </span>
            </a>
          ))}
        </div>
        <p style={{ marginTop: '1rem', fontSize: '0.8rem' }}>
          <a href="https://doaide.com" target="_blank" rel="noopener noreferrer" style={{ color: '#0d6efd', textDecoration: 'none' }}>
            View all 40+ tools &rarr;
          </a>
        </p>
      </div>
    </footer>
  );
}
