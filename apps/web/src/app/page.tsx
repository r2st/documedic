'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/auth';

function RobotLogo({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 400 320"
      className={className}
      aria-hidden="true"
      xmlns="http://www.w3.org/2000/svg"
    >
      <defs>
        <linearGradient id="hero-hg" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#F0B429" />
          <stop offset="100%" stopColor="#D4A017" />
        </linearGradient>
      </defs>
      <line x1="200" y1="45" x2="200" y2="20" stroke="#F0B429" strokeWidth="6" strokeLinecap="round" />
      <circle cx="200" cy="14" r="10" fill="#F0B429" />
      <circle cx="200" cy="14" r="5" fill="#F7CC5F" />
      <rect x="110" y="50" width="180" height="140" rx="35" fill="url(#hero-hg)" />
      <rect x="130" y="68" width="140" height="105" rx="25" fill="#D4A017" opacity="0.4" />
      <ellipse cx="165" cy="115" rx="18" ry="20" fill="#0A0A0B" />
      <ellipse cx="235" cy="115" rx="18" ry="20" fill="#0A0A0B" />
      <circle cx="170" cy="113" r="8" fill="#F7CC5F" />
      <circle cx="240" cy="113" r="8" fill="#F7CC5F" />
      <circle cx="174" cy="109" r="3" fill="white" opacity="0.7" />
      <circle cx="244" cy="109" r="3" fill="white" opacity="0.7" />
      <path d="M170 155Q200 178 230 155" stroke="#0A0A0B" strokeWidth="4" fill="none" strokeLinecap="round" />
      <rect x="92" y="95" width="22" height="45" rx="8" fill="#D4A017" />
      <rect x="286" y="95" width="22" height="45" rx="8" fill="#D4A017" />
      <rect x="175" y="190" width="50" height="14" rx="5" fill="#D4A017" />
      <rect x="145" y="204" width="110" height="55" rx="18" fill="url(#hero-hg)" />
      <circle cx="200" cy="228" r="7" fill="#0A0A0B" />
      <circle cx="200" cy="228" r="3.5" fill="#0A0A0B" />
      <path d="M145 218Q118 223 113 240Q108 257 120 262" stroke="#D4A017" strokeWidth="9" fill="none" strokeLinecap="round" />
      <circle cx="120" cy="265" r="7" fill="#D4A017" />
      <path d="M255 218Q282 223 287 240Q292 257 280 262" stroke="#D4A017" strokeWidth="9" fill="none" strokeLinecap="round" />
      <circle cx="280" cy="265" r="7" fill="#D4A017" />
    </svg>
  );
}

const FEATURES = [
  {
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="landing-feature-icon">
        <path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2" />
        <rect x="9" y="3" width="6" height="4" rx="1" />
        <path d="M9 14l2 2 4-4" />
      </svg>
    ),
    title: 'Multi-Agent Diagnosis',
    description: 'Eight specialised digital robots debate, cross-check, and verify before any suggestion reaches you.',
  },
  {
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="landing-feature-icon">
        <path d="M12 9v2m0 4h.01" />
        <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
      </svg>
    ),
    title: 'Drug Safety Checks',
    description: 'Allergy cross-checks, contraindication detection, and interaction alerts — all working offline.',
  },
  {
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="landing-feature-icon">
        <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" />
        <polyline points="14 2 14 8 20 8" />
        <line x1="16" y1="13" x2="8" y2="13" />
        <line x1="16" y1="17" x2="8" y2="17" />
        <polyline points="10 9 9 9 8 9" />
      </svg>
    ),
    title: 'Guideline-Cited Options',
    description: 'Every management suggestion traces back to ICMR, WHO, or NICE clinical guidelines.',
  },
  {
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="landing-feature-icon">
        <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
        <circle cx="12" cy="12" r="3" />
      </svg>
    ),
    title: 'Transparent Reasoning',
    description: 'See the evidence before the conclusion. Every agent\'s perspective — including dissent — is shown.',
  },
] as const;

export default function LandingPage() {
  const { account, loading } = useAuth();
  const router = useRouter();
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (!loading && account) {
      router.replace('/patients');
    }
  }, [account, loading, router]);

  if (loading || account) {
    return (
      <main className="grid min-h-screen place-items-center">
        <div className="text-center">
          <div className="mx-auto mb-3 h-8 w-8 animate-spin rounded-full border-2 border-[#2A2A2D] border-t-[#F0B429]" />
          <p className="text-sm text-[#9CA3AF]">Loading&hellip;</p>
        </div>
      </main>
    );
  }

  return (
    <>
      <style>{`
        .landing-page {
          min-height: 100vh;
          background: #0A0A0B;
          overflow-x: hidden;
        }

        /* --- Animated gradient background --- */
        .landing-bg {
          position: fixed;
          inset: 0;
          z-index: 0;
          pointer-events: none;
          overflow: hidden;
        }
        .landing-bg-orb {
          position: absolute;
          border-radius: 50%;
          filter: blur(120px);
          opacity: 0;
          animation: orbFadeIn 2s ease-out forwards;
        }
        .landing-bg-orb--gold {
          width: 600px;
          height: 600px;
          background: radial-gradient(circle, rgba(240,180,41,0.12) 0%, transparent 70%);
          top: -200px;
          right: -100px;
          animation-delay: 0.3s;
        }
        .landing-bg-orb--teal {
          width: 500px;
          height: 500px;
          background: radial-gradient(circle, rgba(45,212,191,0.08) 0%, transparent 70%);
          bottom: -150px;
          left: -100px;
          animation-delay: 0.6s;
        }
        .landing-bg-orb--blue {
          width: 400px;
          height: 400px;
          background: radial-gradient(circle, rgba(96,165,250,0.06) 0%, transparent 70%);
          top: 40%;
          left: 50%;
          transform: translateX(-50%);
          animation-delay: 0.9s;
        }
        @keyframes orbFadeIn {
          from { opacity: 0; transform: scale(0.8); }
          to { opacity: 1; transform: scale(1); }
        }

        /* --- Grid pattern overlay --- */
        .landing-grid {
          position: fixed;
          inset: 0;
          z-index: 0;
          pointer-events: none;
          background-image:
            linear-gradient(rgba(255,255,255,0.02) 1px, transparent 1px),
            linear-gradient(90deg, rgba(255,255,255,0.02) 1px, transparent 1px);
          background-size: 60px 60px;
          mask-image: radial-gradient(ellipse at 50% 30%, black 20%, transparent 70%);
          -webkit-mask-image: radial-gradient(ellipse at 50% 30%, black 20%, transparent 70%);
        }

        /* --- Nav --- */
        .landing-nav {
          position: relative;
          z-index: 10;
          display: flex;
          align-items: center;
          justify-content: space-between;
          max-width: 1100px;
          margin: 0 auto;
          padding: 20px 24px;
          opacity: 0;
          animation: fadeSlideDown 0.6s ease-out 0.2s forwards;
        }
        @keyframes fadeSlideDown {
          from { opacity: 0; transform: translateY(-10px); }
          to { opacity: 1; transform: translateY(0); }
        }
        .landing-nav-brand {
          display: flex;
          align-items: center;
          gap: 10px;
          text-decoration: none !important;
        }
        .landing-nav-robot {
          width: 36px;
          height: 29px;
        }
        .landing-nav-title {
          font-family: var(--doaide-font-display, 'Instrument Serif', Georgia, serif);
          font-size: 22px;
          color: #fff !important;
          line-height: 1;
        }
        .landing-nav-title span {
          font-style: italic;
          color: #F0B429 !important;
        }
        .landing-nav-actions {
          display: flex;
          gap: 10px;
        }
        .landing-btn {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          padding: 9px 20px;
          border-radius: 8px;
          font-family: var(--doaide-font, 'Schibsted Grotesk', sans-serif);
          font-size: 14px;
          font-weight: 600;
          text-decoration: none !important;
          transition: all 180ms ease;
          cursor: pointer;
          border: none;
          white-space: nowrap;
        }
        .landing-btn--ghost {
          background: transparent;
          color: #9CA3AF !important;
          border: 1px solid rgba(255,255,255,0.1);
        }
        .landing-btn--ghost:hover {
          background: rgba(255,255,255,0.05);
          color: #E5E7EB !important;
          border-color: rgba(255,255,255,0.15);
        }
        .landing-btn--primary {
          background: #F0B429;
          color: #0A0A0B !important;
          box-shadow: 0 0 20px rgba(240,180,41,0.2);
        }
        .landing-btn--primary:hover {
          background: #F7D070;
          box-shadow: 0 0 30px rgba(240,180,41,0.3);
          transform: translateY(-1px);
        }
        .landing-btn--large {
          padding: 13px 32px;
          font-size: 15px;
          border-radius: 10px;
        }

        /* --- Hero --- */
        .landing-hero {
          position: relative;
          z-index: 1;
          display: flex;
          flex-direction: column;
          align-items: center;
          text-align: center;
          max-width: 720px;
          margin: 0 auto;
          padding: 60px 24px 40px;
        }
        .landing-hero-robot {
          width: 140px;
          height: 112px;
          margin-bottom: 28px;
          opacity: 0;
          animation: heroFloat 0.8s ease-out 0.4s forwards;
          filter: drop-shadow(0 0 30px rgba(240,180,41,0.25));
        }
        @keyframes heroFloat {
          from { opacity: 0; transform: translateY(20px); }
          to { opacity: 1; transform: translateY(0); }
        }
        .landing-hero-badge {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          padding: 5px 14px;
          border-radius: 20px;
          background: rgba(240,180,41,0.1);
          border: 1px solid rgba(240,180,41,0.2);
          font-family: var(--doaide-font-mono, 'IBM Plex Mono', monospace);
          font-size: 11px;
          font-weight: 500;
          letter-spacing: 0.08em;
          text-transform: uppercase;
          color: #F0B429;
          margin-bottom: 24px;
          opacity: 0;
          animation: fadeSlideUp 0.6s ease-out 0.6s forwards;
        }
        .landing-hero-badge-dot {
          width: 6px;
          height: 6px;
          border-radius: 50%;
          background: #F0B429;
          animation: pulse 2s ease-in-out infinite;
        }
        @keyframes pulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.4; }
        }
        .landing-hero h1 {
          font-family: var(--doaide-font-display, 'Instrument Serif', Georgia, serif);
          font-size: clamp(36px, 6vw, 56px);
          font-weight: 400;
          line-height: 1.1;
          color: #fff;
          margin: 0 0 8px;
          opacity: 0;
          animation: fadeSlideUp 0.7s ease-out 0.7s forwards;
        }
        .landing-hero h1 em {
          font-style: italic;
          color: #F0B429;
        }
        .landing-hero-sub {
          font-family: var(--doaide-font, 'Schibsted Grotesk', sans-serif);
          font-size: clamp(15px, 2vw, 18px);
          line-height: 1.6;
          color: #9CA3AF;
          margin: 0 0 36px;
          max-width: 540px;
          opacity: 0;
          animation: fadeSlideUp 0.7s ease-out 0.9s forwards;
        }
        @keyframes fadeSlideUp {
          from { opacity: 0; transform: translateY(16px); }
          to { opacity: 1; transform: translateY(0); }
        }
        .landing-hero-actions {
          display: flex;
          gap: 14px;
          flex-wrap: wrap;
          justify-content: center;
          opacity: 0;
          animation: fadeSlideUp 0.7s ease-out 1.1s forwards;
        }

        /* --- Features --- */
        .landing-features {
          position: relative;
          z-index: 1;
          max-width: 1000px;
          margin: 0 auto;
          padding: 40px 24px 80px;
        }
        .landing-features-label {
          text-align: center;
          font-family: var(--doaide-font-mono, 'IBM Plex Mono', monospace);
          font-size: 11px;
          font-weight: 500;
          letter-spacing: 0.15em;
          text-transform: uppercase;
          color: rgba(255,255,255,0.3);
          margin-bottom: 32px;
          opacity: 0;
          animation: fadeSlideUp 0.6s ease-out 1.3s forwards;
        }
        .landing-features-grid {
          display: grid;
          grid-template-columns: repeat(2, 1fr);
          gap: 16px;
        }
        @media (max-width: 640px) {
          .landing-features-grid {
            grid-template-columns: 1fr;
          }
        }
        .landing-feature-card {
          background: rgba(16,16,18,0.6);
          backdrop-filter: blur(8px);
          -webkit-backdrop-filter: blur(8px);
          border: 1px solid rgba(255,255,255,0.06);
          border-radius: 12px;
          padding: 24px;
          transition: all 250ms ease;
          opacity: 0;
          animation: featureIn 0.6s ease-out forwards;
        }
        .landing-feature-card:nth-child(1) { animation-delay: 1.4s; }
        .landing-feature-card:nth-child(2) { animation-delay: 1.5s; }
        .landing-feature-card:nth-child(3) { animation-delay: 1.6s; }
        .landing-feature-card:nth-child(4) { animation-delay: 1.7s; }
        @keyframes featureIn {
          from { opacity: 0; transform: translateY(20px); }
          to { opacity: 1; transform: translateY(0); }
        }
        .landing-feature-card:hover {
          border-color: rgba(240,180,41,0.15);
          background: rgba(20,20,22,0.8);
          box-shadow: 0 0 30px rgba(240,180,41,0.05);
          transform: translateY(-2px);
        }
        .landing-feature-icon {
          width: 28px;
          height: 28px;
          color: #F0B429;
          margin-bottom: 14px;
        }
        .landing-feature-card h3 {
          font-family: var(--doaide-font, 'Schibsted Grotesk', sans-serif);
          font-size: 15px;
          font-weight: 600;
          color: #fff;
          margin: 0 0 8px;
        }
        .landing-feature-card p {
          font-family: var(--doaide-font, 'Schibsted Grotesk', sans-serif);
          font-size: 13px;
          line-height: 1.55;
          color: #9CA3AF;
          margin: 0;
        }

        /* --- Footer --- */
        .landing-footer {
          position: relative;
          z-index: 1;
          text-align: center;
          padding: 0 24px 32px;
          opacity: 0;
          animation: fadeSlideUp 0.6s ease-out 1.8s forwards;
        }
        .landing-footer-text {
          font-family: var(--doaide-font, 'Schibsted Grotesk', sans-serif);
          font-size: 12px;
          color: rgba(255,255,255,0.25);
        }
        .landing-footer-text a {
          color: rgba(255,255,255,0.4) !important;
          text-decoration: none;
          font-weight: 500;
        }
        .landing-footer-text a:hover {
          color: rgba(255,255,255,0.6) !important;
          text-decoration: underline;
        }

        /* --- Divider --- */
        .landing-divider {
          position: relative;
          z-index: 1;
          max-width: 200px;
          margin: 0 auto 40px;
          height: 1px;
          background: linear-gradient(90deg, transparent, rgba(240,180,41,0.3), transparent);
          opacity: 0;
          animation: fadeSlideUp 0.5s ease-out 1.25s forwards;
        }
      `}</style>

      <div className={`landing-page${mounted ? '' : ''}`}>
        {/* Background effects */}
        <div className="landing-bg">
          <div className="landing-bg-orb landing-bg-orb--gold" />
          <div className="landing-bg-orb landing-bg-orb--teal" />
          <div className="landing-bg-orb landing-bg-orb--blue" />
        </div>
        <div className="landing-grid" />

        {/* Nav */}
        <nav className="landing-nav">
          <div className="landing-nav-brand">
            <RobotLogo className="landing-nav-robot" />
            <div className="landing-nav-title">
              DoAide <span>Med</span>
            </div>
          </div>
          <div className="landing-nav-actions">
            <Link href="/login" className="landing-btn landing-btn--ghost">
              Sign in
            </Link>
            <Link href="/signup" className="landing-btn landing-btn--primary">
              Get Started
            </Link>
          </div>
        </nav>

        {/* Hero */}
        <section className="landing-hero">
          <RobotLogo className="landing-hero-robot" />

          <div className="landing-hero-badge">
            <span className="landing-hero-badge-dot" />
            AI-Powered Clinical Decision Support
          </div>

          <h1>
            Your Digital Robot for<br />
            <em>Clinical Decisions</em>
          </h1>

          <p className="landing-hero-sub">
            DoAide Med helps clinicians with differential diagnosis, drug-safety checks,
            and guideline-cited management options &mdash; all grounded in evidence,
            never in guesswork.
          </p>

          <div className="landing-hero-actions">
            <Link href="/signup" className="landing-btn landing-btn--primary landing-btn--large">
              Create Clinician Account
            </Link>
            <Link href="/login" className="landing-btn landing-btn--ghost landing-btn--large">
              Sign In
            </Link>
          </div>
        </section>

        <div className="landing-divider" />

        {/* Features */}
        <section className="landing-features">
          <p className="landing-features-label">How it works</p>
          <div className="landing-features-grid">
            {FEATURES.map((f) => (
              <div key={f.title} className="landing-feature-card">
                {f.icon}
                <h3>{f.title}</h3>
                <p>{f.description}</p>
              </div>
            ))}
          </div>
        </section>

        {/* Footer */}
        <footer className="landing-footer">
          <p className="landing-footer-text">
            A{' '}
            <a href="https://doaide.com" target="_blank" rel="noopener noreferrer">
              DoAide
            </a>{' '}
            Product &middot; Built for clinicians in India
          </p>
        </footer>
      </div>
    </>
  );
}
