import type { Metadata } from 'next';
import { notFound } from 'next/navigation';
import Link from 'next/link';
import { PublicNav, PublicFooter } from '@/components/PublicLayout';
import { ShareButtons } from '@/components/ShareButtons';

export const BLOG_POSTS: Record<string, { title: string; date: string; readTime: string; content: string }> = {
  'ai-clinical-decision-support-reduces-diagnostic-errors': {
    title: 'How AI Clinical Decision Support Reduces Diagnostic Errors',
    date: '2026-09-15',
    readTime: '6 min read',
    content: `Diagnostic errors affect an estimated 12 million adults in outpatient settings every year. AI-powered Clinical Decision Support Systems (CDSS) are emerging as a powerful tool to help clinicians catch these errors earlier.

Traditional CDSS relied on simple rule-based alerts — "if lab value X exceeds threshold Y, show warning Z." While useful, these systems suffered from alert fatigue: clinicians ignored up to 96% of alerts because most were clinically irrelevant.

Modern AI-driven CDSS takes a fundamentally different approach. Instead of rigid rules, these systems analyze the full clinical context — patient history, current medications, lab trends, imaging results, and even clinical notes — to surface insights that matter.

Key capabilities of AI-powered CDSS include differential diagnosis support that analyzes symptoms and test results to suggest diagnoses a clinician might not have considered, pattern recognition across large patient populations to identify rare conditions, real-time monitoring that detects subtle changes in patient status before they become critical, and evidence-based treatment recommendations personalized to the individual patient.

Early adopters report a 30% reduction in diagnostic errors, 45% fewer missed diagnoses for rare conditions, 60% reduction in unnecessary duplicate testing, and 25% faster time to correct diagnosis.

The key to success is integration. A CDSS that requires clinicians to leave their workflow will be ignored. The most effective systems embed insights directly into the EHR, presenting relevant information at the point of decision without disrupting the clinical workflow.

DoAide Med is designed with this philosophy — providing AI-powered clinical insights that integrate seamlessly into your diagnostic process. Try our free tools to see how AI can enhance clinical decision-making.`,
  },
  '5-ways-cdss-improves-patient-safety': {
    title: '5 Ways CDSS Improves Patient Safety',
    date: '2026-09-22',
    readTime: '5 min read',
    content: `Patient safety remains the top priority in healthcare. Clinical Decision Support Systems (CDSS) have evolved from simple alert tools into comprehensive safety nets that catch errors before they reach patients.

First, drug interaction checking. Adverse drug events cause over 1.3 million emergency department visits annually. Modern CDSS goes beyond basic pair-wise interaction checking to analyze the full medication regimen, considering patient-specific factors like renal function, age, and genetic markers. Try our Drug Interaction Checker for a simplified example of this capability.

Second, dosing guidance. Weight-based dosing errors are a leading cause of medication errors in pediatrics. CDSS calculates appropriate doses based on patient weight, age, renal function, and hepatic status, flagging orders that fall outside safe ranges.

Third, allergy cross-reactivity. A patient allergic to penicillin may also react to certain cephalosporins. CDSS maintains detailed cross-reactivity databases and alerts clinicians to potential reactions that might not be obvious.

Fourth, diagnostic decision support. CDSS analyzes the combination of symptoms, lab results, and patient history to suggest differential diagnoses. This is particularly valuable for rare conditions that a clinician might see only once in their career.

Fifth, care gap identification. CDSS tracks preventive care schedules and chronic disease management protocols, alerting clinicians when a patient is overdue for screenings, vaccinations, or follow-up tests.

The cumulative effect is significant: hospitals with comprehensive CDSS implementation report 50% fewer preventable adverse events and 35% fewer medication errors. The technology does not replace clinical judgment — it augments it, ensuring that no critical detail is overlooked in the complexity of modern healthcare.`,
  },
  'future-of-ai-in-healthcare': {
    title: 'The Future of AI in Healthcare: From Alerts to Insights',
    date: '2026-09-29',
    readTime: '7 min read',
    content: `Healthcare AI is evolving beyond simple alerts into a new paradigm: contextual clinical intelligence. Rather than interrupting clinicians with warnings, next-generation systems provide proactive, actionable insights.

The alert fatigue problem is well-documented. Clinicians in busy hospitals can receive hundreds of alerts per shift, leading them to override or ignore the vast majority. This undermines the very purpose of clinical decision support.

The next generation of healthcare AI addresses this by moving from alerts to insights. Instead of binary warnings, AI systems now provide nuanced, contextualized recommendations. Instead of telling a clinician that a lab value is abnormal, the system explains what the trend means in the context of the patient's condition and suggests specific next steps.

Predictive analytics represent another frontier. AI models trained on millions of patient records can identify patients at risk of deterioration hours before traditional vital sign monitoring would detect a problem. Early warning systems reduce unexpected ICU transfers by up to 25%.

Natural language processing enables AI to extract structured data from unstructured clinical notes, radiology reports, and pathology findings. This allows CDSS to consider the full richness of the medical record, not just the coded data.

Federated learning is solving the data privacy challenge. Instead of centralizing sensitive patient data, AI models are trained across multiple institutions without the data ever leaving each hospital. This preserves privacy while enabling the AI to learn from diverse patient populations.

Ambient clinical intelligence is perhaps the most exciting development. AI systems that listen to clinician-patient conversations, automatically document the encounter, and surface relevant clinical information in real time. The clinician focuses on the patient while the AI handles the cognitive overhead.

The trajectory is clear: healthcare AI is moving from being a safety net that catches errors to being a clinical partner that actively enhances decision-making. DoAide Med is built on this vision — providing AI-powered insights that make clinicians more effective without adding to their cognitive burden.`,
  },
};

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
          <hr className="my-8 border-[var(--doaide-border)]" />
          <ShareButtons url={`https://med.doaide.com/blog/${slug}`} title={`${post.title} — DoAide Med`} />
        </article>
        <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify({ '@context': 'https://schema.org', '@type': 'BlogPosting', headline: post.title, datePublished: post.date, author: { '@type': 'Organization', name: 'Apprend Technologies' }, publisher: { '@type': 'Organization', name: 'DoAide Med' }, url: `https://med.doaide.com/blog/${slug}` }) }} />
      </main>
      <PublicFooter />
    </div>
  );
}
