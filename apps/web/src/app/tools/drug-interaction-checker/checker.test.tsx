import { render, screen, fireEvent } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { DrugInteractionChecker, findInteractions, INTERACTIONS } from './checker';

vi.mock('next/link', () => ({
  default: ({ children, href, ...props }: { children: React.ReactNode; href: string; [k: string]: unknown }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));

describe('findInteractions', () => {
  it('returns empty for fewer than 2 drugs', () => {
    expect(findInteractions([])).toEqual([]);
    expect(findInteractions(['Warfarin'])).toEqual([]);
  });

  it('finds Warfarin + Aspirin interaction', () => {
    const result = findInteractions(['Warfarin', 'Aspirin']);
    expect(result).toHaveLength(1);
    expect(result[0].severity).toBe('major');
  });

  it('finds multiple interactions', () => {
    const result = findInteractions(['Warfarin', 'Aspirin', 'Ibuprofen']);
    expect(result.length).toBeGreaterThanOrEqual(2);
  });

  it('is case-insensitive', () => {
    const result = findInteractions(['warfarin', 'aspirin']);
    expect(result).toHaveLength(1);
  });

  it('returns empty when no interactions exist', () => {
    const result = findInteractions(['Metformin', 'Simvastatin']);
    expect(result).toHaveLength(0);
  });
});

describe('INTERACTIONS database', () => {
  it('has 20 entries', () => {
    expect(INTERACTIONS).toHaveLength(20);
  });

  it('each entry has required fields', () => {
    for (const i of INTERACTIONS) {
      expect(i.drugs).toHaveLength(2);
      expect(['major', 'moderate', 'minor']).toContain(i.severity);
      expect(i.description.length).toBeGreaterThan(0);
    }
  });
});

describe('DrugInteractionChecker', () => {
  it('renders all drug buttons', () => {
    render(<DrugInteractionChecker />);
    expect(screen.getByText('Warfarin')).toBeInTheDocument();
    expect(screen.getByText('Aspirin')).toBeInTheDocument();
  });

  it('shows interaction result when two drugs selected', () => {
    render(<DrugInteractionChecker />);
    fireEvent.click(screen.getByText('Warfarin'));
    fireEvent.click(screen.getByText('Aspirin'));
    expect(screen.getByText('1 interaction found')).toBeInTheDocument();
    expect(screen.getByText(/Increased risk of bleeding/)).toBeInTheDocument();
  });

  it('shows no interactions message for non-interacting drugs', () => {
    render(<DrugInteractionChecker />);
    fireEvent.click(screen.getByText('Metformin'));
    fireEvent.click(screen.getByText('Simvastatin'));
    expect(screen.getByText('No known interactions found')).toBeInTheDocument();
  });

  it('deselects a drug on second click', () => {
    render(<DrugInteractionChecker />);
    fireEvent.click(screen.getByText('Warfarin'));
    fireEvent.click(screen.getByText('Aspirin'));
    expect(screen.getByText('1 interaction found')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Aspirin'));
    expect(screen.queryByText('1 interaction found')).not.toBeInTheDocument();
  });

  it('shows medical disclaimer', () => {
    render(<DrugInteractionChecker />);
    expect(screen.getByText(/educational purposes only/)).toBeInTheDocument();
  });
});
