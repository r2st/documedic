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
      // Branches sit at 99.58%: the three uncovered arms are guards that cannot be reached
      // through the rendered UI (an unknown Metric label, and the `!sessionId` / `!docId`
      // early returns, both behind affordances that only exist once the value is set). They
      // are kept as defence in depth, so the branch bar stays a point below the rest.
      thresholds: {
        statements: 100,
        branches: 99,
        functions: 100,
        lines: 100,
      },
    },
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
});
