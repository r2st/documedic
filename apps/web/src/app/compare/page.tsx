import type { Metadata } from 'next';
import Link from 'next/link';
import { PublicNav, PublicFooter } from '@/components/PublicLayout';

export const metadata: Metadata = {
  title: 'DoAide Med vs UpToDate, DynaMed & Free CDSS Tools — Comparison',
  description:
    'Compare DoAide Med with UpToDate, DynaMed, Isabel CDSS, and other clinical decision support tools. See why Indian doctors choose DoAide Med for drug interactions, symptom triage, and evidence-based care.',
  openGraph: {
    title: 'DoAide Med vs UpToDate & Other CDSS Tools — Feature Comparison',
    description:
      'Feature-by-feature comparison of clinical decision support systems for Indian healthcare professionals.',
    url: 'https://med.doaide.com/compare',
  },
};

const FEATURES = [
  {
    feature: 'Drug Interaction Checker',
    doaide: { text: 'Full-regimen AI analysis', ok: true },
    uptodate: { text: 'Lexicomp add-on (paid)', ok: 'partial' },
    dynamed: { text: 'Basic pair-wise', ok: 'partial' },
    isabel: { text: 'No', ok: false },
  },
  {
    feature: 'Symptom Triage / DDx',
    doaide: { text: 'AI-ranked differentials', ok: true },
    uptodate: { text: 'Manual topic lookup', ok: 'partial' },
    dynamed: { text: 'Manual topic lookup', ok: 'partial' },
    isabel: { text: 'Differential generator', ok: true },
  },
  {
    feature: 'Indian Drug Formulary',
    doaide: { text: 'Indian Pharmacopoeia + generics', ok: true },
    uptodate: { text: 'US/EU focused', ok: false },
    dynamed: { text: 'US/EU focused', ok: false },
    isabel: { text: 'No formulary', ok: false },
  },
  {
    feature: 'Indian Treatment Guidelines',
    doaide: { text: 'ICMR, API, specialty', ok: true },
    uptodate: { text: 'Mostly US/EU guidelines', ok: 'partial' },
    dynamed: { text: 'Mostly US/EU guidelines', ok: 'partial' },
    isabel: { text: 'No guidelines', ok: false },
  },
  {
    feature: 'Hindi / Regional Language',
    doaide: { text: 'Hindi + English', ok: true },
    uptodate: { text: 'English only', ok: false },
    dynamed: { text: 'English only', ok: false },
    isabel: { text: 'English only', ok: false },
  },
  {
    feature: 'Free Clinical Tools',
    doaide: { text: 'BMI, DDx, Drug checker', ok: true },
    uptodate: { text: 'No free tools', ok: false },
    dynamed: { text: 'No free tools', ok: false },
    isabel: { text: 'Limited free trial', ok: 'partial' },
  },
  {
    feature: 'Offline / Low-Connectivity',
    doaide: { text: 'Core algorithms cached', ok: true },
    uptodate: { text: 'App with offline (paid)', ok: 'partial' },
    dynamed: { text: 'App with offline (paid)', ok: 'partial' },
    isabel: { text: 'Online only', ok: false },
  },
  {
    feature: 'Evidence-Based Content',
    doaide: { text: 'AI + curated sources', ok: true },
    uptodate: { text: 'Gold standard', ok: true },
    dynamed: { text: 'Systematic reviews', ok: true },
    isabel: { text: 'Pattern matching', ok: 'partial' },
  },
  {
    feature: 'EHR Integration',
    doaide: { text: 'Embeddable widget', ok: true },
    uptodate: { text: 'Enterprise (expensive)', ok: true },
    dynamed: { text: 'Enterprise', ok: true },
    isabel: { text: 'API available', ok: true },
  },
  {
    feature: 'Pricing for Indian Doctors',
    doaide: { text: 'Free tier + affordable plans', ok: true },
    uptodate: { text: '$500+/year', ok: false },
    dynamed: { text: '$400+/year', ok: false },
    isabel: { text: '$750+/year', ok: false },
  },
  {
    feature: 'Built for Indian Healthcare',
    doaide: { text: 'India-first design', ok: true },
    uptodate: { text: 'US/EU primary', ok: false },
    dynamed: { text: 'US/EU primary', ok: false },
    isabel: { text: 'UK primary', ok: false },
  },
] as const;

function Badge({ ok }: { ok: boolean | 'partial' }) {
  if (ok === true) return <span className="text-emerald-400 font-bold text-xs">YES</span>;
  if (ok === 'partial') return <span className="text-amber-400 font-semibold text-xs">PARTIAL</span>;
  return <span className="text-red-400 font-bold text-xs">NO</span>;
}

export default function ComparePage() {
  return (
    <div className="min-h-screen bg-[var(--doaide-bg)]">
      <PublicNav />
      <main className="max-w-5xl mx-auto px-6 py-16">
        <section className="text-center mb-16">
          <h1
            className="text-3xl sm:text-4xl font-bold text-[var(--doaide-text)] mb-4"
            style={{ fontFamily: 'var(--doaide-font-display)' }}
          >
            DoAide Med vs UpToDate, DynaMed &amp; Isabel
          </h1>
          <p className="text-lg text-[var(--doaide-text-secondary)] max-w-2xl mx-auto">
            Most CDSS tools are built for US/EU hospitals at US/EU prices. DoAide Med is built for
            Indian clinicians — with Indian drugs, Indian guidelines, and Indian pricing.
          </p>
        </section>

        <div className="overflow-x-auto -mx-6 px-6 mb-16">
          <table className="w-full min-w-[700px] text-sm border-collapse">
            <thead>
              <tr>
                <th className="text-left py-3 px-4 text-xs font-bold uppercase tracking-wider text-[var(--doaide-text-muted)] border-b-2 border-[var(--doaide-border)]">
                  Feature
                </th>
                <th className="text-left py-3 px-4 text-xs font-bold uppercase tracking-wider text-[var(--doaide-gold)] border-b-2 border-[var(--doaide-gold)] bg-[rgba(240,180,41,0.05)]">
                  DoAide Med
                </th>
                <th className="text-left py-3 px-4 text-xs font-bold uppercase tracking-wider text-[var(--doaide-text-muted)] border-b-2 border-[var(--doaide-border)]">
                  UpToDate
                </th>
                <th className="text-left py-3 px-4 text-xs font-bold uppercase tracking-wider text-[var(--doaide-text-muted)] border-b-2 border-[var(--doaide-border)]">
                  DynaMed
                </th>
                <th className="text-left py-3 px-4 text-xs font-bold uppercase tracking-wider text-[var(--doaide-text-muted)] border-b-2 border-[var(--doaide-border)]">
                  Isabel CDSS
                </th>
              </tr>
            </thead>
            <tbody>
              {FEATURES.map((row) => (
                <tr key={row.feature} className="hover:bg-[rgba(255,255,255,0.02)]">
                  <td className="py-3 px-4 font-semibold text-[var(--doaide-text)] border-b border-[var(--doaide-border)]">
                    {row.feature}
                  </td>
                  <td className="py-3 px-4 border-b border-[var(--doaide-border)] bg-[rgba(240,180,41,0.03)]">
                    <Badge ok={row.doaide.ok} />
                    <span className="ml-2 text-[var(--doaide-text-secondary)]">{row.doaide.text}</span>
                  </td>
                  <td className="py-3 px-4 border-b border-[var(--doaide-border)]">
                    <Badge ok={row.uptodate.ok} />
                    <span className="ml-2 text-[var(--doaide-text-secondary)]">{row.uptodate.text}</span>
                  </td>
                  <td className="py-3 px-4 border-b border-[var(--doaide-border)]">
                    <Badge ok={row.dynamed.ok} />
                    <span className="ml-2 text-[var(--doaide-text-secondary)]">{row.dynamed.text}</span>
                  </td>
                  <td className="py-3 px-4 border-b border-[var(--doaide-border)]">
                    <Badge ok={row.isabel.ok} />
                    <span className="ml-2 text-[var(--doaide-text-secondary)]">{row.isabel.text}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <section className="text-center py-12 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)]">
          <h2
            className="text-xl font-bold text-[var(--doaide-text)] mb-3"
            style={{ fontFamily: 'var(--doaide-font-display)' }}
          >
            Built for Indian clinicians. Free to start.
          </h2>
          <p className="text-sm text-[var(--doaide-text-secondary)] mb-6 max-w-lg mx-auto">
            Try our free drug interaction checker and symptom triage tool — no account required.
            See why doctors across India are switching from expensive US tools.
          </p>
          <div className="flex items-center justify-center gap-4 flex-wrap">
            <Link
              href="/"
              className="inline-block px-6 py-3 rounded-lg text-sm font-semibold bg-[var(--doaide-gold)] text-[var(--doaide-text-on-gold)] hover:bg-[var(--doaide-gold-hover)] no-underline transition-colors"
            >
              Get Started Free
            </Link>
            <Link
              href="/tools/drug-interaction-checker"
              className="inline-block px-6 py-3 rounded-lg text-sm font-semibold border border-[var(--doaide-gold)] text-[var(--doaide-gold)] hover:bg-[var(--doaide-gold)] hover:text-[var(--doaide-text-on-gold)] no-underline transition-colors"
            >
              Try Drug Interaction Checker
            </Link>
          </div>
        </section>

        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{
            __html: JSON.stringify({
              '@context': 'https://schema.org',
              '@type': 'WebPage',
              name: 'DoAide Med vs UpToDate, DynaMed & Isabel — CDSS Comparison',
              description:
                'Feature-by-feature comparison of clinical decision support tools for Indian healthcare professionals.',
              url: 'https://med.doaide.com/compare',
              breadcrumb: {
                '@type': 'BreadcrumbList',
                itemListElement: [
                  { '@type': 'ListItem', position: 1, name: 'Home', item: 'https://med.doaide.com/' },
                  { '@type': 'ListItem', position: 2, name: 'Compare', item: 'https://med.doaide.com/compare' },
                ],
              },
            }),
          }}
        />
      </main>
      <PublicFooter />
    </div>
  );
}
