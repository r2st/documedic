import type { Metadata } from 'next';
import { PublicNav, PublicFooter } from '@/components/PublicLayout';
import { EmbedGenerator } from './generator';

export const metadata: Metadata = {
  title: 'Embed Widget Generator — DoAide Med',
  description:
    'Generate an embeddable DoAide Med clinical support widget for your healthcare application or EHR system.',
  openGraph: {
    title: 'Embed Widget Generator — DoAide Med',
    description:
      'Add AI-powered clinical decision support to your healthcare application with an embeddable widget.',
    url: 'https://med.doaide.com/embed',
  },
};

export default function Page() {
  return (
    <div className="min-h-screen bg-[var(--doaide-bg)]">
      <PublicNav />
      <main className="max-w-3xl mx-auto px-6 py-16">
        <EmbedGenerator />
      </main>
      <PublicFooter />
    </div>
  );
}
