import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import DashboardLoading from './loading';

describe('DashboardLoading', () => {
  it('renders a route-level loading placeholder rather than an empty frame', () => {
    render(<DashboardLoading />);
    expect(screen.getByText('Loading...')).toBeInTheDocument();
    expect(screen.getByText('Please wait a moment')).toBeInTheDocument();
  });
});
