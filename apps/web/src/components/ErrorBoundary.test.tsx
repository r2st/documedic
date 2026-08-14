import { useState } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ErrorBoundary } from './ErrorBoundary';

/**
 * A component that throws on its first render and, once `healed` flips, stops.
 *
 * Modelled on the real failure: a payload the component cannot render. The retry only helps
 * if something about the data changed, which is what `onReset` is for — so the tests below
 * cover both a reset that fixes it and one that does not.
 */
function Boom({ healed = false }: { healed?: boolean }) {
  if (!healed) throw new Error('malformed suggestion payload');
  return <p>differential rendered</p>;
}

let consoleError: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  // React prints the caught error and a component stack of its own on every boundary hit.
  // Silenced so a passing suite is not full of red, but kept as a spy: componentDidCatch
  // writing this line is the only record an operator gets, and it is asserted below.
  consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  consoleError.mockRestore();
});

describe('ErrorBoundary', () => {
  it('renders its children untouched when nothing throws', () => {
    render(
      <ErrorBoundary section="The timeline">
        <p>chart content</p>
      </ErrorBoundary>,
    );

    expect(screen.getByText('chart content')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('catches a render failure instead of unmounting the tree', () => {
    render(
      <ErrorBoundary section="The reasoning results">
        <Boom />
      </ErrorBoundary>,
    );

    // The whole point: something is on screen. Before this, React unmounted the tree and the
    // clinician was left on a blank page mid-consultation.
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(
      screen.getByRole('heading', { name: /The reasoning results could not be displayed/i }),
    ).toBeInTheDocument();
  });

  it('names the section that failed, so the clinician knows what is missing', () => {
    render(
      <ErrorBoundary section="The timeline">
        <Boom />
      </ErrorBoundary>,
    );

    expect(screen.getByText(/The timeline could not be displayed/i)).toBeInTheDocument();
  });

  it('says the section is missing rather than empty (anti-automation-bias)', () => {
    // The clinically load-bearing assertion in this file. A blank or apologetic panel where a
    // differential should be reads as "nothing to report", and a clinician acting on that has
    // been told something false by omission — the same failure CLAUDE.md rule #6 is about.
    render(
      <ErrorBoundary section="The reasoning results">
        <Boom />
      </ErrorBoundary>,
    );

    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent(/treat this section as missing, not as empty/i);
    expect(alert).toHaveTextContent(/nothing to report/i);
    expect(alert).toHaveTextContent(/Nothing here is a clinical finding/i);
  });

  it('says the record is unaffected, which is the other question a clinician has', () => {
    render(
      <ErrorBoundary section="The timeline">
        <Boom />
      </ErrorBoundary>,
    );

    expect(screen.getByRole('alert')).toHaveTextContent(/record itself is unaffected/i);
  });

  it('logs the failure with the section name and the component stack', () => {
    render(
      <ErrorBoundary section="The timeline">
        <Boom />
      </ErrorBoundary>,
    );

    const logged = consoleError.mock.calls.find(
      (args) => typeof args[0] === 'string' && args[0].includes('Render failed in The timeline'),
    );
    expect(logged).toBeDefined();
    expect((logged?.[1] as Error).message).toBe('malformed suggestion payload');
  });

  it('recovers when the retry is backed by data that now renders', async () => {
    function Owner() {
      const [healed, setHealed] = useState(false);
      return (
        <ErrorBoundary section="The reasoning results" onReset={() => setHealed(true)}>
          <Boom healed={healed} />
        </ErrorBoundary>
      );
    }

    render(<Owner />);
    expect(screen.getByRole('alert')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));

    expect(screen.getByText('differential rendered')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('falls straight back to the fallback when the retry changes nothing', async () => {
    // The boundary must not flicker between fallback and crash: clearing the error re-renders
    // the same payload, which throws again and is caught again.
    render(
      <ErrorBoundary section="The timeline">
        <Boom />
      </ErrorBoundary>,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));

    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByText(/The timeline could not be displayed/i)).toBeInTheDocument();
  });

  it('works without an onReset, for a boundary whose owner has nothing to re-fetch', async () => {
    render(
      <ErrorBoundary section="The sign-in form">
        <Boom />
      </ErrorBoundary>,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));

    expect(screen.getByRole('alert')).toBeInTheDocument();
  });

  it('calls onReset so the owner can go and fetch different data', async () => {
    const onReset = vi.fn();
    render(
      <ErrorBoundary section="The timeline" onReset={onReset}>
        <Boom />
      </ErrorBoundary>,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));

    expect(onReset).toHaveBeenCalledOnce();
  });
});
