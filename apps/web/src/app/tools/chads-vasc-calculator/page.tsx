import type { Metadata } from 'next';
import { PublicNav, PublicFooter } from '@/components/PublicLayout';
import { ChadsVascCalculator } from './calculator';

export const metadata: Metadata = {
  title: 'CHA₂DS₂-VASc Score Calculator — DoAide Med',
  description: 'Free CHA₂DS₂-VASc calculator for stroke risk in atrial fibrillation, with annual risk estimates and anticoagulation guidance. No sign-up required.',
  openGraph: {
    title: 'CHA₂DS₂-VASc Score Calculator — DoAide Med',
    description: 'Estimate stroke risk in non-valvular atrial fibrillation to guide anticoagulation decisions.',
    url: 'https://med.doaide.com/tools/chads-vasc-calculator',
  },
};

export default function Page() {
  return (
    <div className="min-h-screen bg-[var(--doaide-bg)]">
      <PublicNav />
      <main className="max-w-3xl mx-auto px-6 py-16"><ChadsVascCalculator /></main>
      <PublicFooter />
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify({ '@context': 'https://schema.org', '@type': 'WebApplication', name: 'CHA₂DS₂-VASc Score Calculator', description: 'Estimate stroke risk in non-valvular atrial fibrillation to guide anticoagulation decisions.', url: 'https://med.doaide.com/tools/chads-vasc-calculator', applicationCategory: 'HealthApplication', operatingSystem: 'Web', offers: { '@type': 'Offer', price: '0', priceCurrency: 'USD' }, author: { '@type': 'Organization', name: 'Apprend Technologies', url: 'https://doaide.com' } }) }} />
    </div>
  );
}
