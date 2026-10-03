import { render, screen, fireEvent } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { BmiCalculator, bmiCategory } from './calculator';

vi.mock('next/link', () => ({
  default: ({ children, href, ...props }: { children: React.ReactNode; href: string; [k: string]: unknown }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));

describe('bmiCategory', () => {
  it('returns Underweight for BMI < 18.5', () => {
    expect(bmiCategory(17)).toEqual(expect.objectContaining({ label: 'Underweight' }));
  });
  it('returns Normal for BMI 18.5–24.9', () => {
    expect(bmiCategory(22)).toEqual(expect.objectContaining({ label: 'Normal' }));
  });
  it('returns Overweight for BMI 25–29.9', () => {
    expect(bmiCategory(27)).toEqual(expect.objectContaining({ label: 'Overweight' }));
  });
  it('returns Obese for BMI >= 30', () => {
    expect(bmiCategory(35)).toEqual(expect.objectContaining({ label: 'Obese' }));
  });
});

describe('BmiCalculator', () => {
  it('renders with default values and shows BMI', () => {
    render(<BmiCalculator />);
    expect(screen.getByText('BMI Calculator')).toBeInTheDocument();
    expect(screen.getByText('Your BMI')).toBeInTheDocument();
    expect(screen.getByText('Normal')).toBeInTheDocument();
  });

  it('switches to imperial units', () => {
    render(<BmiCalculator />);
    fireEvent.click(screen.getByText('imperial'));
    expect(screen.getByText('Weight (lbs)')).toBeInTheDocument();
    expect(screen.getByText('Height (inches)')).toBeInTheDocument();
  });

  it('updates BMI when weight changes', () => {
    render(<BmiCalculator />);
    const inputs = screen.getAllByRole('spinbutton');
    fireEvent.change(inputs[0], { target: { value: '120' } });
    expect(screen.getByText('Obese')).toBeInTheDocument();
  });

  it('handles zero height gracefully', () => {
    render(<BmiCalculator />);
    const inputs = screen.getAllByRole('spinbutton');
    fireEvent.change(inputs[1], { target: { value: '0' } });
    expect(screen.queryByText('Your BMI')).not.toBeInTheDocument();
  });

  it('shows medical disclaimer', () => {
    render(<BmiCalculator />);
    expect(screen.getByText(/informational purposes only/)).toBeInTheDocument();
  });
});
