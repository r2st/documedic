import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('next/link', () => ({
  default: ({ children, href, ...props }: { children: React.ReactNode; href: string; [k: string]: unknown }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));

import ToolsIndex from './page';

describe('ToolsIndex', () => {
  it('renders all five tool cards', () => {
    render(<ToolsIndex />);
    expect(screen.getByText('BMI Calculator')).toBeInTheDocument();
    expect(screen.getByText('Drug Interaction Checker')).toBeInTheDocument();
    expect(screen.getByText('Symptom Triage')).toBeInTheDocument();
    expect(screen.getByText('Creatinine Clearance Calculator')).toBeInTheDocument();
    expect(screen.getByText('CHA₂DS₂-VASc Calculator')).toBeInTheDocument();
  });

  it('links to correct tool paths', () => {
    render(<ToolsIndex />);
    expect(screen.getByText('BMI Calculator').closest('a')?.getAttribute('href')).toBe('/tools/bmi-calculator');
    expect(screen.getByText('Drug Interaction Checker').closest('a')?.getAttribute('href')).toBe('/tools/drug-interaction-checker');
    expect(screen.getByText('Symptom Triage').closest('a')?.getAttribute('href')).toBe('/tools/symptom-triage');
    expect(screen.getByText('Creatinine Clearance Calculator').closest('a')?.getAttribute('href')).toBe('/tools/creatinine-clearance-calculator');
    expect(screen.getByText('CHA₂DS₂-VASc Calculator').closest('a')?.getAttribute('href')).toBe('/tools/chads-vasc-calculator');
  });
});
