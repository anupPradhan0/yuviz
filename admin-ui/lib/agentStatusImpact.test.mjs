import assert from "node:assert/strict";
import test from "node:test";

import { pauseOutcomes, resumeMissing } from "./agentStatusImpact.ts";

const agent = { id: "a", slug: "support", name: "Support", status: "active" };
const fallback = { id: "b", slug: "backup", name: "Backup", status: "active" };
const number = { did: "+14155550100", agent_id: "a", fallback_agent_id: "b", status: "active" };

test("pausing names each active number and its actual next agent", () => {
  assert.deepEqual(pauseOutcomes(agent, [agent, fallback], [number]),
    ["+14155550100: calls go to Backup."]);
  assert.deepEqual(pauseOutcomes(agent, [agent, { ...fallback, status: "inactive" }], [number]),
    ["+14155550100: calls won't reach an agent."]);
  assert.deepEqual(pauseOutcomes(agent, [agent, fallback], [{ ...number, status: "inactive" }]), []);
});

test("pausing a backup explains whether the primary still answers", () => {
  assert.deepEqual(pauseOutcomes(fallback, [agent, fallback], [number]),
    ["+14155550100: continues to Support; this agent will no longer be its backup."]);
});

test("resuming checks inbound and outbound setup by direction", () => {
  const campaign = { agent_id: "a", caller_id: number.did };
  assert.equal(resumeMissing(agent, "inbound", [], []).length, 1);
  assert.deepEqual(resumeMissing(agent, "outbound", [number], [campaign]), []);
  assert.equal(resumeMissing(agent, "both", [], []).length, 2);
});
