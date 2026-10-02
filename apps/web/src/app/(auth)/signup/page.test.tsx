import { render } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

const replace = vi.fn();
vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace, push: vi.fn(), refresh: vi.fn(), back: vi.fn() }),
}));

import SignupRedirect from './page';

describe('SignupRedirect', () => {
  it('redirects to the landing page', () => {
    render(<SignupRedirect />);
    expect(replace).toHaveBeenCalledWith('/');
  });
});
