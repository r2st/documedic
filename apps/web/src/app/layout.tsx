import type { Metadata, Viewport } from 'next';
import Script from 'next/script';
import './globals.css';
import { AuthProvider } from '@/lib/auth';
import { OfflineBanner } from '@/components/Banners';

export const metadata: Metadata = {
  title: 'DoAide Med',
  description: 'Your digital robot for clinical decisions',
  icons: {
    icon: [
      { url: '/favicon.svg', type: 'image/svg+xml' },
      { url: '/favicon.ico', sizes: '16x16 32x32' },
      { url: '/icon-32.png', sizes: '32x32', type: 'image/png' },
      { url: '/icon-192.png', sizes: '192x192', type: 'image/png' },
      { url: '/icon-512.png', sizes: '512x512', type: 'image/png' },
    ],
    apple: '/apple-touch-icon.png',
  },
  manifest: '/manifest.json',
  openGraph: {
    title: 'DoAide Med — AI Clinical Decision Support',
    description:
      'AI-powered clinical decision support system for healthcare professionals.',
    url: 'https://med.doaide.com',
    siteName: 'DoAide',
    type: 'website',
    images: [
      {
        url: 'https://med.doaide.com/og-image.png',
        width: 1200,
        height: 630,
        alt: 'DoAide Med — AI Clinical Decision Support',
      },
    ],
  },
  twitter: {
    card: 'summary',
    title: 'DoAide Med — AI Clinical Decision Support',
    description: 'AI-powered clinical decision support for healthcare.',
  },
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  maximumScale: 5,
  themeColor: '#0A0A0B',
  colorScheme: 'dark',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';
  const apiOrigin = (() => {
    try {
      return new URL(apiUrl).origin;
    } catch {
      return '';
    }
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
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
              <Script
          defer
          src="https://analytics.doaide.com/script.js"
          data-website-id="a5ae8d1f-1100-447f-bdf9-dd7497c4d1a0"
          strategy="afterInteractive"
        />
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{
            __html: JSON.stringify({
              '@context': 'https://schema.org',
              '@type': 'SoftwareApplication',
              name: 'DoAide Med',
              description:
                'AI-powered clinical decision support system for healthcare professionals.',
              url: 'https://med.doaide.com',
              applicationCategory: 'HealthApplication',
              operatingSystem: 'Web',
              offers: { '@type': 'Offer', price: '0', priceCurrency: 'USD' },
              author: {
                '@type': 'Organization',
                name: 'Apprend Technologies',
                url: 'https://doaide.com',
              },
            }),
          }}
        />
      </head>
      <body>
        <OfflineBanner />
        <AuthProvider>{children}</AuthProvider>
      </body>
    </html>
  );
}
