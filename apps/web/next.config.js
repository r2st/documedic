/** @type {import('next').NextConfig} */
const { securityHeaders } = require('./security-headers');

const nextConfig = {
  reactStrictMode: true,
  // @aether/ui is published as TypeScript source rather than a build artifact, so Next
  // compiles it alongside the app instead of expecting pre-built JavaScript.
  transpilePackages: ['@aether/ui'],
  // Enable gzip/brotli compression
  compress: true,
  // The framework and its version are not something a clinical deployment should announce; it
  // tells an attacker which framework advisories to try and buys nothing.
  poweredByHeader: false,
  // Power bundle splitting — more granular chunks for better caching
  experimental: {
    optimizePackageImports: ['@/components', '@/lib'],
  },
  async headers() {
    return [
      {
        source: '/:path*',
        headers: securityHeaders(),
      },
      // Cache static assets aggressively
      {
        source: '/_next/static/:path*',
        headers: [{ key: 'Cache-Control', value: 'public, max-age=31536000, immutable' }],
      },
    ];
  },
};

module.exports = nextConfig;
