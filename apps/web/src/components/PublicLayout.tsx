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

export function PublicNav() {
  const [open, setOpen] = useState(false);

  return (
    <nav className="border-b border-[var(--doaide-border)] bg-[var(--doaide-bg)] sticky top-0 z-50">
      <div className="max-w-5xl mx-auto px-6 h-14 flex items-center justify-between">
        <Link href="/" className="flex items-center gap-2 font-semibold text-[var(--doaide-gold)] no-underline">
          DoAide Med
        </Link>

        {/* Desktop nav */}
        <div className="hidden md:flex items-center gap-6 text-sm">
          {NAV_LINKS.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className="text-[var(--doaide-text-secondary)] hover:text-[var(--doaide-gold)] no-underline transition-colors"
            >
              {link.label}
            </Link>
          ))}
          <Link
            href="/"
            className="px-4 py-1.5 rounded-md text-sm font-medium bg-[var(--doaide-gold)] text-[var(--doaide-text-on-gold)] hover:bg-[var(--doaide-gold-hover)] no-underline transition-colors"
          >
            Get Started
          </Link>
        </div>

        {/* Mobile hamburger */}
        <button
          type="button"
          className="md:hidden flex items-center justify-center w-10 h-10 rounded-lg text-[var(--doaide-text-secondary)] hover:text-[var(--doaide-gold)] transition-colors"
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
        <div className="md:hidden border-t border-[var(--doaide-border)] bg-[var(--doaide-bg)] px-6 py-4 space-y-1">
          {NAV_LINKS.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              onClick={() => setOpen(false)}
              className="block py-2.5 text-sm text-[var(--doaide-text-secondary)] hover:text-[var(--doaide-gold)] no-underline transition-colors"
            >
              {link.label}
            </Link>
          ))}
          <Link
            href="/"
            onClick={() => setOpen(false)}
            className="block mt-2 px-4 py-2.5 rounded-lg text-sm font-medium text-center bg-[var(--doaide-gold)] text-[var(--doaide-text-on-gold)] hover:bg-[var(--doaide-gold-hover)] no-underline transition-colors"
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
    <footer className="border-t border-[var(--doaide-border)] mt-16 py-8">
      <div className="max-w-5xl mx-auto px-6 flex flex-col sm:flex-row items-center justify-between gap-4 text-sm text-[var(--doaide-text-muted)]">
        <p>&copy; {new Date().getFullYear()} Apprend Technologies. All rights reserved.</p>
        <div className="flex items-center gap-4 sm:gap-6 flex-wrap justify-center">
          <Link href="/about" className="hover:text-[var(--doaide-gold)] no-underline transition-colors">About</Link>
          <Link href="/compare" className="hover:text-[var(--doaide-gold)] no-underline transition-colors">Compare</Link>
          <Link href="/tools" className="hover:text-[var(--doaide-gold)] no-underline transition-colors">Free Tools</Link>
          <Link href="/blog" className="hover:text-[var(--doaide-gold)] no-underline transition-colors">Blog</Link>
          <Link href="/privacy" className="hover:text-[var(--doaide-gold)] no-underline transition-colors">Privacy</Link>
          <Link href="/terms" className="hover:text-[var(--doaide-gold)] no-underline transition-colors">Terms</Link>
          <a href="https://doaide.com" className="hover:text-[var(--doaide-gold)] no-underline transition-colors">DoAide</a>
        </div>
      </div>
    </footer>
  );
}
