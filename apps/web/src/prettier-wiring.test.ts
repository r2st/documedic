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
const REPO_ROOT = path.resolve(APP_ROOT, '../..');

describe('prettier configuration', () => {
  it('resolves a source file to the shared config rather than Prettier defaults', async () => {
    const prettier = require('prettier');
    const shared = require('@aether/config/prettier');

    const resolved = await prettier.resolveConfig(path.join(APP_ROOT, 'src/lib/api.ts'));

    expect(resolved).toEqual(shared);
  });

  it('covers workspaces that have no config of their own', async () => {
    // The root config is what reaches packages/config and packages/shared-types — neither has
    // a `.prettierrc`, and neither has a `format` script for turbo to run, so a per-workspace
    // arrangement would leave exactly the two workspaces that had already drifted uncovered.
    const prettier = require('prettier');
    const shared = require('@aether/config/prettier');

    for (const relative of [
      'packages/shared-types/src/enums.ts',
      'packages/config/eslint/index.js',
      'packages/ui/src/index.ts',
    ]) {
      const resolved = await prettier.resolveConfig(path.join(REPO_ROOT, relative));
      expect(resolved, `${relative} should resolve the shared config`).toEqual(shared);
    }
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
