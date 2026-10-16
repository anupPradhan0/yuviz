"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Agent, ApiError, Campaign, PhoneNumber, listAgents, listCampaigns, listPhoneNumbers } from "@/lib/api";
import { agentDirection } from "@/lib/agentTemplates";
import { pauseOutcomes, resumeMissing } from "@/lib/agentStatusImpact";
import { Modal } from "@/components/Modal";

export function AgentStatusDialog({ agent, tenantSlug, onClose, onConfirm }: {
  agent: Agent | null;
  tenantSlug: string;
  onClose: () => void;
  onConfirm: (agent: Agent) => Promise<void>;
}) {
  const [numbers, setNumbers] = useState<PhoneNumber[] | null>(null);
  const [agents, setAgents] = useState<Agent[] | null>(null);
  const [campaigns, setCampaigns] = useState<Campaign[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!agent) return;
    let cancelled = false;
    // Fetch at the time of the action so the confirmation reflects current routing.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    setError(null);
    Promise.allSettled([
      listPhoneNumbers(agent.tenant_id), listAgents(tenantSlug), listCampaigns(agent.tenant_id),
    ]).then(([phoneResult, agentResult, campaignResult]) => {
      if (cancelled) return;
      setNumbers(phoneResult.status === "fulfilled" ? phoneResult.value : null);
      setAgents(agentResult.status === "fulfilled" ? agentResult.value : null);
      setCampaigns(campaignResult.status === "fulfilled" ? campaignResult.value : null);
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [agent, tenantSlug]);

  if (!agent) return null;
  const pausing = agent.status === "active";
  const activatingDraft = !pausing && !agent.activated_at;
  const action = pausing ? "Pause" : activatingDraft ? "Go live with" : "Resume";
  const storedDirection = (agent as Agent & { direction?: "inbound" | "outbound" | "both" }).direction;
  const direction = storedDirection ?? agentDirection(agent) ?? (campaigns?.some((campaign) => campaign.agent_id === agent.id) ? "outbound" : "inbound");
  const outcomes = numbers && agents ? pauseOutcomes(agent, agents, numbers) : null;
  const missing = numbers && campaigns ? resumeMissing(agent, direction, numbers, campaigns) : null;
  const canConfirm = !loading && !busy && (!pausing || outcomes !== null);

  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      await onConfirm(agent);
      onClose();
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.detail : String(failure));
    } finally {
      setBusy(false);
    }
  };

  return <Modal open title={`${action} "${agent.name}"?`} onClose={onClose}
    footer={<>
      <button className="btn btn-ghost btn-sm" onClick={onClose} disabled={busy}>Cancel</button>
      <button className="btn btn-primary btn-sm" onClick={confirm} disabled={!canConfirm}>
        {busy ? "Saving…" : pausing ? "Pause agent" : activatingDraft
          ? missing?.length ? "Go live anyway" : "Go live"
          : missing?.length ? "Resume anyway" : "Resume agent"}
      </button>
    </>}>
    {loading ? <p>Checking phone routing…</p> : pausing ? (
      outcomes === null ? <p>Could not check phone routing. Try again before pausing.</p> :
      outcomes.length ? <>
        <p>Pausing removes this agent from call routing:</p>
        <ul className="agent-status-impact">{outcomes.map((line) => <li key={line}>{line}</li>)}</ul>
      </> : <p>This agent has no active phone numbers assigned. Browser and outbound calls will stop using it.</p>
    ) : missing === null ? <p>Phone setup could not be checked. Verify the agent’s numbers and campaigns before resuming.</p> :
      missing.length ? <>
        <p>This agent can be tested in the browser, but these items are needed for phone calls:</p>
        <ul className="agent-status-impact">{missing.map((line) => <li key={line}>{line}</li>)}</ul>
        <p><Link href="/telephony">Manage numbers</Link> · <Link href="/campaigns">Manage campaigns</Link></p>
      </> : <p>Phone setup is ready for this agent.</p>}
    {error && <div className="error-banner">{error}</div>}
  </Modal>;
}
