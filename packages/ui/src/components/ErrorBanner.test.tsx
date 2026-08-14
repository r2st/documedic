import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { ErrorBanner } from './ErrorBanner';

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
