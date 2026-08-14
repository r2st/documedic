import { render, screen } from '@testing-library/react';
import { act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { DemoBanner, OfflineBanner } from './Banners';

describe('DemoBanner', () => {
  const original = process.env.NEXT_PUBLIC_DEMO_MODE;

  afterEach(() => {
    process.env.NEXT_PUBLIC_DEMO_MODE = original;
  });

  it('is persistently visible when demo mode is on (Critical Safety Rule / P1-11a)', () => {
    process.env.NEXT_PUBLIC_DEMO_MODE = 'true';
    render(<DemoBanner />);
    expect(screen.getByText(/Demo build/)).toBeInTheDocument();
  });

  it('renders nothing when demo mode is off', () => {
    process.env.NEXT_PUBLIC_DEMO_MODE = 'false';
    const { container } = render(<DemoBanner />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe('OfflineBanner', () => {
  const setOnLine = (value: boolean) =>
    Object.defineProperty(window.navigator, 'onLine', {
      value,
      configurable: true,
      writable: true,
    });

  beforeEach(() => setOnLine(true));

  it('shows nothing while online, but keeps its live region mounted and empty', () => {
    setOnLine(true);
    render(<OfflineBanner />);

    expect(screen.queryByText(/Offline Mode/)).not.toBeInTheDocument();
    // The region has to pre-exist the message it will carry: a live region that appears
    // already populated is not reliably announced. See the comment in Banners.tsx.
    expect(screen.getByRole('status')).toBeEmptyDOMElement();
  });

  it('appears when the browser goes offline and clears the AI-features caveat, keeping records available', () => {
    setOnLine(true);
    render(<OfflineBanner />);
    expect(screen.queryByText(/Offline Mode/)).not.toBeInTheDocument();

    act(() => {
      setOnLine(false);
      window.dispatchEvent(new Event('offline'));
    });
    expect(screen.getByText(/Offline Mode/)).toBeInTheDocument();
    expect(
      screen.getByText(/patient records and drug-safety checks available/),
    ).toBeInTheDocument();
  });

  it('disappears again once back online', () => {
    setOnLine(false);
    render(<OfflineBanner />);
    expect(screen.getByText(/Offline Mode/)).toBeInTheDocument();

    act(() => {
      setOnLine(true);
      window.dispatchEvent(new Event('online'));
    });
    expect(screen.queryByText(/Offline Mode/)).not.toBeInTheDocument();
  });
});
