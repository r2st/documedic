import { render } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

const replace = vi.fn();
vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace, push: vi.fn(), refresh: vi.fn(), back: vi.fn() }),
}));

import LoginRedirect from './page';

describe('LoginRedirect', () => {
  it('redirects to the landing page', () => {
    render(<LoginRedirect />);
    expect(replace).toHaveBeenCalledWith('/');
  });
});
