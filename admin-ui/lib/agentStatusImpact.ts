import type { Agent, Campaign, PhoneNumber } from "./api";

export function pauseOutcomes(agent: Agent, agents: Agent[], numbers: PhoneNumber[]): string[] {
  const byId = new Map(agents.map((row) => [row.id, row]));
  const defaultAgent = agents.find((row) => row.slug === "default" && row.status === "active" && row.id !== agent.id);
  return numbers.filter((number) => number.status === "active" &&
    (number.agent_id === agent.id || number.fallback_agent_id === agent.id)).map((number) => {
    if (number.agent_id !== agent.id) {
      const primary = number.agent_id ? byId.get(number.agent_id) : null;
      return primary?.status === "active"
        ? `${number.did}: continues to ${primary.name}; this agent will no longer be its backup.`
        : `${number.did}: loses its backup agent${defaultAgent ? `; calls use ${defaultAgent.name}.` : "; calls won't reach an agent."}`;
    }
    const fallback = number.fallback_agent_id ? byId.get(number.fallback_agent_id) : null;
    const next = fallback?.status === "active" ? fallback : defaultAgent;
    return next
      ? `${number.did}: calls go to ${next.name}.`
      : `${number.did}: calls won't reach an agent.`;
  });
}

export function resumeMissing(
  agent: Agent, direction: "inbound" | "outbound" | "both",
  numbers: PhoneNumber[], campaigns: Campaign[],
): string[] {
  const missing: string[] = [];
  if (direction !== "outbound" && !numbers.some((number) => number.status === "active" && number.agent_id === agent.id)) {
    missing.push("No active phone number is assigned to this agent.");
  }
  if (direction !== "inbound") {
    const activeDids = new Set(numbers.filter((number) => number.status === "active").map((number) => number.did));
    if (!campaigns.some((campaign) => campaign.agent_id === agent.id && !!campaign.caller_id && activeDids.has(campaign.caller_id))) {
      missing.push("No campaign has an active business number to use as caller ID.");
    }
  }
  return missing;
}
