import type { Metadata } from 'next';
import { PublicNav, PublicFooter } from '@/components/PublicLayout';
import { SymptomTriage } from './triage';

export const metadata: Metadata = {
  title: 'Symptom Triage — DoAide Med',
  description: 'Simple symptom assessment quiz to help prioritize when to seek medical care.',
};

export default function Page() {
  return (
    <div className="min-h-screen bg-[var(--doaide-bg)]">
      <PublicNav />
      <main className="max-w-3xl mx-auto px-6 py-16"><SymptomTriage /></main>
      <PublicFooter />
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify({ '@context': 'https://schema.org', '@type': 'WebApplication', name: 'Symptom Triage', description: 'Simple symptom assessment quiz.', url: 'https://med.doaide.com/tools/symptom-triage', applicationCategory: 'HealthApplication', operatingSystem: 'Web', offers: { '@type': 'Offer', price: '0', priceCurrency: 'USD' }, author: { '@type': 'Organization', name: 'Apprend Technologies', url: 'https://doaide.com' } }) }} />
    </div>
  );
}
