import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('next/link', () => ({
  default: ({ children, href, ...props }: { children: React.ReactNode; href: string; [k: string]: unknown }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));

import Page from './page';

describe('Symptom Triage page', () => {
  it('renders triage inside public layout', () => {
    render(<Page />);
    expect(screen.getByText('Symptom Triage')).toBeInTheDocument();
  });

  it('includes JSON-LD schema', () => {
    render(<Page />);
    const scripts = document.querySelectorAll('script[type="application/ld+json"]');
    const data = JSON.parse(scripts[0].textContent ?? '{}');
    expect(data.name).toBe('Symptom Triage');
  });
});
