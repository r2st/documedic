'use client';

import { useState } from 'react';
import Link from 'next/link';

const NAV_LINKS = [
  { href: '/about', label: 'About' },
  { href: '/compare', label: 'Compare' },
  { href: '/tools', label: 'Free Tools' },
  { href: '/blog', label: 'Blog' },
  { href: '/embed', label: 'Embed' },
];

const DOAIDE_PRODUCTS = [
  { name: 'GST', url: 'https://gst.doaide.com' },
  { name: '409A', url: 'https://409a.doaide.com' },
  { name: 'Pulse', url: 'https://pulse.doaide.com' },
  { name: 'Med', url: 'https://med.doaide.com' },
  { name: 'Jobs', url: 'https://job.doaide.com' },
  { name: 'Desk', url: 'https://desk.doaide.com' },
  { name: 'Realty', url: 'https://realty.doaide.com' },
];

export function PublicNav() {
  const [open, setOpen] = useState(false);

  return (
    <nav className="border-b border-[var(--da-border)] bg-[var(--da-bg)] sticky top-0 z-50">
      <div className="max-w-5xl mx-auto px-6 h-14 flex items-center justify-between">
        <Link href="/" className="flex items-center gap-2 text-base font-semibold text-[var(--da-text)] no-underline">
          DoAide <span className="text-[var(--da-text-muted)]">|</span> <span className="text-[var(--da-primary)]">Med</span>
        </Link>

        {/* Desktop nav */}
        <div className="hidden md:flex items-center gap-6 text-sm">
          {NAV_LINKS.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className="text-[var(--da-text-secondary)] hover:text-[var(--da-primary)] no-underline transition-colors"
            >
              {link.label}
            </Link>
          ))}
          <Link
            href="/"
            className="px-4 py-1.5 rounded-md text-sm font-medium bg-[var(--da-primary)] text-[var(--da-text-on-primary)] hover:bg-[var(--da-primary-hover)] no-underline transition-colors"
          >
            Get Started
          </Link>
        </div>

        {/* Mobile hamburger */}
        <button
          type="button"
          className="md:hidden flex items-center justify-center w-10 h-10 rounded-lg text-[var(--da-text-secondary)] hover:text-[var(--da-primary)] transition-colors"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          aria-label="Toggle navigation menu"
        >
          {open ? (
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          ) : (
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <line x1="3" y1="6" x2="21" y2="6" />
              <line x1="3" y1="12" x2="21" y2="12" />
              <line x1="3" y1="18" x2="21" y2="18" />
            </svg>
          )}
        </button>
      </div>

      {/* Mobile menu */}
      {open && (
        <div className="md:hidden border-t border-[var(--da-border)] bg-[var(--da-bg)] px-6 py-4 space-y-1">
          {NAV_LINKS.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              onClick={() => setOpen(false)}
              className="block py-2.5 text-sm text-[var(--da-text-secondary)] hover:text-[var(--da-primary)] no-underline transition-colors"
            >
              {link.label}
            </Link>
          ))}
          <Link
            href="/"
            onClick={() => setOpen(false)}
            className="block mt-2 px-4 py-2.5 rounded-lg text-sm font-medium text-center bg-[var(--da-primary)] text-[var(--da-text-on-primary)] hover:bg-[var(--da-primary-hover)] no-underline transition-colors"
          >
            Get Started
          </Link>
        </div>
      )}
    </nav>
  );
}

export function PublicFooter() {
  return (
    <footer className="border-t border-[var(--da-border)] mt-16 py-8">
      <div className="max-w-5xl mx-auto px-6">
        {/* Cross-product links */}
        <div className="flex flex-wrap justify-center gap-4 sm:gap-6 mb-6">
          {DOAIDE_PRODUCTS.map((p) => (
            <a
              key={p.name}
              href={p.url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-[var(--da-text-muted)] hover:text-[var(--da-primary)] no-underline transition-colors"
            >
              {p.name}
            </a>
          ))}
        </div>
        <div className="flex flex-col sm:flex-row items-center justify-between gap-4 text-sm text-[var(--da-text-muted)]">
          <p>&copy; {new Date().getFullYear()} Apprend Technologies. All rights reserved.</p>
          <div className="flex items-center gap-4 sm:gap-6 flex-wrap justify-center">
            <Link href="/about" className="hover:text-[var(--da-primary)] no-underline transition-colors">About</Link>
            <Link href="/compare" className="hover:text-[var(--da-primary)] no-underline transition-colors">Compare</Link>
            <Link href="/tools" className="hover:text-[var(--da-primary)] no-underline transition-colors">Free Tools</Link>
            <Link href="/blog" className="hover:text-[var(--da-primary)] no-underline transition-colors">Blog</Link>
            <Link href="/privacy" className="hover:text-[var(--da-primary)] no-underline transition-colors">Privacy</Link>
            <Link href="/terms" className="hover:text-[var(--da-primary)] no-underline transition-colors">Terms</Link>
            <a href="https://doaide.com" className="hover:text-[var(--da-primary)] no-underline transition-colors">DoAide</a>
          </div>
        </div>
      </div>
    </footer>
  );
}
