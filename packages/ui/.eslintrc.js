/**
 * Component library ESLint config.
 *
 * Takes the project conventions without the Next.js layer — this package is a plain React
 * library with no pages or app directory, and `next/core-web-vitals` reports that as missing
 * on every run.
 */
module.exports = {
  root: true,
  // `jsx-runtime` switches off the react-in-scope rules: this package compiles with the
  // automatic JSX transform (tsconfig `jsx: "react-jsx"`), so importing React is not needed.
  extends: [
    'plugin:react/recommended',
    'plugin:react/jsx-runtime',
    'plugin:react-hooks/recommended',
  ],
  settings: { react: { version: 'detect' } },
  ...require('@aether/config/eslint/conventions'),
};
