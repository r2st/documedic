import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import RouteError from './error';
import GlobalError from './global-error';
import NotFound from './not-found';

/**
 * The route-segment boundaries, which exist for the failures `components/ErrorBoundary` cannot
 * reach: that one is mounted inside `<main>` in each group layout, so a throw in the layout
 * *around* it unwinds straight past it. Next routes those to `error.tsx`, and a throw in the root
 * layout — where `AuthProvider` and the banners mount — to `global-error.tsx`.
 *
 * What is asserted here is mostly the copy, because the copy is the safety requirement. A screen
 * that failed must not read as a screen with nothing to report, and a clinician must not be left
 * wondering whether a half-finished write landed.
 */

let consoleError: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  consoleError.mockRestore();
  vi.restoreAllMocks();
});

const boom = Object.assign(new Error('layout blew up'), { digest: 'abc123' });

describe('app/error.tsx (route segment boundary)', () => {
  it('says the screen is missing rather than empty', () => {
    render(<RouteError error={boom} reset={() => {}} />);

    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent(/could not be displayed/i);
    // The anti-automation-bias contract: absence of content is never absence of findings.
    expect(alert).toHaveTextContent(/not as empty/i);
    expect(alert).toHaveTextContent(/nothing to report/i);
  });

  it('tells the clinician the record was not changed', () => {
    render(<RouteError error={boom} reset={() => {}} />);

    expect(screen.getByRole('alert')).toHaveTextContent(/record is unaffected/i);
  });

  it('retries through the reset Next hands it', async () => {
    const reset = vi.fn();
    render(<RouteError error={boom} reset={reset} />);

    await userEvent.click(screen.getByRole('button', { name: /try again/i }));

    expect(reset).toHaveBeenCalledTimes(1);
  });

  it('offers a way out that does not depend on the router that just failed', async () => {
    const assign = vi.fn();
    vi.spyOn(window, 'location', 'get').mockReturnValue({
      ...window.location,
      assign,
    } as unknown as Location);

    render(<RouteError error={boom} reset={() => {}} />);
    await userEvent.click(screen.getByRole('button', { name: /back to patients/i }));

    expect(assign).toHaveBeenCalledWith('/patients');
  });

  it('shows the digest, which is the only handle support has on a server log', () => {
    render(<RouteError error={boom} reset={() => {}} />);

    expect(screen.getByText(/abc123/)).toBeInTheDocument();
  });

  it('renders without a digest, which client-side throws have none of', () => {
    render(<RouteError error={new Error('client throw')} reset={() => {}} />);

    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.queryByText(/^Reference:/)).not.toBeInTheDocument();
  });

  it('logs the failure locally and sends it nowhere', () => {
    render(<RouteError error={boom} reset={() => {}} />);

    // DPDP residency: a component stack from this app can carry rendered patient props, so the
    // console is deliberately the whole of the reporting path.
    expect(consoleError).toHaveBeenCalledWith(
      expect.stringContaining('Route render failed'),
      'abc123',
      boom,
    );
  });
});

describe('app/global-error.tsx (root layout boundary)', () => {
  it('renders without any provider, stylesheet or shared component', () => {
    // The shell is what failed, so this must stand up with nothing around it. Rendering it bare
    // is the assertion.
    render(<GlobalError error={boom} reset={() => {}} />);

    expect(screen.getByRole('alert')).toHaveTextContent(/could not start/i);
  });

  it('does not let a blank application read as an absence of findings', () => {
    render(<GlobalError error={boom} reset={() => {}} />);

    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent(/no part of the record has been shown/i);
    expect(alert).toHaveTextContent(/absence of findings/i);
    expect(alert).toHaveTextContent(/record is unaffected/i);
  });

  it('reloads through the reset Next hands it', async () => {
    const reset = vi.fn();
    render(<GlobalError error={boom} reset={reset} />);

    await userEvent.click(screen.getByRole('button', { name: /reload the application/i }));

    expect(reset).toHaveBeenCalledTimes(1);
  });

  it('logs the failure locally', () => {
    render(<GlobalError error={boom} reset={() => {}} />);

    expect(consoleError).toHaveBeenCalledWith(
      expect.stringContaining('Application shell failed'),
      'abc123',
      boom,
    );
  });
});

describe('app/not-found.tsx', () => {
  it('distinguishes a broken link from a patient with no record', () => {
    render(<NotFound />);

    expect(screen.getByRole('heading', { name: /does not exist/i })).toBeInTheDocument();
    expect(screen.getByText(/not a statement about any patient/i)).toBeInTheDocument();
  });

  it('leads back to a screen that works', () => {
    render(<NotFound />);

    expect(screen.getByRole('link', { name: /back to patients/i })).toHaveAttribute(
      'href',
      '/patients',
    );
  });
});
