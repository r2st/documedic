import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('next/link', () => ({
  default: ({ children, href, ...props }: { children: React.ReactNode; href: string; [k: string]: unknown }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));

import { PublicNav, PublicFooter } from './PublicLayout';

describe('PublicNav', () => {
  it('renders brand and navigation links', () => {
    render(<PublicNav />);
    expect(screen.getByText('DoAide Med')).toBeInTheDocument();
    expect(screen.getByText('Free Tools')).toBeInTheDocument();
    expect(screen.getByText('Blog')).toBeInTheDocument();
    expect(screen.getByText('Embed')).toBeInTheDocument();
    expect(screen.getByText('Get Started')).toBeInTheDocument();
  });

  it('links point to correct paths', () => {
    render(<PublicNav />);
    expect(screen.getByText('Free Tools').closest('a')?.getAttribute('href')).toBe('/tools');
    expect(screen.getByText('Blog').closest('a')?.getAttribute('href')).toBe('/blog');
    expect(screen.getByText('Embed').closest('a')?.getAttribute('href')).toBe('/embed');
  });
});

describe('PublicFooter', () => {
  it('renders copyright and links', () => {
    render(<PublicFooter />);
    expect(screen.getByText(/Apprend Technologies/)).toBeInTheDocument();
    expect(screen.getByText('Free Tools')).toBeInTheDocument();
    expect(screen.getByText('Blog')).toBeInTheDocument();
    expect(screen.getByText('DoAide')).toBeInTheDocument();
  });
});
