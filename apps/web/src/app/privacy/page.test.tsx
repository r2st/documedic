import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('next/link', () => ({
  default: ({ children, href, ...props }: { children: React.ReactNode; href: string; [k: string]: unknown }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));

vi.mock('@/components/PublicLayout', () => ({
  PublicNav: () => <nav data-testid="public-nav" />,
  PublicFooter: () => <footer data-testid="public-footer" />,
}));

import PrivacyPage from './page';

describe('PrivacyPage', () => {
  it('renders the privacy policy heading', () => {
    render(<PrivacyPage />);
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Privacy Policy');
  });

  it('mentions DPDP Act compliance', () => {
    render(<PrivacyPage />);
    expect(screen.getByText(/Digital Personal Data Protection/)).toBeInTheDocument();
  });

  it('has a contact email link', () => {
    render(<PrivacyPage />);
    const emailLinks = screen.getAllByText('privacy@doaide.com');
    expect(emailLinks.length).toBeGreaterThan(0);
  });

  it('renders all required sections', () => {
    render(<PrivacyPage />);
    expect(screen.getByText('Data We Collect')).toBeInTheDocument();
    expect(screen.getByText('Data Protection')).toBeInTheDocument();
    expect(screen.getByText('Your Rights')).toBeInTheDocument();
    expect(screen.getByText('Analytics')).toBeInTheDocument();
  });

  it('includes nav and footer', () => {
    render(<PrivacyPage />);
    expect(screen.getByTestId('public-nav')).toBeInTheDocument();
    expect(screen.getByTestId('public-footer')).toBeInTheDocument();
  });
});
