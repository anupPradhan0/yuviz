import assert from "node:assert/strict";
import test from "node:test";
import { replaceKnowledgeFile } from "../../lib/replaceKnowledgeFile.ts";

const original = { id: "old", kb_id: "folder", title: "prices.pdf", language: "en", tags: { team: "sales" }, usage_mode: "prompt" };
const file = { name: "new.pdf" };
function setup(statuses) {
  const events = [];
  let current = 0;
  const replacement = (status) => ({ id: "new", status, usage_mode: "auto" });
  const api = {
    uploadDocument: async (...args) => { events.push(["upload", ...args]); return replacement(statuses[current++]); },
    listDocuments: async () => { events.push(["poll"]); return [replacement(statuses[Math.min(current++, statuses.length - 1)])]; },
    updateDocument: async (...args) => { events.push(["update", ...args]); return replacement("ready"); },
    deleteDocument: async (...args) => { events.push(["delete", ...args]); },
  };
  return { api, events };
}
const wait = async () => {};

test("replacement waits for Ready, preserves metadata and reading preference, then removes the original", async () => {
  const { api, events } = setup(["pending", "processing", "ready"]);
  await replaceKnowledgeFile(original, file, api, { wait });
  assert.deepEqual(events.map(([event]) => event), ["upload", "poll", "poll", "update", "delete"]);
  assert.deepEqual(events[0].slice(1), ["folder", file, "prices.pdf", { language: "en", tags: original.tags }]);
  assert.deepEqual(events.at(-2), ["update", "new", { usage_mode: "prompt" }]);
  assert.deepEqual(events.at(-1), ["delete", "old"]);
});
for (const status of ["failed", "ready_degraded", "pending"]) {
  test(`a ${status} replacement preserves the original`, async () => {
    const { api, events } = setup(["pending", status]);
    await assert.rejects(replaceKnowledgeFile(original, file, api, { wait, checks: 2 }), /original file has been kept/);
    assert(!events.some(([event]) => event === "delete" || event === "update"));
  });
}
test("a polling or metadata failure preserves the original", async () => {
  for (const fail of ["listDocuments", "updateDocument"]) {
    const { api, events } = setup(["pending", "ready"]);
    api[fail] = async () => { throw new Error("offline"); };
    await assert.rejects(replaceKnowledgeFile(original, file, api, { wait }), /original file has been kept/);
    assert(!events.some(([event]) => event === "delete"));
  }
});
test("switching accounts while preparing never removes the original", async () => {
  const { api, events } = setup(["pending", "ready"]);
  let current = true;
  await assert.rejects(replaceKnowledgeFile(original, file, api, { wait: async () => { current = false; }, isCurrent: () => current }), /account changed/);
  assert(!events.some(([event]) => event === "delete"));
});
test("a failed original cleanup reports both copies without deleting the replacement", async () => {
  const { api } = setup(["ready"]);
  const deleted = [];
  api.deleteDocument = async (id) => { deleted.push(id); throw new Error("Could not remove old copy."); };
  await assert.rejects(replaceKnowledgeFile(original, file, api, { wait }), /removal of the original could not be confirmed/);
  assert.deepEqual(deleted, ["old"]);
});
