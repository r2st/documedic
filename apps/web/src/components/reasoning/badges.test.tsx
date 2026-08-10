import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { AutonomyBadge, CantMissBadge, ProbabilityBandBadge } from './badges';

describe('AutonomyBadge', () => {
  it('labels informational tier without the warning icon', () => {
    render(<AutonomyBadge tier="informational" />);
    expect(screen.getByText('Informational')).toBeInTheDocument();
  });

  it('labels suggestive tier', () => {
    render(<AutonomyBadge tier="suggestive" />);
    expect(screen.getByText('Suggestive')).toBeInTheDocument();
  });

  it('labels flag_for_review and includes the warning icon (Critical Safety Rule #2/#5 visibility)', () => {
    const { container } = render(<AutonomyBadge tier="flag_for_review" />);
    expect(screen.getByText('Flag for review')).toBeInTheDocument();
    expect(container.querySelector('svg')).toBeInTheDocument();
  });
});

describe('ProbabilityBandBadge', () => {
  it.each([
    ['high', 'HIGH'],
    ['moderate', 'MODERATE'],
    ['low', 'LOW'],
    ['very_low', 'VERY LOW'],
    ['insufficient_data', 'INSUFFICIENT DATA'],
  ] as const)('renders a qualitative label for %s, never a raw number', (band, expected) => {
    render(<ProbabilityBandBadge band={band} />);
    expect(screen.getByText(expected)).toBeInTheDocument();
  });

  it('never renders a percentage sign or digit (anti-automation-bias: no false precision)', () => {
    render(<ProbabilityBandBadge band="moderate" />);
    const text = screen.getByText('MODERATE').textContent ?? '';
    expect(text).not.toMatch(/[%0-9]/);
  });
});

describe('CantMissBadge', () => {
  it('always renders the "Can\'t miss" label unconditionally (Critical Safety Rule #3: can\'t-miss output is always shown)', () => {
    render(<CantMissBadge />);
    expect(screen.getByText(/can.t miss/i)).toBeInTheDocument();
  });
});
