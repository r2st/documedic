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

function Pipeline() {
  return (
    <div className="landing-pipeline" aria-hidden="true">
      <svg viewBox="0 0 520 72" xmlns="http://www.w3.org/2000/svg" className="landing-pipeline-svg">
        <defs>
          <filter id="pip-glow">
            <feGaussianBlur stdDeviation="3" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
          <linearGradient id="pip-line-grad" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%" stopColor={GOLD} stopOpacity="0.25" />
            <stop offset="50%" stopColor={GOLD} stopOpacity="0.5" />
            <stop offset="100%" stopColor={GOLD} stopOpacity="0.25" />
          </linearGradient>
        </defs>

        {/* Connecting lines */}
        <line x1="96" y1="36" x2="148" y2="36" stroke="url(#pip-line-grad)" strokeWidth="1.5" />
        <line x1="228" y1="36" x2="280" y2="36" stroke="url(#pip-line-grad)" strokeWidth="1.5" />
        <line x1="360" y1="36" x2="412" y2="36" stroke="url(#pip-line-grad)" strokeWidth="1.5" />

        {/* Flowing dots — 3 per connector, staggered */}
        {[0, 1, 2].map((seg) => (
          <g key={seg}>
            <circle r="2.5" fill={GOLD} filter="url(#pip-glow)" opacity="0.9">
              <animateMotion
                dur="2.4s"
                repeatCount="indefinite"
                begin={`${seg * 0.5}s`}
                path={`M${96 + seg * 132},36 L${148 + seg * 132},36`}
              />
              <animate attributeName="opacity" values="0;0.9;0.9;0" dur="2.4s" repeatCount="indefinite" begin={`${seg * 0.5}s`} />
            </circle>
            <circle r="2.5" fill={GOLD} filter="url(#pip-glow)" opacity="0.9">
              <animateMotion
                dur="2.4s"
                repeatCount="indefinite"
                begin={`${seg * 0.5 + 0.8}s`}
                path={`M${96 + seg * 132},36 L${148 + seg * 132},36`}
              />
              <animate attributeName="opacity" values="0;0.9;0.9;0" dur="2.4s" repeatCount="indefinite" begin={`${seg * 0.5 + 0.8}s`} />
            </circle>
            <circle r="2.5" fill={GOLD} filter="url(#pip-glow)" opacity="0.9">
              <animateMotion
                dur="2.4s"
                repeatCount="indefinite"
                begin={`${seg * 0.5 + 1.6}s`}
                path={`M${96 + seg * 132},36 L${148 + seg * 132},36`}
              />
              <animate attributeName="opacity" values="0;0.9;0.9;0" dur="2.4s" repeatCount="indefinite" begin={`${seg * 0.5 + 1.6}s`} />
            </circle>
          </g>
        ))}

        {/* Stage 1: Symptoms (stethoscope) */}
        <g className="landing-pipeline-node">
          <rect x="8" y="4" width="80" height="64" rx="14" fill="rgba(16,16,18,0.8)" stroke="rgba(240,180,41,0.2)" strokeWidth="1" />
          <circle cx="48" cy="26" r="8" stroke={GOLD} strokeWidth="1.5" fill="none" opacity="0.8" />
          <path d="M43 32 L43 38 Q43 42 47 42 L49 42 Q53 42 53 38 L53 32" stroke={GOLD} strokeWidth="1.5" fill="none" strokeLinecap="round" opacity="0.8" />
          <circle cx="48" cy="26" r="2" fill={GOLD} opacity="0.5" />
          <text x="48" y="58" textAnchor="middle" fill={GOLD} fontSize="8" fontFamily="'IBM Plex Mono', monospace" fontWeight="500" opacity="0.8">Symptoms</text>
        </g>

        {/* Stage 2: Analyze (brain/AI) */}
        <g className="landing-pipeline-node">
          <rect x="148" y="4" width="80" height="64" rx="14" fill="rgba(16,16,18,0.8)" stroke="rgba(240,180,41,0.2)" strokeWidth="1" />
          <ellipse cx="188" cy="25" rx="9" ry="10" stroke={GOLD} strokeWidth="1.5" fill="none" opacity="0.8" />
          <path d="M183 20 Q188 15 193 20" stroke={GOLD} strokeWidth="1" fill="none" strokeLinecap="round" opacity="0.6" />
          <path d="M181 25 Q188 30 195 25" stroke={GOLD} strokeWidth="1" fill="none" strokeLinecap="round" opacity="0.6" />
          <line x1="188" y1="15" x2="188" y2="35" stroke={GOLD} strokeWidth="0.8" opacity="0.4" />
          <circle cx="185" cy="23" r="1.5" fill={GOLD} opacity="0.5" />
          <circle cx="191" cy="27" r="1.5" fill={GOLD} opacity="0.5" />
          <text x="188" y="58" textAnchor="middle" fill={GOLD} fontSize="8" fontFamily="'IBM Plex Mono', monospace" fontWeight="500" opacity="0.8">Analyze</text>
        </g>

        {/* Stage 3: Evidence (book/database) */}
        <g className="landing-pipeline-node">
          <rect x="280" y="4" width="80" height="64" rx="14" fill="rgba(16,16,18,0.8)" stroke="rgba(240,180,41,0.2)" strokeWidth="1" />
          <rect x="311" y="17" width="18" height="22" rx="2" stroke={GOLD} strokeWidth="1.5" fill="none" opacity="0.8" />
          <line x1="320" y1="17" x2="320" y2="39" stroke={GOLD} strokeWidth="1" opacity="0.4" />
          <line x1="314" y1="23" x2="326" y2="23" stroke={GOLD} strokeWidth="0.8" opacity="0.4" />
          <line x1="314" y1="28" x2="326" y2="28" stroke={GOLD} strokeWidth="0.8" opacity="0.4" />
          <line x1="314" y1="33" x2="326" y2="33" stroke={GOLD} strokeWidth="0.8" opacity="0.4" />
          <text x="320" y="58" textAnchor="middle" fill={GOLD} fontSize="8" fontFamily="'IBM Plex Mono', monospace" fontWeight="500" opacity="0.8">Evidence</text>
        </g>

        {/* Stage 4: Recommend (clipboard + check) */}
        <g className="landing-pipeline-node">
          <rect x="412" y="4" width="80" height="64" rx="14" fill="rgba(16,16,18,0.8)" stroke="rgba(240,180,41,0.2)" strokeWidth="1" />
          <rect x="443" y="19" width="16" height="20" rx="2" stroke={GOLD} strokeWidth="1.5" fill="none" opacity="0.8" />
          <rect x="447" y="16" width="8" height="5" rx="1.5" stroke={GOLD} strokeWidth="1" fill="rgba(16,16,18,0.8)" opacity="0.8" />
          <polyline points="447,29 450,32 455,26" stroke={GOLD} strokeWidth="1.5" fill="none" strokeLinecap="round" strokeLinejoin="round" opacity="0.8" />
          <text x="452" y="58" textAnchor="middle" fill={GOLD} fontSize="7.5" fontFamily="'IBM Plex Mono', monospace" fontWeight="500" opacity="0.8">Recommend</text>
        </g>

        {/* Subtle glow pulse on each node */}
        {[48, 188, 320, 452].map((cx, i) => (
          <circle key={cx} cx={cx} cy="36" r="28" fill="none" stroke={GOLD} strokeWidth="0.5" opacity="0">
            <animate attributeName="opacity" values="0;0.15;0" dur="3s" repeatCount="indefinite" begin={`${i * 0.6}s`} />
            <animate attributeName="r" values="28;34;28" dur="3s" repeatCount="indefinite" begin={`${i * 0.6}s`} />
          </circle>
        ))}
      </svg>
    </div>
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

          <Pipeline />

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
