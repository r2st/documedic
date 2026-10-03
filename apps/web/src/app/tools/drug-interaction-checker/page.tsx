import type { Metadata } from 'next';
import { PublicNav, PublicFooter } from '@/components/PublicLayout';
import { DrugInteractionChecker } from './checker';

export const metadata: Metadata = {
  title: 'Drug Interaction Checker — DoAide Med',
  description: 'Check common drug-drug interactions from a curated database of 20 frequently prescribed medications.',
};

export default function Page() {
  return (
    <div className="min-h-screen bg-[var(--doaide-bg)]">
      <PublicNav />
      <main className="max-w-3xl mx-auto px-6 py-16"><DrugInteractionChecker /></main>
      <PublicFooter />
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify({ '@context': 'https://schema.org', '@type': 'WebApplication', name: 'Drug Interaction Checker', description: 'Check common drug-drug interactions.', url: 'https://med.doaide.com/tools/drug-interaction-checker', applicationCategory: 'HealthApplication', operatingSystem: 'Web', offers: { '@type': 'Offer', price: '0', priceCurrency: 'USD' }, author: { '@type': 'Organization', name: 'Apprend Technologies', url: 'https://doaide.com' } }) }} />
    </div>
  );
}
