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

// A backend deployed ahead of the web app is the ordinary case, not the exotic one, and
// `ReasoningTheatre` renders this badge from an unvalidated cast of raw SSE JSON. Every value
// below reached `TIER[tier]` as `undefined` and threw on `.cls`, taking out the live reasoning
// screen mid-run.
describe('AutonomyBadge given a tier this build does not recognise', () => {
  const unknown = [
    ['a tier added by a newer backend', 'advisory'],
    ['a renamed tier', 'flag-for-review'],
    ['a missing field', undefined],
    ['a null field', null],
    ['an empty string', ''],
    ['a non-string', 42],
  ] as const;

  it.each(unknown)('does not throw for %s', (_label, tier) => {
    expect(() =>
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      render(<AutonomyBadge tier={tier as any} />),
    ).not.toThrow();
  });

  it.each(unknown)(
    'falls back to the most conservative tier for %s (Critical Safety Rule #2)',
    (_label, tier) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const { container } = render(<AutonomyBadge tier={tier as any} />);
      expect(screen.getByText('Flag for review')).toBeInTheDocument();
      // The warning icon is what distinguishes the flagged tier from the quiet ones.
      expect(container.querySelector('svg')).toBeInTheDocument();
      expect(container.querySelector('.bg-amber-50')).toBeInTheDocument();
    },
  );

  it('never quietly renders an unrecognised tier as informational or suggestive', () => {
    render(<AutonomyBadge tier={'advisory' as never} />);
    expect(screen.queryByText('Informational')).not.toBeInTheDocument();
    expect(screen.queryByText('Suggestive')).not.toBeInTheDocument();
  });

  it('says that the tier was not recognised rather than silently relabelling it', () => {
    const { container } = render(<AutonomyBadge tier={'advisory' as never} />);
    expect(container.textContent).toContain('advisory');
    expect(container.textContent).toMatch(/not one this version recognises/i);
  });

  it('keeps the note out of the visible label, so a known tier reads identically', () => {
    const { container } = render(<AutonomyBadge tier="flag_for_review" />);
    expect(container.querySelector('.sr-only')).not.toBeInTheDocument();
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

  it.each([
    ['a band added by a newer backend', 'indeterminate', 'INDETERMINATE'],
    ['a multi-underscore band name', 'very_low_confidence', 'VERY LOW CONFIDENCE'],
  ] as const)('renders %s without throwing', (_label, band, expected) => {
    expect(() => render(<ProbabilityBandBadge band={band as never} />)).not.toThrow();
    expect(screen.getByText(expected)).toBeInTheDocument();
  });

  it.each([
    ['null', null],
    ['undefined', undefined],
    ['a number', 0.7],
  ] as const)('does not throw when the band field is %s', (_label, band) => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect(() => render(<ProbabilityBandBadge band={band as any} />)).not.toThrow();
  });

  it('never invents a band the payload did not carry', () => {
    render(<ProbabilityBandBadge band={undefined as never} />);
    expect(screen.getByText('UNSPECIFIED')).toBeInTheDocument();
    for (const known of ['HIGH', 'MODERATE', 'LOW', 'VERY LOW', 'INSUFFICIENT DATA']) {
      expect(screen.queryByText(known)).not.toBeInTheDocument();
    }
  });
});

describe('CantMissBadge', () => {
  it('always renders the "Can\'t miss" label unconditionally (Critical Safety Rule #3: can\'t-miss output is always shown)', () => {
    render(<CantMissBadge />);
    expect(screen.getByText(/can.t miss/i)).toBeInTheDocument();
  });
});
