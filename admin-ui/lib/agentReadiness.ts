import type { Agent, Campaign, PhoneNumber } from "./api";

export interface ReadinessItem {
  text: string;
  href: string;
  action: string;
}

// Keep in sync with the shipped chat templates in services/config/agent_templates.py.
const CHAT_TEMPLATE_IDS = new Set(["faq-support"]);

export function isChatAgent(agent: Agent): boolean {
  return agent.template_id != null && CHAT_TEMPLATE_IDS.has(agent.template_id);
}

// Mirror libs/telephony_sdk/did_format.normalize_did: phone-shaped values are
// digits-only; SIP URIs and named extensions retain their exact spelling.
function normalizeDid(raw: string): string {
  const did = raw.replace(/^[ \t\n\r\f\v]+|[ \t\n\r\f\v]+$/g, "");
  return /^\+?[0-9 ().-]*[0-9][0-9 ().-]*$/.test(did) ? did.replace(/[^0-9]/g, "") : did;
}

/** Phone reachability is separate from the agent's active/paused switch. */
export function hasInboundNumber(agent: Agent, numbers: PhoneNumber[]): boolean {
  return numbers.some((number) => number.agent_id === agent.id && number.status === "active");
}

export function readinessItems(agent: Agent, numbers: PhoneNumber[], campaigns: Campaign[] | null): ReadinessItem[] {
  const missing: ReadinessItem[] = [];
  if (!isChatAgent(agent) && agent.direction !== "outbound" && !hasInboundNumber(agent, numbers)) {
    missing.push({ text: "No active phone number is assigned to this agent", href: "/telephony", action: "Assign a number" });
  }
  if (!isChatAgent(agent) && agent.direction !== "inbound") {
    if (campaigns === null) {
      missing.push({
        text: "Could not check outbound campaign setup",
        href: "/campaigns", action: "Check campaigns",
      });
    } else {
      const ownedDids = new Set(numbers.filter((number) => number.status === "active").map((number) => normalizeDid(number.did)));
      const dialable = campaigns.some((campaign) =>
        campaign.agent_id === agent.id && !!campaign.caller_id && ownedDids.has(normalizeDid(campaign.caller_id)),
      );
      if (!dialable) {
        missing.push({
          text: "No campaign has an active business number to use as caller ID",
          href: "/campaigns", action: "Set up outbound calls",
        });
      }
    }
  }
  return missing;
}
