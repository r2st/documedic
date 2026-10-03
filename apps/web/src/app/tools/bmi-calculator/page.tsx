import type { Metadata } from 'next';
import { PublicNav, PublicFooter } from '@/components/PublicLayout';
import { BmiCalculator } from './calculator';

export const metadata: Metadata = {
  title: 'BMI Calculator — DoAide Med',
  description: 'Calculate Body Mass Index with health category and personalized recommendations.',
};

export default function Page() {
  return (
    <div className="min-h-screen bg-[var(--doaide-bg)]">
      <PublicNav />
      <main className="max-w-3xl mx-auto px-6 py-16"><BmiCalculator /></main>
      <PublicFooter />
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify({ '@context': 'https://schema.org', '@type': 'WebApplication', name: 'BMI Calculator', description: 'Calculate Body Mass Index with health category.', url: 'https://med.doaide.com/tools/bmi-calculator', applicationCategory: 'HealthApplication', operatingSystem: 'Web', offers: { '@type': 'Offer', price: '0', priceCurrency: 'USD' }, author: { '@type': 'Organization', name: 'Apprend Technologies', url: 'https://doaide.com' } }) }} />
    </div>
  );
}
