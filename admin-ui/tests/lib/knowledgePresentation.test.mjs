import assert from "node:assert/strict";
import test from "node:test";
import { mergeKnowledgeFolderRows } from "../../lib/knowledgePresentation.ts";

const row = (kb_id, id, status = "ready") => ({ kb_id, id, status });
const original = [row("A", "a1"), row("B", "b1", "pending"), row("B", "b2"), row("C", "c1")];

test("polling the middle folder keeps it between its neighbors across repeated updates", () => {
  let current = original;
  for (const status of ["processing", "processing", "ready"]) {
    const updated = [row("B", "b1", status), row("B", "b2")];
    current = mergeKnowledgeFolderRows(current, new Map([["B", updated]]));
    assert.deepEqual(current.map((doc) => doc.id), ["a1", "b1", "b2", "c1"]);
    assert.equal(current[1].status, status);
    assert.equal(current[0], original[0]);
    assert.equal(current.at(-1), original.at(-1));
  }
});

test("new and deleted files stay within their folder's existing position", () => {
  const updated = [row("B", "b2"), row("B", "b3")];
  const result = mergeKnowledgeFolderRows(original, new Map([["B", updated]]));
  assert.deepEqual(result, [original[0], ...updated, original[3]]);
  assert.deepEqual(mergeKnowledgeFolderRows(original, new Map([["B", []]])), [original[0], original[3]]);
});

test("multiple folder updates keep the original folder order regardless of response order", () => {
  const updatedA = row("A", "a1", "ready");
  const updatedC = row("C", "c1", "failed");
  const result = mergeKnowledgeFolderRows(original, new Map([["C", [updatedC]], ["A", [updatedA]]]));
  assert.deepEqual(result, [updatedA, original[1], original[2], updatedC]);
  // A failed poll supplies no update for that folder; retain its existing rows.
  assert.equal(result[1], original[1]);
  assert.equal(result[2], original[2]);
  assert.deepEqual(mergeKnowledgeFolderRows(original, new Map()), original);
});
