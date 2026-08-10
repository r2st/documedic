import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { Button, Card, ConfidenceBadge, SafetyFlagCard } from './ui';

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
