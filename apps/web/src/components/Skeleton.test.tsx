import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { LoadingBlock, Skeleton, SkeletonCards, SkeletonList } from './Skeleton';

describe('Skeleton', () => {
  it('is hidden from assistive technology', () => {
    // A grey rectangle means "loading" to the eye and nothing at all to a screen reader.
    // Announcing each one would read a row of empty boxes aloud; LoadingBlock speaks instead.
    const { container } = render(<Skeleton className="h-4 w-20" />);
    expect(container.firstChild).toHaveAttribute('aria-hidden', 'true');
  });

  it('carries the sizing classes it is given through to the element', () => {
    const { container } = render(<Skeleton className="h-7 w-52" />);
    expect((container.firstChild as HTMLElement).className).toContain('h-7 w-52');
  });

  it('animates, so the wait reads as a wait rather than as broken layout', () => {
    const { container } = render(<Skeleton />);
    expect((container.firstChild as HTMLElement).className).toContain('animate-pulse');
  });
});

describe('LoadingBlock', () => {
  it('announces what is being waited on', () => {
    render(
      <LoadingBlock label="Loading patient record">
        <Skeleton className="h-4" />
      </LoadingBlock>,
    );

    const status = screen.getByRole('status', { name: 'Loading patient record' });
    expect(status).toHaveAttribute('aria-busy', 'true');
  });

  it('renders its placeholders inside the announced region', () => {
    render(
      <LoadingBlock label="Loading patients">
        <p>placeholder</p>
      </LoadingBlock>,
    );

    expect(screen.getByRole('status')).toHaveTextContent('placeholder');
  });

  it('accepts layout classes, so a caller can lay the placeholders out in a grid', () => {
    render(
      <LoadingBlock label="Loading performance metrics" className="grid grid-cols-2">
        <Skeleton />
      </LoadingBlock>,
    );

    expect(screen.getByRole('status').className).toContain('grid grid-cols-2');
  });
});

describe('SkeletonList', () => {
  it('renders the requested number of placeholder rows', () => {
    const { container } = render(<SkeletonList rows={5} />);
    expect(container.querySelectorAll('.shadow-card')).toHaveLength(5);
  });

  it('defaults to three rows, roughly a screenful without overpromising a long list', () => {
    const { container } = render(<SkeletonList />);
    expect(container.querySelectorAll('.shadow-card')).toHaveLength(3);
  });
});

describe('SkeletonCards', () => {
  it('renders the requested number of placeholder cards', () => {
    const { container } = render(<SkeletonCards count={6} />);
    expect(container.querySelectorAll('.animate-pulse')).toHaveLength(12); // two bars per card
  });

  it('defaults to four cards', () => {
    const { container } = render(<SkeletonCards />);
    expect(container.querySelectorAll('.animate-pulse')).toHaveLength(8);
  });

  it('lets the caller drive both the grid and the card styling', () => {
    const { container } = render(
      <SkeletonCards count={1} className="flex" cardClassName="border-t-4" />,
    );
    expect((container.firstChild as HTMLElement).className).toBe('flex');
    expect(container.querySelector('.border-t-4')).toBeInTheDocument();
  });
});
