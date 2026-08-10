/** Web app ESLint config — see packages/config/eslint for the shared baseline. */
module.exports = {
  ...require('@aether/config/eslint'),
  settings: {
    next: { rootDir: __dirname },
  },
};
