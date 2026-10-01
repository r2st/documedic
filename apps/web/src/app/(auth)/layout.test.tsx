import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import AuthLayout from './layout';

describe('AuthLayout', () => {
  it('wraps the auth forms in the branded shell', () => {
    render(
      <AuthLayout>
        <p>form</p>
      </AuthLayout>,
    );

    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('DoAide Clinician');
    expect(screen.getByText('AI clinical decision support for modern healthcare')).toBeInTheDocument();
    expect(screen.getByText('form')).toBeInTheDocument();
  });

  it('shows the DoAide parent brand attribution', () => {
    render(
      <AuthLayout>
        <p>form</p>
      </AuthLayout>,
    );

    const link = screen.getByRole('link', { name: 'DoAide' });
    expect(link).toHaveAttribute('href', 'https://doaide.com');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.getByText(/Product/)).toBeInTheDocument();
  });
});
