import js from "@eslint/js";
import tseslint from "@typescript-eslint/eslint-plugin";
import tsParser from "@typescript-eslint/parser";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";

export default [
  { ignores: ["dist/**", "node_modules/**"] },
  js.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        ecmaFeatures: { jsx: true },
        sourceType: "module",
      },
      globals: { ...globals.browser, ...globals.es2021 },
    },
    plugins: {
      "@typescript-eslint": tseslint,
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...tseslint.configs.recommended.rules,
      // Deliberately NOT eslint-plugin-react-hooks' `recommended` preset:
      // v7 folded in the React Compiler's static-analysis rules (purity,
      // immutability, set-state-in-effect, etc.), which assume a codebase
      // written to opt into the Compiler. This app targets plain React 18
      // and predates that ruleset; enabling it wholesale would demand a
      // structural rewrite of already-validated, working components for
      // rules unrelated to this phase's actual scope (reliability/
      // security). Keep the two rules that matter for a non-Compiler React
      // 18 app: rules-of-hooks (a real correctness rule — calling hooks
      // conditionally is a genuine bug) and exhaustive-deps (the
      // traditional advisory rule this codebase was already written
      // against, per its existing eslint-disable comments).
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      // TypeScript itself (via `tsc -b` in `npm run build`) already catches
      // genuinely undefined identifiers with full knowledge of ambient/DOM
      // lib types (RequestInit, etc.) and the automatic JSX runtime's
      // implicit React usage — ESLint's own core `no-undef` doesn't know
      // about either and produces false positives on both. This is
      // typescript-eslint's own documented recommendation for TS projects.
      "no-undef": "off",
      // The codebase's own convention (see api/client.ts, services layer)
      // deliberately types some payload/JSON fields as `any` at trust
      // boundaries (parsing API responses, JSON columns) — matching the
      // backend's Pydantic `dict[str, Any]` usage. Downgraded to a warning
      // rather than disabled outright, so new *unintentional* `any` usage
      // still shows up in review without failing the build on the
      // deliberate cases that already exist.
      "@typescript-eslint/no-explicit-any": "warn",
      "no-unused-vars": "off",
      "@typescript-eslint/no-unused-vars": ["warn", { argsIgnorePattern: "^_" }],
    },
  },
];
