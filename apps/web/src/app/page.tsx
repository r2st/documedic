'use client';

import { useEffect, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth } from '@/lib/auth';
import { api } from '@/lib/api';
import { requestErrorMessage } from '@/lib/errors';
import './landing.css';

const GOLD = '#F0B429';
const GOLD_LIGHT = '#F7CC5F';

const TYPEWRITER_PHRASES = [
  'Evidence-based guidance',
  'Drug interaction checks',
  'Smart diagnosis support',
  'AI clinical insights',
];

const DOAIDE_PRODUCTS = [
  { name: 'Desk', url: 'https://desk.doaide.com' },
  { name: 'Jobs', url: 'https://job.doaide.com' },
  { name: '409A', url: 'https://409a.doaide.com' },
  { name: 'GST', url: 'https://gst.doaide.com' },
  { name: 'Pulse', url: 'https://pulse.doaide.com' },
  { name: 'Med', url: 'https://med.doaide.com' },
  { name: 'Realty', url: 'https://realty.doaide.com' },
  { name: 'Reach', url: 'https://reach.doaide.com' },
  { name: 'Trade', url: 'https://trade.doaide.com' },
];

function RobotIcon({ size = 24 }: { size?: number }) {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width={size} height={size}>
      <line x1="16" y1="6" x2="16" y2="2" stroke={GOLD} strokeWidth="1.5" strokeLinecap="round" />
      <circle cx="16" cy="1.5" r="1.5" fill={GOLD} />
      <rect x="5" y="6" width="22" height="17" rx="5" fill={GOLD} />
      <ellipse cx="11" cy="13" rx="2.5" ry="3" fill="#0A0A0B" />
      <ellipse cx="21" cy="13" rx="2.5" ry="3" fill="#0A0A0B" />
      <circle cx="11.5" cy="12.5" r="1" fill={GOLD_LIGHT} opacity="0.6" />
      <circle cx="21.5" cy="12.5" r="1" fill={GOLD_LIGHT} opacity="0.6" />
      <path d="M12 19Q16 22 20 19" stroke="#0A0A0B" strokeWidth="1.2" fill="none" strokeLinecap="round" />
      <rect x="1" y="10" width="4" height="5" rx="2" fill={GOLD} opacity="0.8" />
      <rect x="27" y="10" width="4" height="5" rx="2" fill={GOLD} opacity="0.8" />
    </svg>
  );
}

function HeroRobot() {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 320" className="landing-hero-robot">
      <defs>
        <linearGradient id="hg" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={GOLD} />
          <stop offset="100%" stopColor="#D4A017" />
        </linearGradient>
      </defs>
      <line x1="200" y1="45" x2="200" y2="20" stroke={GOLD} strokeWidth="6" strokeLinecap="round" />
      <circle cx="200" cy="14" r="10" fill={GOLD} className="landing-antenna-glow" />
      <circle cx="200" cy="14" r="5" fill={GOLD_LIGHT} />
      <rect x="110" y="50" width="180" height="140" rx="35" fill="url(#hg)" />
      <rect x="130" y="68" width="140" height="105" rx="25" fill="#D4A017" opacity="0.4" />
      <ellipse cx="165" cy="115" rx="18" ry="20" fill="#0A0A0B" />
      <ellipse cx="235" cy="115" rx="18" ry="20" fill="#0A0A0B" />
      <circle cx="170" cy="113" r="8" fill={GOLD_LIGHT} />
      <circle cx="240" cy="113" r="8" fill={GOLD_LIGHT} />
      <circle cx="174" cy="109" r="3" fill="white" opacity="0.7" />
      <circle cx="244" cy="109" r="3" fill="white" opacity="0.7" />
      <path d="M170 155Q200 178 230 155" stroke="#0A0A0B" strokeWidth="4" fill="none" strokeLinecap="round" />
      <rect x="92" y="95" width="22" height="45" rx="8" fill="#D4A017" />
      <rect x="286" y="95" width="22" height="45" rx="8" fill="#D4A017" />
      <rect x="175" y="190" width="50" height="14" rx="5" fill="#D4A017" />
      <rect x="145" y="204" width="110" height="55" rx="18" fill="url(#hg)" />
      <circle cx="200" cy="228" r="7" fill="#0A0A0B" />
      <circle cx="200" cy="228" r="3.5" fill="#0A0A0B" />
      <path d="M145 218Q118 223 113 240Q108 257 120 262" stroke="#D4A017" strokeWidth="9" fill="none" strokeLinecap="round" />
      <circle cx="120" cy="265" r="7" fill="#D4A017" />
      <path d="M255 218Q282 223 287 240Q292 257 280 262" stroke="#D4A017" strokeWidth="9" fill="none" strokeLinecap="round" />
      <circle cx="280" cy="265" r="7" fill="#D4A017" />
    </svg>
  );
}

function useTypewriter(phrases: string[], typingMs = 80, pauseMs = 2000, deleteMs = 40) {
  const [text, setText] = useState('');
  const [phraseIdx, setPhraseIdx] = useState(0);

  useEffect(() => {
    const phrase = phrases[phraseIdx];
    let charIdx = 0;
    let deleting = false;
    let timer: ReturnType<typeof setTimeout>;

    function tick() {
      if (!deleting) {
        charIdx++;
        setText(phrase.slice(0, charIdx));
        if (charIdx === phrase.length) {
          timer = setTimeout(() => {
            deleting = true;
            tick();
          }, pauseMs);
          return;
        }
        timer = setTimeout(tick, typingMs);
      } else {
        charIdx--;
        setText(phrase.slice(0, charIdx));
        if (charIdx === 0) {
          setPhraseIdx((i) => (i + 1) % phrases.length);
          return;
        }
        timer = setTimeout(tick, deleteMs);
      }
    }

    tick();
    return () => clearTimeout(timer);
  }, [phraseIdx, phrases, typingMs, pauseMs, deleteMs]);

  return text;
}

function ParticleCanvas() {
  useEffect(() => {
    const canvas = document.getElementById('landing-particles') as HTMLCanvasElement | null;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let animId: number;
    const particles: { x: number; y: number; vx: number; vy: number; r: number; o: number }[] = [];
    const count = 40;

    function resize() {
      canvas!.width = window.innerWidth;
      canvas!.height = window.innerHeight;
    }
    resize();
    window.addEventListener('resize', resize);

    for (let i = 0; i < count; i++) {
      particles.push({
        x: Math.random() * canvas.width,
        y: Math.random() * canvas.height,
        vx: (Math.random() - 0.5) * 0.3,
        vy: (Math.random() - 0.5) * 0.3,
        r: Math.random() * 2 + 0.5,
        o: Math.random() * 0.4 + 0.1,
      });
    }

    function draw() {
      ctx!.clearRect(0, 0, canvas!.width, canvas!.height);
      for (const p of particles) {
        p.x += p.vx;
        p.y += p.vy;
        if (p.x < 0) p.x = canvas!.width;
        if (p.x > canvas!.width) p.x = 0;
        if (p.y < 0) p.y = canvas!.height;
        if (p.y > canvas!.height) p.y = 0;

        ctx!.beginPath();
        ctx!.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx!.fillStyle = `rgba(240, 180, 41, ${p.o})`;
        ctx!.fill();
      }
      animId = requestAnimationFrame(draw);
    }
    draw();

    return () => {
      cancelAnimationFrame(animId);
      window.removeEventListener('resize', resize);
    };
  }, []);

  return <canvas id="landing-particles" className="landing-particles" />;
}

function Spinner() {
  return (
    <svg aria-hidden="true" className="landing-spinner" viewBox="0 0 24 24" fill="none">
      <circle opacity="0.25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path opacity="0.75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  );
}

type AuthTab = 'signin' | 'signup';

function AuthPanel() {
  const router = useRouter();
  const { refresh } = useAuth();
  const [tab, setTab] = useState<AuthTab>('signin');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const resetForm = useCallback((newTab: AuthTab) => {
    setTab(newTab);
    setError(null);
    setEmail('');
    setPassword('');
    setDisplayName('');
  }, []);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (tab === 'signin') {
        await api.login(email, password);
      } else {
        await api.signup(email, password, displayName || undefined);
      }
      await refresh();
      router.replace('/patients');
    } catch (err) {
      setError(requestErrorMessage(err, tab === 'signin' ? 'your sign-in' : 'your sign-up'));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="landing-auth-card">
      <div className="landing-auth-tabs">
        <button
          type="button"
          className={`landing-auth-tab${tab === 'signin' ? ' landing-auth-tab-active' : ''}`}
          onClick={() => resetForm('signin')}
        >
          Sign in
        </button>
        <button
          type="button"
          className={`landing-auth-tab${tab === 'signup' ? ' landing-auth-tab-active' : ''}`}
          onClick={() => resetForm('signup')}
        >
          Create account
        </button>
      </div>

      <form onSubmit={onSubmit} className="landing-auth-form">
        {error && (
          <div role="alert" className="landing-auth-error">
            {error}
          </div>
        )}

        {tab === 'signup' && (
          <div className="landing-auth-field">
            <label htmlFor="landing-name" className="landing-auth-label">
              Display name <span className="landing-auth-hint">(optional)</span>
            </label>
            <input
              id="landing-name"
              placeholder="Dr. Jane Smith"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              className="landing-auth-input"
              autoComplete="name"
            />
          </div>
        )}

        <div className="landing-auth-field">
          <label htmlFor="landing-email" className="landing-auth-label">
            Email
          </label>
          <input
            id="landing-email"
            type="email"
            required
            placeholder="you@example.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="landing-auth-input"
            autoComplete="email"
          />
        </div>

        <div className="landing-auth-field">
          <label htmlFor="landing-password" className="landing-auth-label">
            Password
          </label>
          <input
            id="landing-password"
            type="password"
            required
            minLength={tab === 'signup' ? 8 : undefined}
            placeholder={tab === 'signup' ? 'Minimum 8 characters' : 'Enter your password'}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="landing-auth-input"
            autoComplete={tab === 'signup' ? 'new-password' : 'current-password'}
          />
        </div>

        <button type="submit" disabled={busy} className="landing-auth-button">
          {busy ? (
            <span className="landing-auth-busy">
              <Spinner />
              {tab === 'signin' ? 'Signing in…' : 'Creating account…'}
            </span>
          ) : tab === 'signin' ? (
            'Sign in'
          ) : (
            'Create account'
          )}
        </button>
      </form>
    </div>
  );
}

export default function LandingPage() {
  const { account, loading } = useAuth();
  const router = useRouter();
  const typedText = useTypewriter(TYPEWRITER_PHRASES);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    requestAnimationFrame(() => setVisible(true));
  }, []);

  useEffect(() => {
    if (!loading && account) {
      router.replace('/patients');
    }
  }, [account, loading, router]);

  if (loading || account) {
    return (
      <main className="grid min-h-screen place-items-center" style={{ background: '#0A0A0B' }}>
        <div className="text-center">
          <div
            className="mx-auto mb-3 h-8 w-8 animate-spin rounded-full border-2 border-t-[#F0B429]"
            style={{ borderColor: '#2A2A2D', borderTopColor: GOLD }}
          />
          <p style={{ color: '#9CA3AF', fontSize: 14 }}>Loading&hellip;</p>
        </div>
      </main>
    );
  }

  return (
    <div className={`landing-root${visible ? ' landing-visible' : ''}`}>
      <ParticleCanvas />

      {/* Header */}
      <header className="landing-header">
        <a href="https://doaide.com" className="landing-brand">
          <RobotIcon size={28} />
          <span className="landing-brand-text">
            Do<em>Aide</em>
          </span>
        </a>
      </header>

      {/* Main split layout */}
      <main className="landing-split">
        {/* Left panel — product info */}
        <div className="landing-left">
          <div className="landing-robot-wrap">
            <HeroRobot />
          </div>

          <h1 className="landing-headline">
            Clinical decisions, <em>supported.</em>
          </h1>

          <p className="landing-subtitle">
            AI-powered clinical decision support for evidence-based patient care.
          </p>

          <div className="landing-typewriter" aria-live="polite">
            <span className="landing-typewriter-text">{typedText}</span>
            <span className="landing-cursor" />
          </div>
        </div>

        {/* Right panel — auth form */}
        <div className="landing-right">
          <AuthPanel />
        </div>
      </main>

      {/* Footer */}
      <footer className="landing-footer">
        <div className="landing-footer-products">
          {DOAIDE_PRODUCTS.map((p) => (
            <a key={p.name} href={p.url} className="landing-footer-link">
              {p.name}
            </a>
          ))}
        </div>
        <div className="landing-footer-bottom">
          <a href="https://doaide.com" className="landing-footer-home">
            <RobotIcon size={14} />
            doaide.com
          </a>
          <span className="landing-footer-copy">&copy; 2026 DoAide</span>
        </div>
      </footer>
    </div>
  );
}
