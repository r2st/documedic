'use client';

import { useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useRequireAuth, useAuth } from '@/lib/auth';
import { Button } from '@/components/ui';

function NavLink({ href, children }: { href: string; children: React.ReactNode }) {
  const pathname = usePathname();
  const isActive = pathname === href || pathname?.startsWith(href + '/');

  return (
    <Link
      href={href}
      className={`relative rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
        isActive
          ? 'text-brand-700 bg-brand-50'
          : 'text-slate-600 hover:text-slate-900 hover:bg-slate-50'
      }`}
    >
      {children}
    </Link>
  );
}

function MobileNavLink({ href, children, onClick }: { href: string; children: React.ReactNode; onClick: () => void }) {
  const pathname = usePathname();
  const isActive = pathname === href || pathname?.startsWith(href + '/');

  return (
    <Link
      href={href}
      className={`flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors ${
        isActive
          ? 'text-brand-700 bg-brand-50'
          : 'text-slate-600 hover:bg-slate-50 hover:text-slate-900'
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

  if (loading || !account) {
    return (
      <main className="grid min-h-screen place-items-center">
        <div className="text-center">
          <div className="mx-auto mb-4 h-10 w-10 animate-spin rounded-full border-[3px] border-slate-200 border-t-brand-600" />
          <p className="text-sm font-medium text-slate-500">Loading Aether Clinician…</p>
        </div>
      </main>
    );
  }

  const closeMobileMenu = () => setMobileMenuOpen(false);

  return (
    <div className="min-h-screen bg-slate-50">
      <header className="sticky top-0 z-30 border-b border-slate-200/80 bg-white/95 backdrop-blur-sm shadow-nav">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-4 py-3 md:px-6">
          {/* Logo + Brand */}
          <Link href="/patients" className="flex items-center gap-2.5 group">
            {/* Static SVG logo — next/image does not optimize SVG, so <img> is correct here. */}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src="/logo.svg"
              alt="Aether Clinician"
              className="h-8 w-8 transition-transform group-hover:scale-105"
            />
            <span className="text-lg font-bold tracking-tight text-slate-900">
              Aether <span className="text-brand-600">Clinician</span>
            </span>
          </Link>

          {/* Desktop navigation */}
          <div className="hidden items-center gap-1 md:flex">
            <nav className="flex items-center gap-1 mr-4">
              <NavLink href="/patients">Patients</NavLink>
              <NavLink href="/guidelines">Guidelines</NavLink>
              <NavLink href="/metrics">Metrics</NavLink>
            </nav>
            <div className="flex items-center gap-3 border-l border-slate-200 pl-4">
              <div className="flex items-center gap-2">
                <div className="flex h-8 w-8 items-center justify-center rounded-full bg-brand-100 text-sm font-semibold text-brand-700">
                  {(account.display_name ?? account.email)?.[0]?.toUpperCase() ?? '?'}
                </div>
                <span className="text-sm font-medium text-slate-700 max-w-[140px] truncate">
                  {account.display_name ?? account.email}
                </span>
              </div>
              <Button variant="ghost" size="sm" onClick={() => logout()}>
                Sign out
              </Button>
            </div>
          </div>

          {/* Mobile hamburger button */}
          <button
            type="button"
            className="inline-flex items-center justify-center rounded-lg p-2 text-slate-500 hover:bg-slate-100 hover:text-slate-700 md:hidden"
            onClick={() => setMobileMenuOpen((prev) => !prev)}
            aria-expanded={mobileMenuOpen}
            aria-label={mobileMenuOpen ? 'Close menu' : 'Open menu'}
          >
            {mobileMenuOpen ? (
              <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            ) : (
              <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5M3.75 17.25h16.5" />
              </svg>
            )}
          </button>
        </div>

        {/* Mobile menu panel */}
        {mobileMenuOpen && (
          <div className="animate-slide-down border-t border-slate-100 bg-white shadow-elevated md:hidden">
            <nav className="flex flex-col gap-1 px-4 py-3">
              <MobileNavLink href="/patients" onClick={closeMobileMenu}>Patients</MobileNavLink>
              <MobileNavLink href="/guidelines" onClick={closeMobileMenu}>Guidelines</MobileNavLink>
              <MobileNavLink href="/metrics" onClick={closeMobileMenu}>Metrics</MobileNavLink>
            </nav>
            <div className="border-t border-slate-100 px-4 py-3">
              <div className="mb-3 flex items-center gap-2 px-3">
                <div className="flex h-8 w-8 items-center justify-center rounded-full bg-brand-100 text-sm font-semibold text-brand-700">
                  {(account.display_name ?? account.email)?.[0]?.toUpperCase() ?? '?'}
                </div>
                <span className="text-sm font-medium text-slate-700">
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
      <main className="mx-auto max-w-6xl px-4 py-6 md:px-6 md:py-8 animate-fade-in">{children}</main>
    </div>
  );
}
