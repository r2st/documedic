import { ErrorBoundary } from '@/components/ErrorBoundary';
import './auth.css';

export default function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <main className="auth-shell">
      <div className="auth-container">
        <h1 className="auth-title">
          DoAide <span className="auth-title-sep">|</span> <span className="auth-title-product">Med</span>
        </h1>
        <p className="auth-subtitle">AI-powered clinical decision support</p>

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
