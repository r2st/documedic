'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useRequireAuth, useAuth } from '@/lib/auth';
import { SessionExpiryBanner } from '@/components/Banners';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { Button } from '@aether/ui';

function NavLink({ href, children }: { href: string; children: React.ReactNode }) {
  const pathname = usePathname();
  const isActive = pathname === href || pathname?.startsWith(href + '/');

  return (
    <Link
      href={href}
      aria-current={isActive ? 'page' : undefined}
      className={`relative rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
        isActive
          ? 'text-[#F0B429] bg-[#F0B429]/10'
          : 'text-[#9CA3AF] hover:text-[#E5E7EB] hover:bg-[#1A1A1D]'
      }`}
    >
      {children}
    </Link>
  );
}

function MobileNavLink({
  href,
  children,
  onClick,
}: {
  href: string;
  children: React.ReactNode;
  onClick: () => void;
}) {
  const pathname = usePathname();
  const isActive = pathname === href || pathname?.startsWith(href + '/');

  return (
    <Link
      href={href}
      aria-current={isActive ? 'page' : undefined}
      className={`flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors ${
        isActive
          ? 'text-[#F0B429] bg-[#F0B429]/10'
          : 'text-[#9CA3AF] hover:bg-[#1A1A1D] hover:text-[#E5E7EB]'
      }`}
      onClick={onClick}
    >
      {children}
    </Link>
  );
}

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const { account, loading } = useRequireAuth();
  const { logout } = useAuth();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const pathname = usePathname();

  useEffect(() => {
    if (!mobileMenuOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMobileMenuOpen(false);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [mobileMenuOpen]);

  if (loading || !account) {
    return (
      <main className="grid min-h-screen place-items-center">
        <div className="text-center" role="status">
          <div className="mx-auto mb-4 h-10 w-10 animate-spin rounded-full border-[3px] border-[#2A2A2D] border-t-[#F0B429]" />
          <p className="text-sm font-medium text-[#9CA3AF]">Loading DoAide Clinician…</p>
        </div>
      </main>
    );
  }

  const closeMobileMenu = () => setMobileMenuOpen(false);

  return (
    <div className="min-h-screen bg-[#0A0A0B]">
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-lg focus:bg-[#F0B429] focus:px-4 focus:py-2 focus:text-sm focus:font-medium focus:text-[#0A0A0B]"
      >
        Skip to main content
      </a>
      <header className="sticky top-0 z-30 border-b border-[#2A2A2D] bg-[#0A0A0B]/95 backdrop-blur-sm shadow-nav">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-4 py-3 md:px-6">
          <Link href="/patients" className="flex items-center gap-2.5 group">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src="/logo.svg"
              alt="DoAide Clinician"
              className="h-8 w-8 transition-transform group-hover:scale-105"
            />
            <span className="text-lg font-bold tracking-tight text-[#E5E7EB]">
              DoAide <span className="text-[#F0B429]">Clinician</span>
            </span>
          </Link>

          <div className="hidden items-center gap-1 md:flex">
            <nav className="flex items-center gap-1 mr-4">
              <NavLink href="/patients">Patients</NavLink>
              <NavLink href="/guidelines">Guidelines</NavLink>
              <NavLink href="/metrics">Metrics</NavLink>
            </nav>
            <div className="flex items-center gap-3 border-l border-[#2A2A2D] pl-4">
              <div className="flex items-center gap-2">
                <div className="flex h-8 w-8 items-center justify-center rounded-full bg-[#F0B429]/15 text-sm font-semibold text-[#F0B429]">
                  {(account.display_name ?? account.email)?.[0]?.toUpperCase() ?? '?'}
                </div>
                <span className="text-sm font-medium text-[#E5E7EB] max-w-[140px] truncate">
                  {account.display_name ?? account.email}
                </span>
              </div>
              <Button variant="ghost" size="sm" onClick={() => logout()}>
                Sign out
              </Button>
            </div>
          </div>

          <button
            type="button"
            className="inline-flex items-center justify-center rounded-lg p-2 text-[#9CA3AF] hover:bg-[#1A1A1D] hover:text-[#E5E7EB] md:hidden"
            onClick={() => setMobileMenuOpen((prev) => !prev)}
            aria-expanded={mobileMenuOpen}
            aria-controls="mobile-menu"
            aria-label={mobileMenuOpen ? 'Close menu' : 'Open menu'}
          >
            {mobileMenuOpen ? (
              <svg
                aria-hidden="true"
                className="h-5 w-5"
                fill="none"
                viewBox="0 0 24 24"
                strokeWidth={2}
                stroke="currentColor"
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            ) : (
              <svg
                aria-hidden="true"
                className="h-5 w-5"
                fill="none"
                viewBox="0 0 24 24"
                strokeWidth={2}
                stroke="currentColor"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M3.75 6.75h16.5M3.75 12h16.5M3.75 17.25h16.5"
                />
              </svg>
            )}
          </button>
        </div>

        {mobileMenuOpen && (
          <div
            id="mobile-menu"
            className="animate-slide-down border-t border-[#2A2A2D] bg-[#1A1A1D] shadow-elevated md:hidden"
          >
            <nav className="flex flex-col gap-1 px-4 py-3">
              <MobileNavLink href="/patients" onClick={closeMobileMenu}>
                Patients
              </MobileNavLink>
              <MobileNavLink href="/guidelines" onClick={closeMobileMenu}>
                Guidelines
              </MobileNavLink>
              <MobileNavLink href="/metrics" onClick={closeMobileMenu}>
                Metrics
              </MobileNavLink>
            </nav>
            <div className="border-t border-[#2A2A2D] px-4 py-3">
              <div className="mb-3 flex items-center gap-2 px-3">
                <div className="flex h-8 w-8 items-center justify-center rounded-full bg-[#F0B429]/15 text-sm font-semibold text-[#F0B429]">
                  {(account.display_name ?? account.email)?.[0]?.toUpperCase() ?? '?'}
                </div>
                <span className="text-sm font-medium text-[#E5E7EB]">
                  {account.display_name ?? account.email}
                </span>
              </div>
              <Button
                variant="secondary"
                size="sm"
                className="w-full"
                onClick={() => {
                  closeMobileMenu();
                  logout();
                }}
              >
                Sign out
              </Button>
            </div>
          </div>
        )}
      </header>
      <SessionExpiryBanner />
      <main
        id="main-content"
        tabIndex={-1}
        className="mx-auto max-w-6xl px-4 py-6 md:px-6 md:py-8 animate-fade-in focus:outline-none"
      >
        <ErrorBoundary key={pathname} section="This screen">
          {children}
        </ErrorBoundary>
      </main>
      <footer className="border-t border-[#2A2A2D] bg-[#0A0A0B]/80 py-3 text-center text-xs text-[#6B7280]">
        A{' '}
        <a
          href="https://doaide.com"
          className="font-medium text-[#9CA3AF] hover:text-[#E5E7EB] hover:underline"
          target="_blank"
          rel="noopener noreferrer"
        >
          DoAide
        </a>{' '}
        Product
      </footer>
    </div>
  );
}
