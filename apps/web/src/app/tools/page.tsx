import type { Metadata } from 'next';
import Link from 'next/link';
import { CrossProductLinks } from '@/components/CrossProductLinks';
import { PublicNav, PublicFooter } from '@/components/PublicLayout';

export const metadata: Metadata = {
  title: 'Free Clinical Tools — BMI Calculator, Drug Interaction Checker, Symptom Triage | DoAide Med',
  description:
    'Free clinical tools for healthcare professionals: BMI calculator, drug interaction checker, and symptom triage. No sign-up required.',
  openGraph: {
    title: 'Free Clinical Tools — DoAide Med',
    description:
      'BMI calculator, drug interaction checker, and symptom triage — free clinical tools for Indian doctors. No sign-up required.',
    url: 'https://med.doaide.com/tools',
  },
};

const TOOLS = [
  {
    slug: 'bmi-calculator',
    title: 'BMI Calculator',
    description: 'Calculate Body Mass Index with health category and personalized recommendations for your patients.',
    icon: '⚖️',
  },
  {
    slug: 'drug-interaction-checker',
    title: 'Drug Interaction Checker',
    description: 'Check common drug-drug interactions from a curated database of frequently prescribed medications.',
    icon: '💊',
  },
  {
    slug: 'symptom-triage',
    title: 'Symptom Triage',
    description: 'AI-powered symptom assessment to help prioritize care and generate differential diagnoses.',
    icon: '🩺',
  },
] as const;

export default function ToolsIndex() {
  return (
    <div className="min-h-screen bg-[var(--doaide-bg)]">
      <PublicNav />
      <main className="max-w-5xl mx-auto px-6 py-16">
        <h1
          className="text-3xl font-bold text-[var(--doaide-text)] mb-2"
          style={{ fontFamily: 'var(--doaide-font-display)' }}
        >
          Free Clinical Tools
        </h1>
        <p className="text-[var(--doaide-text-secondary)] mb-10 max-w-2xl">
          Clinical calculators for healthcare professionals and patients — no sign-up required.
          Always consult a qualified healthcare provider for medical decisions.
        </p>
        <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
          {TOOLS.map((tool) => (
            <Link
              key={tool.slug}
              href={`/tools/${tool.slug}`}
              className="block p-6 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)] hover:border-[var(--doaide-gold)] hover:shadow-[var(--doaide-shadow-gold)] transition-all no-underline group"
            >
              <div className="text-3xl mb-3">{tool.icon}</div>
              <h2 className="text-lg font-semibold text-[var(--doaide-text)] group-hover:text-[var(--doaide-gold)] transition-colors mb-2">
                {tool.title}
              </h2>
              <p className="text-sm text-[var(--doaide-text-secondary)]">{tool.description}</p>
            </Link>
          ))}
        </div>
        <CrossProductLinks page="tools" />

        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{
            __html: JSON.stringify({
              '@context': 'https://schema.org',
              '@type': 'CollectionPage',
              name: 'Free Clinical Tools — DoAide Med',
              description:
                'Free clinical calculators for healthcare professionals: BMI calculator, drug interaction checker, symptom triage.',
              url: 'https://med.doaide.com/tools',
              mainEntity: TOOLS.map((tool) => ({
                '@type': 'WebApplication',
                name: tool.title,
                description: tool.description,
                url: `https://med.doaide.com/tools/${tool.slug}`,
                applicationCategory: 'HealthApplication',
                operatingSystem: 'Web',
                offers: { '@type': 'Offer', price: '0', priceCurrency: 'USD' },
              })),
            }),
          }}
        />
      </main>
      <PublicFooter />
    </div>
  );
}
