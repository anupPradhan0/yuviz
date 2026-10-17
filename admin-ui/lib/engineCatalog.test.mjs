// Run with `npm test`. engineCatalog.ts is transpiled on the fly so the real helpers are under test.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import ts from "typescript";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "engineCatalog.ts"), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const dir = mkdtempSync(join(here, ".engine-catalog-"));
writeFileSync(join(dir, "engineCatalog.mjs"), outputText);
const { keptGreetings, languagesWithoutVoice } = await import(pathToFileURL(join(dir, "engineCatalog.mjs")).href);
rmSync(dir, { recursive: true });

const anyVoice = () => true;
const noFreeVoice = () => false;
const eleven = (model) => ({ engine: "elevenlabs", model, voice: "v1", extra: null });

test("a secondary language needs a voice unless the default voice speaks it", () => {
  assert.deepEqual(languagesWithoutVoice(["en", "hi"], {}, eleven("eleven_multilingual_v2"), anyVoice), []);
  assert.deepEqual(languagesWithoutVoice(["en", "hi"], {}, eleven("eleven_turbo_v2"), anyVoice), ["hi"]);
  assert.deepEqual(languagesWithoutVoice(["en", "hi"], {}, null, anyVoice), ["hi"]);
  assert.deepEqual(languagesWithoutVoice(["en", "hi"], { hi: "cfg" }, null, anyVoice), []);
});

test("a multilingual workspace default voice covers the second language of an agent with no voice of its own", () => {
  const workspaceDefault = eleven("eleven_multilingual_v2");
  assert.deepEqual(languagesWithoutVoice(["en", "hi"], {}, workspaceDefault, anyVoice), []);
});

test("a free default voice speaks only the languages its catalogue entry lists", () => {
  const kokoro = { engine: "kokoro", model: null, voice: "af_heart", extra: null };
  assert.deepEqual(languagesWithoutVoice(["en", "hi"], {}, kokoro, noFreeVoice), ["hi"]);
  assert.deepEqual(languagesWithoutVoice(["en", "hi"], {}, kokoro, anyVoice), []);
});

test("the greeting kept never includes the default language", () => {
  const stored = { en: "Hello", hi: "Namaste", fr: "Bonjour" };
  assert.deepEqual(keptGreetings(stored, ["en", "hi"]), { hi: "Namaste" });
  assert.deepEqual(keptGreetings(stored, ["hi", "en"]), { en: "Hello" });
  assert.deepEqual(keptGreetings(null, ["en", "hi"]), {});
});
