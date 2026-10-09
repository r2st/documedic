import type { MetadataRoute } from 'next';

export default function sitemap(): MetadataRoute.Sitemap {
  const base = 'https://med.doaide.com';
  return [
    { url: base, lastModified: new Date(), changeFrequency: 'weekly', priority: 1 },
    { url: `${base}/about`, lastModified: new Date(), changeFrequency: 'monthly', priority: 0.9 },
    { url: `${base}/compare`, lastModified: new Date(), changeFrequency: 'monthly', priority: 0.9 },
    { url: `${base}/tools`, lastModified: new Date(), changeFrequency: 'monthly', priority: 0.9 },
    { url: `${base}/tools/bmi-calculator`, lastModified: new Date(), changeFrequency: 'monthly', priority: 0.8 },
    { url: `${base}/tools/drug-interaction-checker`, lastModified: new Date(), changeFrequency: 'monthly', priority: 0.8 },
    { url: `${base}/tools/symptom-triage`, lastModified: new Date(), changeFrequency: 'monthly', priority: 0.8 },
    { url: `${base}/blog`, lastModified: new Date(), changeFrequency: 'weekly', priority: 0.9 },
    { url: `${base}/blog/ai-clinical-decision-support-reduces-diagnostic-errors`, lastModified: new Date('2026-09-15'), changeFrequency: 'monthly', priority: 0.7 },
    { url: `${base}/blog/5-ways-cdss-improves-patient-safety`, lastModified: new Date('2026-09-22'), changeFrequency: 'monthly', priority: 0.7 },
    { url: `${base}/blog/future-of-ai-in-healthcare`, lastModified: new Date('2026-09-29'), changeFrequency: 'monthly', priority: 0.7 },
    { url: `${base}/blog/drug-interaction-checking-ai-patient-safety`, lastModified: new Date('2026-10-06'), changeFrequency: 'monthly', priority: 0.7 },
    { url: `${base}/blog/medical-ai-tools-indian-doctors-guide`, lastModified: new Date('2026-10-08'), changeFrequency: 'monthly', priority: 0.7 },
    { url: `${base}/blog/clinical-decision-support-rural-healthcare-india`, lastModified: new Date('2026-10-10'), changeFrequency: 'monthly', priority: 0.7 },
    { url: `${base}/embed`, lastModified: new Date(), changeFrequency: 'monthly', priority: 0.7 },
    { url: `${base}/privacy`, lastModified: new Date(), changeFrequency: 'yearly', priority: 0.4 },
    { url: `${base}/terms`, lastModified: new Date(), changeFrequency: 'yearly', priority: 0.4 },
  ];
}
