import type { Metadata } from 'next';
import Link from 'next/link';
import { PublicNav, PublicFooter } from '@/components/PublicLayout';

export const metadata: Metadata = {
  title: 'Free Health Tools — DoAide Med',
  description: 'Free clinical calculators: BMI calculator, drug interaction checker, and symptom triage quiz.',
};

const TOOLS = [
  { slug: 'bmi-calculator', title: 'BMI Calculator', description: 'Calculate Body Mass Index with health category and recommendations.', icon: '⚖️' },
  { slug: 'drug-interaction-checker', title: 'Drug Interaction Checker', description: 'Check common drug-drug interactions from a curated database.', icon: '💊' },
  { slug: 'symptom-triage', title: 'Symptom Triage', description: 'Simple symptom assessment quiz to help prioritize care.', icon: '🩺' },
] as const;

export default function ToolsIndex() {
  return (
    <div className="min-h-screen bg-[var(--doaide-bg)]">
      <PublicNav />
      <main className="max-w-5xl mx-auto px-6 py-16">
        <h1 className="text-3xl font-bold text-[var(--doaide-text)] mb-2" style={{ fontFamily: 'var(--doaide-font-display)' }}>Free Health Tools</h1>
        <p className="text-[var(--doaide-text-secondary)] mb-10 max-w-2xl">
          Clinical calculators for healthcare professionals and patients — no sign-up required. Always consult a qualified healthcare provider for medical decisions.
        </p>
        <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
          {TOOLS.map((tool) => (
            <Link key={tool.slug} href={`/tools/${tool.slug}`} className="block p-6 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)] hover:border-[var(--doaide-gold)] hover:shadow-[var(--doaide-shadow-gold)] transition-all no-underline group">
              <div className="text-3xl mb-3">{tool.icon}</div>
              <h2 className="text-lg font-semibold text-[var(--doaide-text)] group-hover:text-[var(--doaide-gold)] transition-colors mb-2">{tool.title}</h2>
              <p className="text-sm text-[var(--doaide-text-secondary)]">{tool.description}</p>
            </Link>
          ))}
        </div>
      </main>
      <PublicFooter />
    </div>
  );
}
