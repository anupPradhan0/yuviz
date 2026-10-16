import type { Agent, Campaign, PhoneNumber } from "./api";

export interface ReadinessItem {
  text: string;
  href: string;
  action: string;
}

/** Phone reachability is separate from the agent's active/paused switch. */
export function hasInboundNumber(agent: Agent, numbers: PhoneNumber[]): boolean {
  return numbers.some((number) => number.agent_id === agent.id && number.status === "active");
}

export function readinessItems(agent: Agent, numbers: PhoneNumber[], campaigns: Campaign[]): ReadinessItem[] {
  const missing: ReadinessItem[] = [];
  if (agent.direction !== "outbound" && !hasInboundNumber(agent, numbers)) {
    missing.push({ text: "No active phone number is assigned to this agent", href: "/telephony", action: "Assign a number" });
  }
  if (agent.direction !== "inbound") {
    const ownedDids = new Set(numbers.filter((number) => number.status === "active").map((number) => number.did));
    const dialable = campaigns.some((campaign) =>
      campaign.agent_id === agent.id && !!campaign.caller_id && ownedDids.has(campaign.caller_id),
    );
    if (!dialable) {
      missing.push({
        text: "No campaign has an active business number to use as caller ID",
        href: "/campaigns", action: "Set up outbound calls",
      });
    }
  }
  return missing;
}
