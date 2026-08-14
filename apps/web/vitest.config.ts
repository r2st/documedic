import path from 'node:path';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    globals: true,
    include: ['src/**/*.test.{ts,tsx}'],
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,tsx}'],
      // types.ts is declarations only; globals.css has no executable lines.
      exclude: ['src/**/*.test.{ts,tsx}', 'src/lib/types.ts'],
      reporter: ['text', 'lcov'],
      // Ratchet: raise these as coverage improves, never lower them.
      //
      // Branches reached 100% once the accessibility suite exercised the remaining guards
      // (the `!sessionId` / `!docId` early returns and the unknown-Metric-label arm), so the
      // bar now sits level with the rest.
      thresholds: {
        statements: 100,
        branches: 100,
        functions: 100,
        lines: 100,
      },
    },
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
      // Resolved to source rather than through node_modules so the shared components are
      // transformed by this app's React plugin like any other .tsx file under test.
      '@aether/ui': path.resolve(__dirname, '../../packages/ui/src/index.ts'),
    },
  },
});
