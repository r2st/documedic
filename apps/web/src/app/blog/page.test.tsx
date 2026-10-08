import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('next/link', () => ({
  default: ({ children, href, ...props }: { children: React.ReactNode; href: string; [k: string]: unknown }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));

import BlogIndex from './page';
import { POSTS } from './data';

describe('BlogIndex', () => {
  it('renders all blog posts', () => {
    render(<BlogIndex />);
    for (const post of POSTS) {
      expect(screen.getByText(post.title)).toBeInTheDocument();
    }
  });

  it('links to correct blog paths', () => {
    render(<BlogIndex />);
    for (const post of POSTS) {
      expect(screen.getByText(post.title).closest('a')?.getAttribute('href')).toBe(`/blog/${post.slug}`);
    }
  });
});
