import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('next/link', () => ({
  default: ({ children, href, ...props }: { children: React.ReactNode; href: string; [k: string]: unknown }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));

import Page from './page';

describe('Embed page', () => {
  it('renders the embed generator', () => {
    render(<Page />);
    expect(screen.getByText('Embed Widget Generator')).toBeInTheDocument();
  });
});
