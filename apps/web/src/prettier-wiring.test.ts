// The formatter's wiring, not its output.
//
// `packages/config/prettier` existed for a long time with nothing in the repo resolving it:
// every workspace formatted to Prettier's built-in defaults instead, so the shared 100-column
// / single-quote settings were a file nobody read. A `.prettierrc` that silently stops
// resolving degrades the same way — Prettier does not fail when a config is missing, it just
// falls back to defaults and reformats the tree on the next `--write`. These assertions are
// what turn that back into a failure.

import { createRequire } from 'node:module';
import path from 'node:path';

import { describe, expect, it } from 'vitest';

const require = createRequire(import.meta.url);
const APP_ROOT = path.resolve(__dirname, '..');

describe('prettier configuration', () => {
  it('resolves a source file to the shared config rather than Prettier defaults', async () => {
    const prettier = require('prettier');
    const shared = require('@aether/config/prettier');

    const resolved = await prettier.resolveConfig(path.join(APP_ROOT, 'src/lib/api.ts'));

    expect(resolved).toEqual(shared);
  });

  it('pins the settings the tree is formatted to', () => {
    // Spelled out here so a change to the shared config is a deliberate edit in two places
    // rather than a silent reformat of every file in the app.
    expect(require('@aether/config/prettier')).toEqual({
      singleQuote: true,
      trailingComma: 'all',
      printWidth: 100,
      semi: true,
    });
  });

  it('does not format build output or generated coverage reports', async () => {
    const prettier = require('prettier');
    const ignored = ['.next/build.js', 'coverage/lcov-report/base.css', 'next-env.d.ts'];

    for (const relative of ignored) {
      const info = await prettier.getFileInfo(path.join(APP_ROOT, relative), {
        ignorePath: path.join(APP_ROOT, '.prettierignore'),
      });
      expect(info.ignored, `${relative} should be ignored`).toBe(true);
    }
  });

  it('does format the app source it is meant to govern', async () => {
    const prettier = require('prettier');
    const info = await prettier.getFileInfo(path.join(APP_ROOT, 'src/components/ui.tsx'), {
      ignorePath: path.join(APP_ROOT, '.prettierignore'),
    });

    expect(info.ignored).toBe(false);
    expect(info.inferredParser).toBe('typescript');
  });
});
