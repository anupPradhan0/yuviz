"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ApiError, PhoneNumber, listPhoneNumbers } from "@/lib/api";

const STATUS_CLS: Record<PhoneNumber["status"], string> = { active: "green", inactive: "gray", suspended: "amber" };

// Read-only: the numbers whose calls reach this agent. Edited on Telephony.
export function SipPanel({ tenantId, agentId }: { tenantId: string; agentId: string }) {
  const [numbers, setNumbers] = useState<PhoneNumber[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    listPhoneNumbers(tenantId)
      .then((n) => { if (!cancelled) setNumbers(n); })
      .catch((e) => { if (!cancelled) setError(e instanceof ApiError ? e.detail : String(e)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [tenantId, agentId]);

  if (loading) return <div className="empty-state">Loading…</div>;

  const reachable = numbers
    .filter((n) => n.agent_id === agentId || n.fallback_agent_id === agentId)
    .sort((a, b) => Number(b.agent_id === agentId) - Number(a.agent_id === agentId) || a.did.localeCompare(b.did));

  return (
    <div className="card">
      <div className="card-hdr">
        <div className="card-title">Reachable on</div>
        <div className="card-sub">numbers whose calls reach this agent</div>
        <Link href="/telephony" className="btn btn-ghost btn-sm" style={{ marginLeft: "auto" }}>Manage in Telephony</Link>
      </div>

      {error && <div className="error-banner">{error}</div>}

      {reachable.length === 0 ? (
        <div className="empty-state">
          No number routes to this agent yet. In Telephony, open a configuration and add a number, or edit one and pick this agent.
        </div>
      ) : (
        reachable.map((n) => {
          const configKey = n.carrier_id
            ? `carrier:${n.carrier_id}`
            : n.telephony_config_id
              ? `telephony_config:${n.telephony_config_id}`
              : null;
          const isFallback = n.agent_id !== agentId;
          const syncFailed = n.provider_sync?.ok === false;
          return (
            <div key={n.id} className="kb-row">
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 500, fontFamily: "var(--mono)" }}>{n.did}</div>
                <div style={{ fontSize: ".7rem", color: syncFailed ? "var(--red)" : "var(--text-3)" }}>
                  {isFallback ? "Fallback: answers when the number's main agent can't" : "Inbound agent"}
                  {n.region ? ` · ${n.region}` : ""}
                  {syncFailed ? ` · provider isn't sending calls here: ${n.provider_sync?.message ?? "unknown error"}` : ""}
                </div>
              </div>
              <span className={`badge ${STATUS_CLS[n.status]}`}>{n.status}</span>
              {configKey ? (
                <Link href={`/telephony?config=${configKey}`} className="btn btn-ghost btn-sm">View configuration</Link>
              ) : (
                <Link href="/telephony" className="badge amber">No configuration</Link>
              )}
            </div>
          );
        })
      )}
    </div>
  );
}
