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
      thresholds: {
        statements: 90,
        branches: 82,
        functions: 88,
        lines: 90,
      },
    },
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
});
