import { configDefaults, defineConfig } from "vitest/config";

// Unit tests only (lib/**/*.test.ts). The page-render suite under e2e/ needs a
// running pmi-web and has its own config: `npm run test:render`.
export default defineConfig({
  test: {
    exclude: [...configDefaults.exclude, "e2e/**", ".next/**"],
  },
});
