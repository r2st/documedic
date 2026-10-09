import type { MetadataRoute } from 'next';

export default function robots(): MetadataRoute.Robots {
  return {
    rules: [
      {
        userAgent: '*',
        allow: ['/', '/about', '/compare', '/tools/', '/blog/', '/embed', '/privacy', '/terms'],
        disallow: ['/api/', '/auth/', '/patients/'],
      },
    ],
    sitemap: 'https://med.doaide.com/sitemap.xml',
  };
}
