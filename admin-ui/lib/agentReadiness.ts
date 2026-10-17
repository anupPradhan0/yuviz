import type { Agent, PhoneNumber } from "./api";

// Keep in sync with the shipped chat templates in services/config/agent_templates.py.
const CHAT_TEMPLATE_IDS = new Set(["faq-support"]);

export function isChatAgent(agent: Agent): boolean {
  return agent.template_id != null && CHAT_TEMPLATE_IDS.has(agent.template_id);
}

/** Phone reachability is separate from the agent's active/paused switch. */
export function hasInboundNumber(agent: Agent, numbers: PhoneNumber[]): boolean {
  return numbers.some((number) => number.agent_id === agent.id && number.status === "active");
}
