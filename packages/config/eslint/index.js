/**
 * Shared ESLint configuration for Aether Clinician Next.js workspaces.
 *
 * Layers, in order:
 *   1. `next/core-web-vitals` — Next.js App Router correctness + a11y + hooks rules.
 *   2. Project conventions from CLAUDE.md (no `any`, no class components, etc.).
 *   3. Test-file relaxations.
 *
 * Workspaces that are not Next.js apps take `@aether/config/eslint/conventions` instead,
 * which is layers 2 and 3 without the framework.
 */
const conventions = require('./conventions');

module.exports = {
  root: true,
  extends: ['next/core-web-vitals'],
  ...conventions,
};
