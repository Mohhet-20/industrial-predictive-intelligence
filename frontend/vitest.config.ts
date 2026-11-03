import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Deliberately separate from vite.config.ts (rather than adding a `test`
// block there): vite.config.ts is type-checked by tsconfig.node.json as
// part of `tsc -b`, and mixing vitest's config types in would pull test
// runner types into that project's build. This mirrors vite.config.ts's
// actual settings (same alias, same react plugin) so app code under test
// resolves imports identically to the real build.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": "/src",
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    globals: false,
    css: false,
  },
});
