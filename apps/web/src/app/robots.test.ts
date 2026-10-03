import { describe, expect, it } from 'vitest';
import robots from './robots';

describe('robots', () => {
  it('returns valid robots config', () => {
    const config = robots();
    expect(config.sitemap).toBe('https://med.doaide.com/sitemap.xml');
    expect(config.rules).toBeDefined();
    const rule = Array.isArray(config.rules) ? config.rules[0] : config.rules;
    expect(rule.userAgent).toBe('*');
  });
});
