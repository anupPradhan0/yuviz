import assert from "node:assert/strict";
import test from "node:test";

import { hasInboundNumber, readinessItems } from "./agentReadiness.ts";

const agent = { id: "agent-1", direction: "inbound" };
const number = { agent_id: "agent-1", did: "+14155550100", status: "active" };

test("inbound needs an active number routed to the agent", () => {
  assert.equal(hasInboundNumber(agent, [{ ...number, status: "inactive" }]), false);
  assert.equal(readinessItems(agent, [{ ...number, agent_id: "other" }], []).length, 1);
  assert.deepEqual(readinessItems(agent, [number], []), []);
});

test("outbound needs a campaign with an owned active caller ID", () => {
  const outbound = { ...agent, direction: "outbound" };
  const campaign = { agent_id: agent.id, caller_id: number.did };
  assert.equal(readinessItems(outbound, [], [campaign]).length, 1);
  assert.equal(readinessItems(outbound, [number], [{ ...campaign, caller_id: null }]).length, 1);
  assert.deepEqual(readinessItems(outbound, [number], [campaign]), []);
});

test("both directions require both routes", () => {
  const both = { ...agent, direction: "both" };
  assert.equal(readinessItems(both, [], []).length, 2);
});
