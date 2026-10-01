// The root layout renders <html>/<head>/<body>, which cannot be mounted inside a jsdom
// container, so its markup is asserted through the server renderer instead.
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('./globals.css', () => ({}));

import RootLayout, { metadata, viewport } from './layout';

function markup(apiUrl?: string): string {
  if (apiUrl === undefined) delete process.env.NEXT_PUBLIC_API_URL;
  else process.env.NEXT_PUBLIC_API_URL = apiUrl;
  return renderToStaticMarkup(
    <RootLayout>
      <p>page</p>
    </RootLayout>,
  );
}

describe('RootLayout', () => {
  const originalApiUrl = process.env.NEXT_PUBLIC_API_URL;

  afterEach(() => {
    if (originalApiUrl === undefined) delete process.env.NEXT_PUBLIC_API_URL;
    else process.env.NEXT_PUBLIC_API_URL = originalApiUrl;
  });

  it('renders the app shell around the routed page', () => {
    const html = markup('http://localhost:8000');
    expect(html).toContain('<html lang="en">');
    expect(html).toContain('<p>page</p>');
  });

  it('preconnects to the API origin, dropping any path from the configured URL', () => {
    const html = markup('https://api.documedic.in/api/v1');
    expect(html).toContain('rel="preconnect" href="https://api.documedic.in"');
    expect(html).toContain('rel="dns-prefetch" href="https://api.documedic.in"');
    expect(html).not.toContain('https://api.documedic.in/api/v1"');
  });

  it('skips the API preconnect hints when the configured URL is unparseable', () => {
    const html = markup('not a url');
    expect(html).not.toContain('dns-prefetch');
    // Font preconnects are unconditional and must survive.
    expect(html).toContain('https://fonts.googleapis.com');
  });

  it('falls back to the local API origin when none is configured', () => {
    const html = markup(undefined);
    expect(html).toContain('rel="preconnect" href="http://localhost:8000"');
  });

  it('declares the PWA metadata the installable app depends on', () => {
    expect(metadata.title).toBe('DoAide Med');
    expect(metadata.manifest).toBe('/manifest.json');
    expect(viewport.themeColor).toBe('#0A0A0B');
    // Pinch-zoom must stay available for accessibility.
    expect(viewport.maximumScale).toBeGreaterThan(1);
  });
});
