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

import TermsPage from './page';

describe('TermsPage', () => {
  it('renders the terms of service heading', () => {
    render(<TermsPage />);
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Terms of Service');
  });

  it('includes the clinical decision support disclaimer', () => {
    render(<TermsPage />);
    expect(screen.getByText('Clinical Decision Support Disclaimer')).toBeInTheDocument();
  });

  it('states the tool does not replace clinical judgment', () => {
    render(<TermsPage />);
    expect(screen.getByText(/does not provide medical diagnoses/)).toBeInTheDocument();
  });

  it('mentions governing law as India', () => {
    render(<TermsPage />);
    expect(screen.getByText(/laws of India/)).toBeInTheDocument();
  });

  it('includes nav and footer', () => {
    render(<TermsPage />);
    expect(screen.getByTestId('public-nav')).toBeInTheDocument();
    expect(screen.getByTestId('public-footer')).toBeInTheDocument();
  });
});
