import { ErrorBoundary } from '@/components/ErrorBoundary';
import './auth.css';

function RobotLogo() {
  return (
    <svg
      viewBox="0 0 48 48"
      style={{ width: 48, height: 48, color: '#F0B429', margin: '0 auto 20px' }}
      aria-hidden="true"
    >
      <g fill="none">
        <line
          x1="24"
          y1="8"
          x2="24"
          y2="3"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
        />
        <circle cx="24" cy="2" r="1.8" fill="currentColor" opacity="0.9" />
        <circle cx="24" cy="2" r="2.8" fill="currentColor" opacity="0.25" />
        <rect x="14" y="8" width="20" height="14" rx="4" fill="currentColor" />
        <circle cx="19.5" cy="14" r="2.2" fill="#0A0A0B" />
        <circle cx="28.5" cy="14" r="2.2" fill="#0A0A0B" />
        <path
          d="M20 18.5 Q24 21.5 28 18.5"
          stroke="#0A0A0B"
          strokeWidth="1.4"
          fill="none"
          strokeLinecap="round"
        />
        <rect x="16" y="23" width="16" height="12" rx="3" fill="currentColor" />
        <rect x="8" y="24" width="7" height="3.5" rx="1.8" fill="currentColor" />
        <rect x="33" y="24" width="7" height="3.5" rx="1.8" fill="currentColor" />
        <rect x="19" y="36" width="3.5" height="5" rx="1.5" fill="currentColor" />
        <rect x="25.5" y="36" width="3.5" height="5" rx="1.5" fill="currentColor" />
        <g transform="translate(36, 28)">
          <rect
            x="-2.5"
            y="0"
            width="7"
            height="5.5"
            rx="1"
            fill="#0A0A0B"
            stroke="currentColor"
            strokeWidth="0.8"
          />
          <path
            d="M-0.5 0 v-1.2 a1.2 1.2 0 0 1 1.2-1.2 h0.6 a1.2 1.2 0 0 1 1.2 1.2 v1.2"
            stroke="currentColor"
            strokeWidth="0.7"
            fill="none"
          />
          <rect x="0" y="2" width="2" height="1" rx="0.3" fill="currentColor" />
        </g>
      </g>
    </svg>
  );
}

export default function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <main className="auth-shell">
      <div className="auth-container">
        <RobotLogo />
        <h1 className="auth-title">
          DoAide <span className="auth-title-product">Clinician</span>
        </h1>
        <p className="auth-subtitle">AI clinical decision support for modern healthcare</p>

        <ErrorBoundary section="The sign-in form">{children}</ErrorBoundary>

        <p className="auth-footer">
          A{' '}
          <a
            href="https://doaide.com"
            className="auth-footer-link"
            target="_blank"
            rel="noopener noreferrer"
          >
            DoAide
          </a>{' '}
          Product
        </p>
      </div>
    </main>
  );
}
