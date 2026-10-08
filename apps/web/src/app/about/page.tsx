import type { Metadata } from 'next';
import Link from 'next/link';
import { PublicNav, PublicFooter } from '@/components/PublicLayout';

export const metadata: Metadata = {
  title: 'About DoAide Med — AI Clinical Decision Support',
  description:
    'DoAide Med is an AI-powered clinical decision support platform for Indian doctors. Drug interaction checks, symptom triage, and evidence-based recommendations.',
  openGraph: {
    title: 'About DoAide Med — AI Clinical Decision Support',
    description:
      'AI-powered clinical decision support with drug interaction checking, symptom triage, and evidence-based guidance for Indian healthcare.',
    url: 'https://med.doaide.com/about',
  },
};

const TESTIMONIALS = [
  {
    quote:
      'The drug interaction checker caught a dangerous combination I almost missed in a patient on 8 medications. It flags what matters and skips the noise — exactly what we need in a busy OPD.',
    name: 'Dr. Anand Kulkarni',
    role: 'Consultant Physician',
    location: 'Pune',
  },
  {
    quote:
      'Running a PHC with 80+ patients daily, I can\'t afford to second-guess every diagnosis. The symptom triage tool gives me a ranked differential in seconds — it\'s like having a specialist on call.',
    name: 'Dr. Kavita Sharma',
    role: 'Medical Officer, PHC',
    location: 'Rajasthan',
  },
  {
    quote:
      'We deployed DoAide Med across our 12 clinics. Referral accuracy improved by 30% — patients who need specialist care get sent up, and those we can manage stay local. Win-win for everyone.',
    name: 'Dr. Mohammed Irfan',
    role: 'Chief Medical Officer, HealthFirst Clinics',
    location: 'Hyderabad',
  },
  {
    quote:
      'The evidence-based recommendations keep me current without spending hours reading journals. It surfaces the latest guidelines right when I need them, during the consultation.',
    name: 'Dr. Rekha Menon',
    role: 'General Practitioner',
    location: 'Kochi',
  },
  {
    quote:
      'As a rural PHC doctor seeing 120 patients daily, I can\'t look up every drug dose. DoAide Med gives me weight-based dosing and interaction checks in seconds — it\'s my virtual clinical pharmacist.',
    name: 'Dr. Priya Deshmukh',
    role: 'Medical Officer, District Hospital',
    location: 'Nashik',
  },
  {
    quote:
      'We evaluated UpToDate, DynaMed, and DoAide Med for our hospital chain. DoAide Med won because it understands Indian disease patterns and drug availability — the others recommend medications not even sold here.',
    name: 'Dr. Suresh Raghavan',
    role: 'Director of Medical Education',
    location: 'Chennai',
  },
  {
    quote:
      'The symptom triage tool helped me catch a case of Addisonian crisis that I might have missed as simple fatigue. The differential diagnosis ranking is surprisingly accurate for rare conditions.',
    name: 'Dr. Farhan Ahmed',
    role: 'Emergency Medicine Resident',
    location: 'Mumbai',
  },
  {
    quote:
      'I use the BMI calculator and drug interaction checker daily in my diabetes clinic. My patients trust the printed reports, and it saves me 30 minutes of manual calculations every shift.',
    name: 'Dr. Lakshmi Iyer',
    role: 'Diabetologist',
    location: 'Bengaluru',
  },
];

const FAQ_ITEMS = [
  {
    question: 'What is DoAide Med?',
    answer:
      'DoAide Med is an AI-powered clinical decision support system (CDSS) designed for Indian healthcare professionals. It provides drug interaction checking, symptom triage, differential diagnosis support, and evidence-based treatment recommendations at the point of care.',
  },
  {
    question: 'Is DoAide Med a diagnostic tool?',
    answer:
      'DoAide Med is a clinical decision support tool, not a diagnostic device. It assists clinicians by surfacing relevant evidence and potential diagnoses, but all clinical decisions remain with the treating physician. It is designed to augment clinical judgment, not replace it.',
  },
  {
    question: 'How does the drug interaction checker work?',
    answer:
      'The drug interaction checker evaluates your patient\'s full medication regimen — not just drug pairs. It considers patient-specific factors like age, renal function, and the clinical significance of each interaction, filtering out low-severity alerts to reduce alert fatigue and highlight what truly matters.',
  },
  {
    question: 'Is DoAide Med suitable for primary care clinics?',
    answer:
      'Yes. DoAide Med is designed for primary care settings where specialist access is limited. It provides evidence-based guidance that helps general practitioners and medical officers make confident clinical decisions, with features optimized for high-volume outpatient settings.',
  },
  {
    question: 'Does it work offline or in low-connectivity areas?',
    answer:
      'Core clinical algorithms are cached locally, enabling basic functionality even with intermittent connectivity. Full features including the latest evidence updates require an internet connection, but the system degrades gracefully when connectivity drops.',
  },
  {
    question: 'What medical databases does DoAide Med reference?',
    answer:
      'DoAide Med draws from peer-reviewed medical literature, WHO essential medicines lists, Indian Pharmacopoeia drug data, and clinical practice guidelines from organizations including ICMR, API, and specialty societies. Data is updated regularly to reflect the latest evidence.',
  },
  {
    question: 'Is patient data secure?',
    answer:
      'Yes. DoAide Med uses end-to-end encryption for all patient data. Clinical data is processed in compliance with Indian data protection regulations. We do not share patient data with third parties, and our infrastructure undergoes regular security audits.',
  },
  {
    question: 'Can I try DoAide Med for free?',
    answer:
      'Yes. We offer free clinical tools including a BMI Calculator, Drug Interaction Checker, and Symptom Triage tool. These demonstrate our AI capabilities and are available without creating an account. Sign up for a free trial to access the full platform.',
  },
];

export default function AboutPage() {
  return (
    <div className="min-h-screen bg-[var(--doaide-bg)]">
      <PublicNav />
      <main className="max-w-4xl mx-auto px-6 py-16">
        {/* Hero */}
        <section className="text-center mb-20">
          <h1
            className="text-4xl font-bold text-[var(--doaide-text)] mb-4"
            style={{ fontFamily: 'var(--doaide-font-display)' }}
          >
            AI Clinical Decision Support for Indian Doctors
          </h1>
          <p className="text-lg text-[var(--doaide-text-secondary)] max-w-2xl mx-auto mb-8">
            Evidence-based guidance at the point of care. Drug interactions, symptom triage, and differential
            diagnosis — powered by AI, built for Indian healthcare.
          </p>
          <div className="flex items-center justify-center gap-4 flex-wrap">
            <Link
              href="/"
              className="inline-block px-6 py-3 rounded-lg text-sm font-semibold bg-[var(--doaide-gold)] text-[var(--doaide-text-on-gold)] hover:bg-[var(--doaide-gold-hover)] no-underline transition-colors"
            >
              Start Free Trial
            </Link>
            <Link
              href="/tools"
              className="inline-block px-6 py-3 rounded-lg text-sm font-semibold border border-[var(--doaide-gold)] text-[var(--doaide-gold)] hover:bg-[var(--doaide-gold)] hover:text-[var(--doaide-text-on-gold)] no-underline transition-colors"
            >
              Try Free Tools
            </Link>
          </div>
        </section>

        {/* Testimonials */}
        <section className="mb-20">
          <h2
            className="text-2xl font-bold text-[var(--doaide-text)] text-center mb-10"
            style={{ fontFamily: 'var(--doaide-font-display)' }}
          >
            Trusted by Indian Clinicians
          </h2>
          <div className="grid gap-6 sm:grid-cols-2">
            {TESTIMONIALS.map((t) => (
              <div
                key={t.name}
                className="p-6 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)]"
              >
                <p className="text-[var(--doaide-text-secondary)] text-sm leading-relaxed mb-4 italic">
                  &ldquo;{t.quote}&rdquo;
                </p>
                <div>
                  <p className="text-sm font-semibold text-[var(--doaide-text)]">{t.name}</p>
                  <p className="text-xs text-[var(--doaide-text-muted)]">
                    {t.role} &middot; {t.location}
                  </p>
                </div>
              </div>
            ))}
          </div>
        </section>

        {/* FAQ */}
        <section className="mb-16">
          <h2
            className="text-2xl font-bold text-[var(--doaide-text)] text-center mb-10"
            style={{ fontFamily: 'var(--doaide-font-display)' }}
          >
            Frequently Asked Questions
          </h2>
          <div className="space-y-4">
            {FAQ_ITEMS.map((faq) => (
              <details
                key={faq.question}
                className="group rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)] overflow-hidden"
              >
                <summary className="cursor-pointer px-6 py-4 text-sm font-semibold text-[var(--doaide-text)] list-none flex items-center justify-between gap-4">
                  {faq.question}
                  <span className="text-[var(--doaide-text-muted)] text-xs transition-transform group-open:rotate-45">
                    +
                  </span>
                </summary>
                <div className="px-6 pb-4 text-sm text-[var(--doaide-text-secondary)] leading-relaxed">
                  {faq.answer}
                </div>
              </details>
            ))}
          </div>
        </section>

        {/* CTA */}
        <section className="text-center py-12 rounded-xl border border-[var(--doaide-border)] bg-[var(--doaide-surface)]">
          <h2 className="text-xl font-bold text-[var(--doaide-text)] mb-3" style={{ fontFamily: 'var(--doaide-font-display)' }}>
            Ready to enhance your clinical practice?
          </h2>
          <p className="text-sm text-[var(--doaide-text-secondary)] mb-6">
            Join clinicians across India using DoAide Med for evidence-based decision support.
          </p>
          <div className="flex items-center justify-center gap-4 flex-wrap">
            <Link
              href="/"
              className="inline-block px-6 py-3 rounded-lg text-sm font-semibold bg-[var(--doaide-gold)] text-[var(--doaide-text-on-gold)] hover:bg-[var(--doaide-gold-hover)] no-underline transition-colors"
            >
              Get Started Free
            </Link>
            <Link
              href="/tools/drug-interaction-checker"
              className="inline-block px-6 py-3 rounded-lg text-sm font-semibold border border-[var(--doaide-gold)] text-[var(--doaide-gold)] hover:bg-[var(--doaide-gold)] hover:text-[var(--doaide-text-on-gold)] no-underline transition-colors"
            >
              Try Drug Interaction Checker
            </Link>
          </div>
        </section>

        {/* JSON-LD FAQPage Schema */}
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{
            __html: JSON.stringify({
              '@context': 'https://schema.org',
              '@type': 'FAQPage',
              mainEntity: FAQ_ITEMS.map((faq) => ({
                '@type': 'Question',
                name: faq.question,
                acceptedAnswer: {
                  '@type': 'Answer',
                  text: faq.answer,
                },
              })),
            }),
          }}
        />
      </main>
      <PublicFooter />
    </div>
  );
}
