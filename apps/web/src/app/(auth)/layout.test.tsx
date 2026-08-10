import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import AuthLayout from './layout';

describe('AuthLayout', () => {
  it('wraps the auth forms in the branded shell', () => {
    render(<AuthLayout><p>form</p></AuthLayout>);

    expect(screen.getByRole('img', { name: 'Aether Clinician' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Aether Clinician');
    expect(screen.getByText('Diagnostic & management decision support')).toBeInTheDocument();
    expect(screen.getByText('form')).toBeInTheDocument();
  });
});
