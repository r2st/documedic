import type { Metadata } from 'next';
import { PublicNav, PublicFooter } from '@/components/PublicLayout';
import { EmbedGenerator } from './generator';

export const metadata: Metadata = {
  title: 'Embed Widget Generator — DoAide Med',
  description: 'Generate an embeddable DoAide Med clinical support widget for your healthcare application.',
};

export default function Page() {
  return (
    <div className="min-h-screen bg-[var(--doaide-bg)]">
      <PublicNav />
      <main className="max-w-3xl mx-auto px-6 py-16"><EmbedGenerator /></main>
      <PublicFooter />
    </div>
  );
}
