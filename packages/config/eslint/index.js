/**
 * Shared ESLint configuration for Aether Clinician TypeScript/React workspaces.
 *
 * Layers, in order:
 *   1. `next/core-web-vitals` — Next.js App Router correctness + a11y + hooks rules.
 *   2. Project conventions from CLAUDE.md (no `any`, no class components, etc.).
 *   3. Test-file relaxations.
 */
module.exports = {
  root: true,
  extends: ['next/core-web-vitals'],
  parser: '@typescript-eslint/parser',
  parserOptions: {
    ecmaVersion: 2022,
    sourceType: 'module',
    ecmaFeatures: { jsx: true },
  },
  plugins: ['@typescript-eslint'],
  rules: {
    // --- CLAUDE.md: "No `any` types in TypeScript — use `unknown` and narrow" ---
    '@typescript-eslint/no-explicit-any': 'error',

    // --- CLAUDE.md: "Functional components only; no class components" ---
    'react/prefer-stateless-function': 'off', // covered by no-restricted-syntax below
    'no-restricted-syntax': [
      'error',
      {
        selector: 'ClassDeclaration[superClass.name=/^(React\\.)?(Component|PureComponent)$/]',
        message: 'Functional components only — no class components (see CLAUDE.md).',
      },
      {
        selector:
          'ClassDeclaration[superClass.object.name="React"][superClass.property.name=/^(Component|PureComponent)$/]',
        message: 'Functional components only — no class components (see CLAUDE.md).',
      },
    ],

    // --- General hygiene ---
    '@typescript-eslint/no-unused-vars': [
      'error',
      { argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrorsIgnorePattern: '^_' },
    ],
    'no-unused-vars': 'off', // superseded by the TS-aware rule above
    eqeqeq: ['error', 'smart'],
    'no-console': ['warn', { allow: ['warn', 'error'] }],
    'prefer-const': 'error',
    'no-var': 'error',
  },
  overrides: [
    {
      files: ['**/*.test.ts', '**/*.test.tsx', '**/vitest.setup.ts', '**/*.config.{js,ts}'],
      rules: {
        '@typescript-eslint/no-explicit-any': 'off',
        'no-console': 'off',
      },
    },
  ],
};
