import { render, screen, fireEvent } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { SymptomTriage, triageLevel, QUESTIONS } from './triage';

vi.mock('next/link', () => ({
  default: ({ children, href, ...props }: { children: React.ReactNode; href: string; [k: string]: unknown }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));

describe('triageLevel', () => {
  it('returns emergency for score >= 15', () => {
    expect(triageLevel(15).level).toBe('emergency');
    expect(triageLevel(20).level).toBe('emergency');
  });
  it('returns urgent for score 10–14', () => {
    expect(triageLevel(10).level).toBe('urgent');
    expect(triageLevel(14).level).toBe('urgent');
  });
  it('returns soon for score 5–9', () => {
    expect(triageLevel(5).level).toBe('soon');
    expect(triageLevel(9).level).toBe('soon');
  });
  it('returns self-care for score < 5', () => {
    expect(triageLevel(0).level).toBe('self-care');
    expect(triageLevel(4).level).toBe('self-care');
  });
});

describe('QUESTIONS', () => {
  it('has 5 questions', () => {
    expect(QUESTIONS).toHaveLength(5);
  });
  it('each question has options', () => {
    for (const q of QUESTIONS) {
      expect(q.options.length).toBeGreaterThanOrEqual(2);
      expect(q.text.length).toBeGreaterThan(0);
      expect(q.id.length).toBeGreaterThan(0);
    }
  });
});

describe('SymptomTriage', () => {
  it('renders first question', () => {
    render(<SymptomTriage />);
    expect(screen.getByText('Symptom Triage')).toBeInTheDocument();
    expect(screen.getByText('How severe are your symptoms?')).toBeInTheDocument();
    expect(screen.getByText('Question 1 of 5')).toBeInTheDocument();
  });

  it('advances through questions', () => {
    render(<SymptomTriage />);
    fireEvent.click(screen.getByText('Mild — barely noticeable'));
    expect(screen.getByText('Question 2 of 5')).toBeInTheDocument();
  });

  it('shows result after all questions answered', () => {
    render(<SymptomTriage />);
    // Answer all with lowest scores
    fireEvent.click(screen.getByText('Mild — barely noticeable'));
    fireEvent.click(screen.getByText('Less than 24 hours'));
    fireEvent.click(screen.getByText('No'));
    fireEvent.click(screen.getByText('No fever'));
    // Last question - need to click the "No" for chest pain
    const noButtons = screen.getAllByText('No');
    fireEvent.click(noButtons[0]);
    expect(screen.getByText('Self-Care')).toBeInTheDocument();
    expect(screen.getByText('Start Over')).toBeInTheDocument();
  });

  it('resets on Start Over', () => {
    render(<SymptomTriage />);
    fireEvent.click(screen.getByText('Mild — barely noticeable'));
    fireEvent.click(screen.getByText('Less than 24 hours'));
    fireEvent.click(screen.getByText('No'));
    fireEvent.click(screen.getByText('No fever'));
    const noButtons = screen.getAllByText('No');
    fireEvent.click(noButtons[0]);
    fireEvent.click(screen.getByText('Start Over'));
    expect(screen.getByText('Question 1 of 5')).toBeInTheDocument();
  });

  it('shows disclaimer', () => {
    render(<SymptomTriage />);
    expect(screen.getByText(/NOT a substitute for professional medical advice/)).toBeInTheDocument();
  });
});
