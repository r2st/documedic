import type { Metadata } from 'next';
import { PublicNav, PublicFooter } from '@/components/PublicLayout';

export const metadata: Metadata = {
  title: 'Terms of Service — DoAide Med',
  description:
    'Terms and conditions for using DoAide Med clinical decision support system.',
  openGraph: {
    title: 'Terms of Service — DoAide Med',
    description: 'Terms of service for DoAide Med clinical decision support.',
    url: 'https://med.doaide.com/terms',
  },
};

export default function TermsPage() {
  return (
    <div className="min-h-screen bg-[var(--doaide-bg)]">
      <PublicNav />
      <main className="max-w-3xl mx-auto px-6 py-16">
        <h1
          className="text-3xl font-bold text-[var(--doaide-text)] mb-2"
          style={{ fontFamily: 'var(--doaide-font-display)' }}
        >
          Terms of Service
        </h1>
        <p className="text-sm text-[var(--doaide-text-muted)] mb-8">
          Last updated: October 1, 2026
        </p>

        <div className="space-y-8 text-sm text-[var(--doaide-text-secondary)] leading-relaxed">
          <section>
            <h2 className="text-lg font-semibold text-[var(--doaide-text)] mb-3">
              Clinical Decision Support Disclaimer
            </h2>
            <p>
              DoAide Med is a clinical decision support tool designed to assist qualified healthcare
              professionals. It does not provide medical diagnoses, prescribe treatments, or replace
              clinical judgment. All clinical decisions are the responsibility of the treating
              clinician. DoAide Med provides evidence-based suggestions and should be used as one
              input in the clinical decision-making process.
            </p>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[var(--doaide-text)] mb-3">Intended Users</h2>
            <p>
              DoAide Med is intended for use by licensed healthcare professionals, including
              physicians, pharmacists, and nurse practitioners. The free clinical tools (BMI
              Calculator, Drug Interaction Checker, Symptom Triage) are available for informational
              purposes to any user, but are not a substitute for professional medical advice.
            </p>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[var(--doaide-text)] mb-3">
              Limitation of Liability
            </h2>
            <p>
              DoAide Med and Apprend Technologies shall not be liable for clinical decisions made
              using the platform. The platform provides information and suggestions based on
              available evidence, but cannot account for all patient-specific factors. Users are
              responsible for verifying all clinical information independently.
            </p>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[var(--doaide-text)] mb-3">Account Terms</h2>
            <ul className="list-disc list-inside space-y-1">
              <li>You must provide accurate registration information</li>
              <li>You are responsible for maintaining the confidentiality of your account</li>
              <li>You must not share your account credentials with others</li>
              <li>You must comply with all applicable healthcare regulations</li>
            </ul>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[var(--doaide-text)] mb-3">
              Acceptable Use
            </h2>
            <p>
              You agree not to misuse DoAide Med, including but not limited to: using the platform
              for purposes other than clinical decision support, attempting to reverse-engineer the
              AI models, or using the platform to generate misleading medical information.
            </p>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[var(--doaide-text)] mb-3">Governing Law</h2>
            <p>
              These terms are governed by the laws of India. Any disputes shall be subject to the
              exclusive jurisdiction of courts in Bengaluru, Karnataka.
            </p>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[var(--doaide-text)] mb-3">Contact</h2>
            <p>
              For questions about these terms, contact us at{' '}
              <a href="mailto:legal@doaide.com" className="text-[var(--doaide-gold)] hover:text-[var(--doaide-gold-light)]">
                legal@doaide.com
              </a>.
            </p>
          </section>
        </div>
      </main>
      <PublicFooter />
    </div>
  );
}
