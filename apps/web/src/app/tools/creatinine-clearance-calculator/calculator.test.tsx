import { render, screen, fireEvent } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { CreatinineClearanceCalculator, calculateCrCl, renalCategory } from './calculator';

describe('calculateCrCl', () => {
  it('computes the Cockcroft-Gault estimate for a male', () => {
    // ((140 - 60) × 70) / (72 × 1.0) = 77.78
    expect(calculateCrCl(60, 70, 1.0, 'male')).toBeCloseTo(77.78, 1);
  });

  it('applies the 0.85 female factor', () => {
    expect(calculateCrCl(60, 70, 1.0, 'female')).toBeCloseTo(66.11, 1);
  });

  it('returns 0 for invalid inputs', () => {
    expect(calculateCrCl(0, 70, 1.0, 'male')).toBe(0);
    expect(calculateCrCl(60, 70, 0, 'male')).toBe(0);
  });
});

describe('renalCategory', () => {
  it('categorises renal function bands', () => {
    expect(renalCategory(95).label).toBe('Normal renal function');
    expect(renalCategory(75).label).toBe('Mildly reduced');
    expect(renalCategory(45).label).toBe('Moderately reduced');
    expect(renalCategory(20).label).toBe('Severely reduced');
    expect(renalCategory(10).label).toBe('Kidney failure range');
  });
});

describe('CreatinineClearanceCalculator', () => {
  it('renders title and default result', () => {
    render(<CreatinineClearanceCalculator />);
    expect(screen.getByText('Creatinine Clearance Calculator')).toBeInTheDocument();
    expect(screen.getByText('77.8')).toBeInTheDocument();
  });

  it('recalculates when switching to female', () => {
    render(<CreatinineClearanceCalculator />);
    fireEvent.click(screen.getByRole('button', { name: 'female' }));
    expect(screen.getByText('66.1')).toBeInTheDocument();
  });

  it('shows the medical disclaimer', () => {
    render(<CreatinineClearanceCalculator />);
    expect(screen.getByText(/educational purposes only/)).toBeInTheDocument();
  });
});
