import type { Metadata } from 'next';
import { PublicNav, PublicFooter } from '@/components/PublicLayout';
import { CreatinineClearanceCalculator } from './calculator';

export const metadata: Metadata = {
  title: 'Creatinine Clearance Calculator (Cockcroft-Gault) — DoAide Med',
  description: 'Free Cockcroft-Gault creatinine clearance calculator for renal drug dosing, with renal function categories. No sign-up required.',
  openGraph: {
    title: 'Creatinine Clearance Calculator — DoAide Med',
    description: 'Estimate CrCl with the Cockcroft-Gault equation for renal drug dosing.',
    url: 'https://med.doaide.com/tools/creatinine-clearance-calculator',
  },
};

export default function Page() {
  return (
    <div className="min-h-screen bg-[var(--doaide-bg)]">
      <PublicNav />
      <main className="max-w-3xl mx-auto px-6 py-16"><CreatinineClearanceCalculator /></main>
      <PublicFooter />
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify({ '@context': 'https://schema.org', '@type': 'WebApplication', name: 'Creatinine Clearance Calculator', description: 'Estimate creatinine clearance with the Cockcroft-Gault equation for renal drug dosing.', url: 'https://med.doaide.com/tools/creatinine-clearance-calculator', applicationCategory: 'HealthApplication', operatingSystem: 'Web', offers: { '@type': 'Offer', price: '0', priceCurrency: 'USD' }, author: { '@type': 'Organization', name: 'Apprend Technologies', url: 'https://doaide.com' } }) }} />
    </div>
  );
}
