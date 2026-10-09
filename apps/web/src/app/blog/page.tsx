import type { Metadata } from 'next';
import Link from 'next/link';
import { PublicNav, PublicFooter } from '@/components/PublicLayout';
import { POSTS } from './data';

export const metadata: Metadata = {
  title: 'Blog — AI Clinical Decision Support Insights | DoAide Med',
  description:
    'Insights on AI clinical decision support, patient safety, drug interaction checking, and healthcare technology for Indian doctors.',
  openGraph: {
    title: 'Blog — DoAide Med',
    description:
      'Insights on AI clinical decision support, patient safety, and healthcare technology.',
    url: 'https://med.doaide.com/blog',
  },
};

export default function BlogIndex() {
  return (
    <div className="min-h-screen bg-[var(--doaide-bg)]">
      <PublicNav />
      <main className="max-w-3xl mx-auto px-6 py-16">
        <h1
          className="text-3xl font-bold text-[var(--doaide-text)] mb-2"
          style={{ fontFamily: 'var(--doaide-font-display)' }}
        >
          Blog
        </h1>
        <p className="text-[var(--doaide-text-secondary)] mb-10">
          Insights on AI clinical decision support and healthcare technology.
        </p>
        <div className="space-y-8">
          {POSTS.map((post) => (
            <Link
              key={post.slug}
              href={`/blog/${post.slug}`}
              className="block p-6 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)] hover:border-[var(--doaide-gold)] transition-colors no-underline group"
            >
              <div className="flex items-center gap-3 mb-2 text-xs text-[var(--doaide-text-muted)]">
                <time>{post.date}</time>
                <span>&middot;</span>
                <span>{post.readTime}</span>
              </div>
              <h2 className="text-lg font-semibold text-[var(--doaide-text)] group-hover:text-[var(--doaide-gold)] transition-colors mb-2">
                {post.title}
              </h2>
              <p className="text-sm text-[var(--doaide-text-secondary)]">{post.excerpt}</p>
            </Link>
          ))}
        </div>

        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{
            __html: JSON.stringify({
              '@context': 'https://schema.org',
              '@type': 'Blog',
              name: 'DoAide Med Blog',
              description:
                'Insights on AI clinical decision support, patient safety, and healthcare technology.',
              url: 'https://med.doaide.com/blog',
              publisher: {
                '@type': 'Organization',
                name: 'DoAide Med',
                url: 'https://med.doaide.com',
              },
              blogPost: POSTS.map((post) => ({
                '@type': 'BlogPosting',
                headline: post.title,
                datePublished: post.date,
                description: post.excerpt,
                url: `https://med.doaide.com/blog/${post.slug}`,
              })),
            }),
          }}
        />
      </main>
      <PublicFooter />
    </div>
  );
}
