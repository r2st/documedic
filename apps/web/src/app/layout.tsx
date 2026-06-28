import type { Metadata, Viewport } from 'next';
import './globals.css';
import { AuthProvider } from '@/lib/auth';
import { DemoBanner, OfflineBanner } from '@/components/Banners';

export const metadata: Metadata = {
  title: 'Aether Clinician',
  description: 'Clinician-facing diagnostic & management decision-support system',
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  maximumScale: 5,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';
  const apiOrigin = (() => {
    try { return new URL(apiUrl).origin; } catch { return ''; }
  })();

  return (
    <html lang="en">
      <head>
        {apiOrigin && (
          <>
            <link rel="preconnect" href={apiOrigin} />
            <link rel="dns-prefetch" href={apiOrigin} />
          </>
        )}
      </head>
      <body>
        <DemoBanner />
        <OfflineBanner />
        <AuthProvider>{children}</AuthProvider>
      </body>
    </html>
  );
}
