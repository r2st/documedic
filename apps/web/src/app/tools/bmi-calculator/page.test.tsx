import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('next/link', () => ({
  default: ({ children, href, ...props }: { children: React.ReactNode; href: string; [k: string]: unknown }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));

import Page from './page';

describe('BMI Calculator page', () => {
  it('renders the calculator inside the public layout', () => {
    render(<Page />);
    expect(screen.getByText('BMI Calculator')).toBeInTheDocument();
    expect(screen.getByText('DoAide Med')).toBeInTheDocument();
  });

  it('includes JSON-LD schema', () => {
    render(<Page />);
    const scripts = document.querySelectorAll('script[type="application/ld+json"]');
    expect(scripts.length).toBeGreaterThan(0);
    const data = JSON.parse(scripts[0].textContent ?? '{}');
    expect(data['@type']).toBe('WebApplication');
    expect(data.name).toBe('BMI Calculator');
  });
});
