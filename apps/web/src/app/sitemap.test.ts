import { describe, expect, it } from 'vitest';
import sitemap from './sitemap';

describe('sitemap', () => {
  it('returns all expected URLs', () => {
    const entries = sitemap();
    const urls = entries.map((e) => e.url);
    expect(urls).toContain('https://med.doaide.com');
    expect(urls).toContain('https://med.doaide.com/tools');
    expect(urls).toContain('https://med.doaide.com/tools/bmi-calculator');
    expect(urls).toContain('https://med.doaide.com/tools/drug-interaction-checker');
    expect(urls).toContain('https://med.doaide.com/tools/symptom-triage');
    expect(urls).toContain('https://med.doaide.com/blog');
    expect(urls).toContain('https://med.doaide.com/embed');
    expect(entries.length).toBeGreaterThanOrEqual(10);
  });
});
