import assert from "node:assert/strict";
import test from "node:test";

import { hasInboundNumber, isChatAgent } from "./agentReadiness.ts";

const agent = { id: "agent-1", template_id: null };
const number = { agent_id: agent.id, status: "active" };

test("only active numbers routed to the agent count for inbound reachability", () => {
  assert.equal(hasInboundNumber(agent, [number]), true);
  assert.equal(hasInboundNumber(agent, [{ ...number, status: "inactive" }]), false);
  assert.equal(hasInboundNumber(agent, [{ ...number, status: "suspended" }]), false);
  assert.equal(hasInboundNumber(agent, [{ ...number, agent_id: "other" }]), false);
});

test("the shipped chat template does not require phone reachability", () => {
  assert.equal(isChatAgent({ ...agent, template_id: "faq-support" }), true);
  assert.equal(isChatAgent(agent), false);
});
