'use client';

import { useState } from 'react';
import Link from 'next/link';
import { useRequireAuth, useAuth } from '@/lib/auth';
import { Button } from '@/components/ui';

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const { account, loading } = useRequireAuth();
  const { logout } = useAuth();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  if (loading || !account) {
    return (
      <main className="grid min-h-screen place-items-center">
        <div className="text-center">
          <div className="mx-auto mb-3 h-8 w-8 animate-spin rounded-full border-2 border-slate-300 border-t-blue-600" />
          <p className="text-sm text-slate-500">Loading…</p>
        </div>
      </main>
    );
  }

  const closeMobileMenu = () => setMobileMenuOpen(false);

  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-200 bg-white">
        <div className="flex items-center justify-between px-4 py-3 md:px-6">
          <Link href="/patients" className="text-lg font-bold text-slate-900">
            Aether Clinician
          </Link>

          {/* Desktop navigation */}
          <div className="hidden items-center gap-3 text-sm md:flex">
            <Link href="/patients" className="text-slate-600 hover:text-slate-900">
              Patients
            </Link>
            <Link href="/guidelines" className="text-slate-600 hover:text-slate-900">
              Guidelines
            </Link>
            <Link href="/metrics" className="text-slate-600 hover:text-slate-900">
              Metrics
            </Link>
            <span className="text-slate-500">{account.display_name ?? account.email}</span>
            <Button variant="secondary" onClick={() => logout()}>
              Sign out
            </Button>
          </div>

          {/* Mobile hamburger button */}
          <button
            type="button"
            className="inline-flex items-center justify-center rounded-md p-2 text-slate-600 hover:bg-slate-100 hover:text-slate-900 md:hidden"
            onClick={() => setMobileMenuOpen((prev) => !prev)}
            aria-expanded={mobileMenuOpen}
            aria-label={mobileMenuOpen ? 'Close menu' : 'Open menu'}
          >
            {mobileMenuOpen ? (
              <svg
                className="h-6 w-6"
                fill="none"
                viewBox="0 0 24 24"
                strokeWidth={1.5}
                stroke="currentColor"
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            ) : (
              <svg
                className="h-6 w-6"
                fill="none"
                viewBox="0 0 24 24"
                strokeWidth={1.5}
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

        {/* Mobile menu panel */}
        {mobileMenuOpen && (
          <div className="border-t border-slate-200 bg-white shadow-lg md:hidden">
            <nav className="flex flex-col gap-1 px-4 py-3">
              <Link
                href="/patients"
                className="rounded-md px-3 py-2 text-sm text-slate-600 hover:bg-slate-50 hover:text-slate-900"
                onClick={closeMobileMenu}
              >
                Patients
              </Link>
              <Link
                href="/guidelines"
                className="rounded-md px-3 py-2 text-sm text-slate-600 hover:bg-slate-50 hover:text-slate-900"
                onClick={closeMobileMenu}
              >
                Guidelines
              </Link>
              <Link
                href="/metrics"
                className="rounded-md px-3 py-2 text-sm text-slate-600 hover:bg-slate-50 hover:text-slate-900"
                onClick={closeMobileMenu}
              >
                Metrics
              </Link>
            </nav>
            <div className="border-t border-slate-100 px-4 py-3">
              <p className="mb-2 px-3 text-sm text-slate-500">
                {account.display_name ?? account.email}
              </p>
              <Button
                variant="secondary"
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
      <main className="mx-auto max-w-5xl px-4 py-4 md:px-6 md:py-6">{children}</main>
    </div>
  );
}
