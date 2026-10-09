import type { Metadata } from 'next';
import { PublicNav, PublicFooter } from '@/components/PublicLayout';

export const metadata: Metadata = {
  title: 'Privacy Policy — DoAide Med',
  description:
    'How DoAide Med collects, uses, and protects your data. Compliant with the Digital Personal Data Protection (DPDP) Act 2023.',
  openGraph: {
    title: 'Privacy Policy — DoAide Med',
    description: 'Data protection practices for DoAide Med clinical decision support.',
    url: 'https://med.doaide.com/privacy',
  },
};

export default function PrivacyPage() {
  return (
    <div className="min-h-screen bg-[var(--doaide-bg)]">
      <PublicNav />
      <main className="max-w-3xl mx-auto px-6 py-16">
        <h1
          className="text-3xl font-bold text-[var(--doaide-text)] mb-2"
          style={{ fontFamily: 'var(--doaide-font-display)' }}
        >
          Privacy Policy
        </h1>
        <p className="text-sm text-[var(--doaide-text-muted)] mb-8">
          Last updated: October 1, 2026
        </p>

        <div className="space-y-8 text-sm text-[var(--doaide-text-secondary)] leading-relaxed">
          <section>
            <h2 className="text-lg font-semibold text-[var(--doaide-text)] mb-3">Overview</h2>
            <p>
              DoAide Med is operated by Apprend Technologies. We are committed to protecting the
              privacy and security of your personal and clinical data in compliance with the Digital
              Personal Data Protection (DPDP) Act, 2023 and applicable Indian regulations.
            </p>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[var(--doaide-text)] mb-3">Data We Collect</h2>
            <ul className="list-disc list-inside space-y-1">
              <li>Account information (email address, display name) when you register</li>
              <li>Clinical data you enter for decision support (patient demographics, symptoms, medications)</li>
              <li>Usage data (pages visited, features used) through privacy-respecting analytics</li>
              <li>Device information (browser type, screen size) for performance optimization</li>
            </ul>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[var(--doaide-text)] mb-3">How We Use Your Data</h2>
            <ul className="list-disc list-inside space-y-1">
              <li>To provide clinical decision support services</li>
              <li>To maintain audit logs as required for clinical safety</li>
              <li>To improve our AI models and service quality</li>
              <li>To communicate with you about your account</li>
            </ul>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[var(--doaide-text)] mb-3">Data Protection</h2>
            <p>
              All data is encrypted in transit (TLS 1.3) and at rest. Clinical data is processed
              and stored on servers located in India, in compliance with DPDP data residency
              requirements. We do not sell, rent, or share patient data with third parties.
            </p>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[var(--doaide-text)] mb-3">Your Rights</h2>
            <p>
              Under the DPDP Act, you have the right to access, correct, and delete your personal
              data. You can request data export or deletion by contacting us at{' '}
              <a href="mailto:privacy@doaide.com" className="text-[var(--doaide-gold)] hover:text-[var(--doaide-gold-light)]">
                privacy@doaide.com
              </a>.
            </p>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[var(--doaide-text)] mb-3">Analytics</h2>
            <p>
              We use self-hosted, privacy-respecting analytics (Umami) to understand how our tools
              are used. This analytics system does not use cookies, does not collect personally
              identifiable information, and is GDPR/DPDP compliant.
            </p>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[var(--doaide-text)] mb-3">Free Clinical Tools</h2>
            <p>
              Our free tools (BMI Calculator, Drug Interaction Checker, Symptom Triage) can be used
              without creating an account. Data entered into these tools is processed locally in your
              browser and is not stored on our servers.
            </p>
          </section>

          <section>
            <h2 className="text-lg font-semibold text-[var(--doaide-text)] mb-3">Contact</h2>
            <p>
              For privacy-related inquiries, contact our Data Protection Officer at{' '}
              <a href="mailto:privacy@doaide.com" className="text-[var(--doaide-gold)] hover:text-[var(--doaide-gold-light)]">
                privacy@doaide.com
              </a>.
            </p>
          </section>
        </div>
      </main>
      <PublicFooter />
    </div>
  );
}
