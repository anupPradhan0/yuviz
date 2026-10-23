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
    assert(!events.some(([event, id]) => event === "delete" && id === "old"));
    assert(!events.some(([event]) => event === "update"));
    assert.deepEqual(events.filter(([event]) => event === "delete"), status === "failed" ? [] : [["delete", "new"]]);
  });
}
test("a polling or metadata failure preserves the original", async () => {
  for (const fail of ["listDocuments", "updateDocument"]) {
    const { api, events } = setup(["pending", "ready"]);
    api[fail] = async () => { throw new Error("offline"); };
    await assert.rejects(replaceKnowledgeFile(original, file, api, { wait }), /original file has been kept/);
    assert.deepEqual(events.filter(([event]) => event === "delete"), [["delete", "new"]]);
  }
});
test("switching accounts while preparing never removes the original", async () => {
  const { api, events } = setup(["pending", "ready"]);
  let current = true;
  await assert.rejects(replaceKnowledgeFile(original, file, api, { wait: async () => { current = false; }, isCurrent: () => current }), /account changed/);
  assert.deepEqual(events.filter(([event]) => event === "delete"), [["delete", "new"]]);
});
test("a failed original cleanup reports both copies without deleting the replacement", async () => {
  const { api } = setup(["ready"]);
  const deleted = [];
  api.deleteDocument = async (id) => { deleted.push(id); throw new Error("Could not remove old copy."); };
  await assert.rejects(replaceKnowledgeFile(original, file, api, { wait }), /removal of the original could not be confirmed/);
  assert.deepEqual(deleted, ["old"]);
});

test("a timeout discards the pending replacement and explains the rollback", async () => {
  const { api, events } = setup(["pending"]);
  await assert.rejects(replaceKnowledgeFile(original, file, api, { wait, checks: 2 }), /new file was discarded; try again/);
  assert.deepEqual(events.filter(([event]) => event === "delete"), [["delete", "new"]]);
});

test("a degraded replacement is discarded before it can remain alongside the original", async () => {
  const { api, events } = setup(["pending", "ready_degraded"]);
  await assert.rejects(replaceKnowledgeFile(original, file, api, { wait }), /new file was discarded/);
  assert.deepEqual(events.filter(([event]) => event === "delete"), [["delete", "new"]]);
});

test("a failed rollback warns that both copies may be available to agents", async () => {
  const { api } = setup(["ready_degraded"]);
  const deleted = [];
  api.deleteDocument = async (id) => { deleted.push(id); throw new Error("offline"); };
  await assert.rejects(replaceKnowledgeFile(original, file, api, { wait }), /Both copies may be available to agents/);
  assert.deepEqual(deleted, ["new"]);
});

test("an account switch while copying reading preferences discards only the replacement", async () => {
  const { api, events } = setup(["ready"]);
  let current = true;
  api.updateDocument = async () => { current = false; };
  await assert.rejects(replaceKnowledgeFile(original, file, api, { wait, isCurrent: () => current }), /new file was discarded/);
  assert.deepEqual(events.filter(([event]) => event === "delete"), [["delete", "new"]]);
});

test("an upload failure does not attempt to delete either file", async () => {
  const { api, events } = setup(["pending"]);
  api.uploadDocument = async () => { throw new Error("Upload failed"); };
  await assert.rejects(replaceKnowledgeFile(original, file, api, { wait }), /Upload failed/);
  assert.deepEqual(events, []);
});
