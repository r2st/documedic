import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { Button, Card, ConfidenceBadge, ErrorBanner, SafetyFlagCard } from './ui';

describe('Card', () => {
  it('renders children', () => {
    render(<Card>hello</Card>);
    expect(screen.getByText('hello')).toBeInTheDocument();
  });
});

describe('Button', () => {
  it('fires onClick', async () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Save</Button>);
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onClick).toHaveBeenCalledOnce();
  });

  it('disables interaction when disabled', async () => {
    const onClick = vi.fn();
    render(
      <Button onClick={onClick} disabled>
        Save
      </Button>,
    );
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onClick).not.toHaveBeenCalled();
  });
});

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

describe('ErrorBanner', () => {
  it('announces itself, since it appears far from wherever focus is', () => {
    render(<ErrorBanner message="The upload did not complete." />);
    expect(screen.getByRole('alert')).toHaveTextContent('The upload did not complete.');
  });

  it('offers no retry unless one is given', () => {
    // Deliberate: most banners here follow a *write*, and a retry button beside "this may not
    // have completed" invites a second attempt at work that may already be in the chart.
    render(<ErrorBanner message="The approval may not have completed." />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('runs the retry when one is given', async () => {
    const onRetry = vi.fn();
    render(<ErrorBanner message="Could not reach the server." onRetry={onRetry} />);

    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));

    expect(onRetry).toHaveBeenCalledOnce();
  });

  it('locks the retry out while one is already in flight', async () => {
    const onRetry = vi.fn();
    render(<ErrorBanner message="Could not reach the server." onRetry={onRetry} retrying />);

    const button = screen.getByRole('button', { name: 'Retrying…' });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('aria-busy', 'true');

    await userEvent.click(button);
    expect(onRetry).not.toHaveBeenCalled();
  });

  it('takes a specific retry label where "Try again" would mislead', () => {
    // On the encounter screen the retry re-reads results the agents already produced, and a
    // clinician who just watched eight agents deliberate needs to know it is not a re-run.
    render(
      <ErrorBanner
        message="Could not load the results."
        onRetry={vi.fn()}
        retryLabel="Fetch results again"
      />,
    );
    expect(screen.getByRole('button', { name: 'Fetch results again' })).toBeInTheDocument();
  });
});
