import type { Metadata } from 'next';
import { notFound } from 'next/navigation';
import Link from 'next/link';
import { CrossProductLinks } from '@/components/CrossProductLinks';
import { PublicNav, PublicFooter } from '@/components/PublicLayout';
import { ShareButtons } from '@/components/ShareButtons';
import { BLOG_POSTS } from '../data';

interface Props {
  params: Promise<{ slug: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { slug } = await params;
  const post = BLOG_POSTS[slug];
  if (!post) return {};
  return {
    title: `${post.title} — DoAide Med`,
    description: post.content.slice(0, 155).replace(/\n/g, ' '),
    openGraph: { title: post.title, url: `https://med.doaide.com/blog/${slug}`, type: 'article', publishedTime: post.date },
  };
}

export function generateStaticParams() {
  return Object.keys(BLOG_POSTS).map((slug) => ({ slug }));
}

export default async function BlogPost({ params }: Props) {
  const { slug } = await params;
  const post = BLOG_POSTS[slug];
  if (!post) notFound();

  return (
    <div className="min-h-screen bg-[var(--doaide-bg)]">
      <PublicNav />
      <main className="max-w-3xl mx-auto px-6 py-16">
        <Link href="/blog" className="text-sm text-[var(--doaide-text-muted)] hover:text-[var(--doaide-gold)] no-underline mb-6 inline-block">&larr; Back to Blog</Link>
        <article>
          <div className="flex items-center gap-3 mb-3 text-xs text-[var(--doaide-text-muted)]">
            <time>{post.date}</time><span>&middot;</span><span>{post.readTime}</span>
          </div>
          <h1 className="text-3xl font-bold text-[var(--doaide-text)] mb-8" style={{ fontFamily: 'var(--doaide-font-display)' }}>{post.title}</h1>
          <div className="prose prose-invert max-w-none text-[var(--doaide-text-secondary)] [&_p]:mb-4 [&_p]:leading-relaxed">
            {post.content.split('\n\n').map((block, i) => <p key={i}>{block}</p>)}
          </div>
          {post.faqs && post.faqs.length > 0 && (
            <section className="mt-12">
              <h2 className="text-xl font-bold text-[var(--doaide-text)] mb-6" style={{ fontFamily: 'var(--doaide-font-display)' }}>Frequently Asked Questions</h2>
              <div className="space-y-6">
                {post.faqs.map((faq, i) => (
                  <div key={i} className="p-5 rounded-lg border border-[var(--doaide-border)] bg-[var(--doaide-surface)]">
                    <h3 className="text-base font-semibold text-[var(--doaide-text)] mb-2">{faq.question}</h3>
                    <p className="text-sm text-[var(--doaide-text-secondary)] leading-relaxed">{faq.answer}</p>
                  </div>
                ))}
              </div>
            </section>
          )}
          <hr className="my-8 border-[var(--doaide-border)]" />
          <ShareButtons url={`https://med.doaide.com/blog/${slug}`} title={`${post.title} — DoAide Med`} />
        </article>
        <CrossProductLinks page="blog" />
        <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify({ '@context': 'https://schema.org', '@type': 'BlogPosting', headline: post.title, datePublished: post.date, author: { '@type': 'Organization', name: 'Apprend Technologies' }, publisher: { '@type': 'Organization', name: 'DoAide Med' }, url: `https://med.doaide.com/blog/${slug}` }) }} />
        {post.faqs && post.faqs.length > 0 && (
          <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify({ '@context': 'https://schema.org', '@type': 'FAQPage', mainEntity: post.faqs.map((faq) => ({ '@type': 'Question', name: faq.question, acceptedAnswer: { '@type': 'Answer', text: faq.answer } })) }) }} />
        )}
      </main>
      <PublicFooter />
    </div>
  );
}
