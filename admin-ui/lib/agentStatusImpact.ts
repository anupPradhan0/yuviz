import type { Agent, Campaign, PhoneNumber } from "./api";

export function pauseOutcomes(agent: Agent, agents: Agent[], numbers: PhoneNumber[]): string[] {
  const byId = new Map(agents.map((row) => [row.id, row]));
  const defaultAgent = agents.find((row) => row.slug === "default" && row.status === "active");
  const active = (id: string | null, pausedId?: string): Agent | null => {
    const row = id ? byId.get(id) : null;
    return row?.status === "active" && row.id !== pausedId ? row : null;
  };
  const route = (number: PhoneNumber, pausedId?: string) =>
    active(number.agent_id, pausedId)
    ?? active(number.fallback_agent_id, pausedId)
    ?? (defaultAgent?.id !== pausedId ? defaultAgent : null);

  return numbers.flatMap((number) => {
    if (number.status !== "active" || number.provider_sync?.ok === false) return [];
    const current = route(number);
    const assigned = number.agent_id === agent.id || number.fallback_agent_id === agent.id;
    if (current?.id !== agent.id && !assigned) return [];
    const next = route(number, agent.id);
    if (current && current.id !== agent.id && next?.id === current.id) {
      return [`${number.did}: continues to ${current.name}; this agent will no longer be its backup.`];
    }
    return [next
      ? `${number.did}: calls go to ${next.name}.`
      : `${number.did}: calls won't reach an agent.`];
  });
}

export function resumeMissing(
  agent: Agent, direction: "inbound" | "outbound" | "both",
  numbers: PhoneNumber[], campaigns: Campaign[],
): string[] {
  const missing: string[] = [];
  const wired = numbers.filter((number) => number.status === "active" && number.provider_sync?.ok !== false);
  if (direction !== "outbound" && !wired.some((number) => number.agent_id === agent.id)) {
    missing.push("No active phone number is assigned to this agent.");
  }
  if (direction !== "inbound") {
    const activeDids = new Set(wired.map((number) => number.did));
    if (!campaigns.some((campaign) => campaign.agent_id === agent.id && !!campaign.caller_id && activeDids.has(campaign.caller_id))) {
      missing.push("No campaign has an active business number to use as caller ID.");
    }
  }
  return missing;
}
