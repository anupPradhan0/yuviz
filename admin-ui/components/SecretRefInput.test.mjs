// Run with `npm test`. SecretRefInput.tsx is transpiled on the fly so the real
// secretPayload() is under test, not a copy of it.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import ts from "typescript";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "SecretRefInput.tsx"), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
});
// Beside the source so `react` resolves from admin-ui/node_modules.
const dir = mkdtempSync(join(here, ".secretpayload-"));
writeFileSync(join(dir, "SecretRefInput.mjs"), outputText);
const { secretPayload } = await import(pathToFileURL(join(dir, "SecretRefInput.mjs")).href);
rmSync(dir, { recursive: true });

test('the "[stored]" mask goes back as a ref, never as a typed key', () => {
  assert.deepEqual(secretPayload("[stored]", "[stored]"), { api_key_ref: "[stored]" });
  assert.deepEqual(secretPayload("[stored]"), { api_key_ref: "[stored]" });
  assert.equal("api_key" in secretPayload("[stored]", "[stored]"), false);
});

test("an enc: ref still goes back as a ref", () => {
  assert.deepEqual(secretPayload("enc:abc", "enc:abc"), { api_key_ref: "enc:abc" });
});

test("a typed key is a key, a pointer is a ref, and nothing typed is untouched", () => {
  assert.deepEqual(secretPayload("AIza-typed", "[stored]"), { api_key: "AIza-typed" });
  assert.deepEqual(secretPayload("env:GEMINI_API_KEY"), { api_key_ref: "env:GEMINI_API_KEY" });
  assert.deepEqual(secretPayload(""), {});
  assert.deepEqual(secretPayload("", "[stored]"), { api_key_ref: "" });
});
