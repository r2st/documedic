import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { BLOG_POSTS, generateStaticParams, generateMetadata } from './page';

vi.mock('next/navigation', () => ({
  notFound: () => { throw new Error('NEXT_NOT_FOUND'); },
}));
vi.mock('next/link', () => ({
  default: ({ children, href, ...props }: { children: React.ReactNode; href: string; [k: string]: unknown }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));

import BlogPost from './page';

describe('BlogPost', () => {
  it('renders a valid post', async () => {
    const slug = 'ai-clinical-decision-support-reduces-diagnostic-errors';
    const Page = await BlogPost({ params: Promise.resolve({ slug }) });
    render(Page);
    expect(screen.getByText(BLOG_POSTS[slug].title)).toBeInTheDocument();
    expect(screen.getByText('2026-09-15')).toBeInTheDocument();
  });

  it('calls notFound for invalid slug', async () => {
    await expect(BlogPost({ params: Promise.resolve({ slug: 'nonexistent' }) })).rejects.toThrow('NEXT_NOT_FOUND');
  });
});

describe('generateStaticParams', () => {
  it('returns params for all posts', () => {
    const params = generateStaticParams();
    expect(params).toHaveLength(Object.keys(BLOG_POSTS).length);
    expect(params[0]).toHaveProperty('slug');
  });
});

describe('generateMetadata', () => {
  it('returns metadata for valid slug', async () => {
    const meta = await generateMetadata({ params: Promise.resolve({ slug: 'ai-clinical-decision-support-reduces-diagnostic-errors' }) });
    expect(meta.title).toContain('Diagnostic Errors');
  });

  it('returns empty for invalid slug', async () => {
    const meta = await generateMetadata({ params: Promise.resolve({ slug: 'nonexistent' }) });
    expect(meta).toEqual({});
  });
});
