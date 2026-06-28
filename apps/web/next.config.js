/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Enable gzip/brotli compression
  compress: true,
  // Power bundle splitting — more granular chunks for better caching
  experimental: {
    optimizePackageImports: ['@/components', '@/lib'],
  },
  async headers() {
    return [
      {
        source: '/:path*',
        headers: [
          { key: 'X-Content-Type-Options', value: 'nosniff' },
          { key: 'X-Frame-Options', value: 'DENY' },
          { key: 'Referrer-Policy', value: 'no-referrer' },
        ],
      },
      // Cache static assets aggressively
      {
        source: '/_next/static/:path*',
        headers: [
          { key: 'Cache-Control', value: 'public, max-age=31536000, immutable' },
        ],
      },
    ];
  },
};

module.exports = nextConfig;
