import { render, screen, fireEvent } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ChadsVascCalculator, chadsVascScore, recommendation } from './calculator';

const base = {
  chf: false,
  hypertension: false,
  ageBand: 'under65' as const,
  diabetes: false,
  strokeTia: false,
  vascular: false,
  female: false,
};

describe('chadsVascScore', () => {
  it('scores 0 with no risk factors', () => {
    expect(chadsVascScore(base)).toBe(0);
  });

  it('gives 2 points for age 75+ and prior stroke', () => {
    expect(chadsVascScore({ ...base, ageBand: '75plus' })).toBe(2);
    expect(chadsVascScore({ ...base, strokeTia: true })).toBe(2);
  });

  it('reaches the maximum of 9', () => {
    expect(
      chadsVascScore({
        chf: true,
        hypertension: true,
        ageBand: '75plus',
        diabetes: true,
        strokeTia: true,
        vascular: true,
        female: true,
      }),
    ).toBe(9);
  });
});

describe('recommendation', () => {
  it('recommends no therapy at effective score 0', () => {
    expect(recommendation(0, false)).toMatch(/No antithrombotic/);
    expect(recommendation(1, true)).toMatch(/No antithrombotic/);
  });

  it('recommends anticoagulation at effective score 2+', () => {
    expect(recommendation(2, false)).toMatch(/recommended unless contraindicated/);
  });
});

describe('ChadsVascCalculator', () => {
  it('renders with a zero score by default', () => {
    render(<ChadsVascCalculator />);
    expect(screen.getByText('CHA₂DS₂-VASc Calculator')).toBeInTheDocument();
    expect(screen.getByText('0 / 9')).toBeInTheDocument();
  });

  it('updates the score when factors are selected', () => {
    render(<ChadsVascCalculator />);
    fireEvent.click(screen.getByText('Hypertension'));
    fireEvent.click(screen.getByText('75 or older (+2)'));
    expect(screen.getByText('3 / 9')).toBeInTheDocument();
  });

  it('shows the medical disclaimer', () => {
    render(<ChadsVascCalculator />);
    expect(screen.getByText(/educational purposes only/)).toBeInTheDocument();
  });
});
