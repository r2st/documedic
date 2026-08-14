import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { ConfidenceBadge, SafetyFlagCard } from './ui';

describe('ConfidenceBadge', () => {
  it('renders the confidence as a whole-number percentage', () => {
    render(<ConfidenceBadge band="high" value={0.87} />);
    expect(screen.getByText('87%')).toBeInTheDocument();
  });
});

describe('SafetyFlagCard', () => {
  it('labels a hard block distinctly from a plain severity (Critical Safety Rule #3)', () => {
    render(<SafetyFlagCard severity="hard_block" isHardBlock summary="Allergy conflict" />);
    expect(screen.getByText('HARD BLOCK')).toBeInTheDocument();
    expect(screen.getByText('Allergy conflict')).toBeInTheDocument();
  });

  it('labels a non-blocking flag with its severity, not "HARD BLOCK"', () => {
    render(<SafetyFlagCard severity="warning" isHardBlock={false} summary="Renal caution" />);
    expect(screen.queryByText('HARD BLOCK')).not.toBeInTheDocument();
    expect(screen.getByText('warning')).toBeInTheDocument();
  });

  it('names the check alongside the severity so a chart-level finding is legible as one', () => {
    // Without the label this is an amber box reading "warning" and nothing else — the same
    // rendering a duplicate-therapy note or a guideline deviation gets.
    render(
      <SafetyFlagCard
        severity="warning"
        isHardBlock={false}
        checkType="hepatic_severity"
        summary="Child-Pugh class C on this chart's labs."
      />,
    );
    expect(screen.getByText('Liver function')).toBeInTheDocument();
    expect(screen.getByText('warning')).toBeInTheDocument();
  });

  it('is a listitem so a screen reader can announce how many findings there are', () => {
    render(
      <SafetyFlagCard severity="warning" isHardBlock={false} summary="A" checkType="renal_dose" />,
    );
    expect(screen.getByRole('listitem')).toBeInTheDocument();
  });

  it('renders without a check type for a caller that has none', () => {
    render(<SafetyFlagCard severity="warning" isHardBlock={false} summary="No type supplied" />);
    expect(screen.getByText('No type supplied')).toBeInTheDocument();
    expect(screen.queryByText('Safety check')).not.toBeInTheDocument();
  });
});

describe('ConfidenceBadge fallbacks', () => {
  it('falls back to a neutral style for an unrecognised confidence band', () => {
    render(<ConfidenceBadge band="unheard-of" value={0.5} />);
    expect(screen.getByText('50%').className).toContain('bg-slate-50');
  });
});

describe('SafetyFlagCard fallbacks', () => {
  it('still renders the summary for an unrecognised severity', () => {
    render(
      <SafetyFlagCard severity="mystery_level" isHardBlock={false} summary="Unclassified flag" />,
    );
    expect(screen.getByText('mystery level')).toBeInTheDocument();
    expect(screen.getByText('Unclassified flag')).toBeInTheDocument();
  });
});
