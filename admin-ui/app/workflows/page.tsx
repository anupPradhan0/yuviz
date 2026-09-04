"use client";

// The agents list. An agent IS its conversation flow (2026-08-30) — creating
// one here drops you straight onto its canvas, and everything else about it
// (voice, model, tools, number) lives at ./[tenant]/[agent]/settings.
//
// The URLs stay /workflows/* while the labels say "agent": the flow is the
// thing you edit, the agent is the thing you own. Same split Dograh uses.

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  AgentWithTenant,
  ApiError,
  Tenant,
  createAgent,
  listAllAgents,
  listTenants,
} from "@/lib/api";
import { Modal } from "@/components/Modal";

type FlowState = "live" | "draft" | "none";

function flowState(a: AgentWithTenant): FlowState {
  if (a.workflow) return "live";
  if (a.workflow_draft) return "draft";
  return "none";
}

const STATE_LABEL: Record<FlowState, string> = {
  live: "Live",
  draft: "Draft — not published",
  none: "Single prompt",
};

const STATE_BADGE: Record<FlowState, string> = {
  live: "green",
  draft: "amber",
  none: "gray",
};

function stepCount(a: AgentWithTenant): number | null {
  const g = a.workflow ?? a.workflow_draft;
  return g?.nodes?.length ?? null;
}

function slugify(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

// A new agent's global prompt has to be non-empty: pipeline.py only appends
// the date grounding and the [[END_CALL]] safety net when there is a system
// prompt to append them to, so an agent created blank would have no way to
// hang up outside its graph. Same wording as config/agents/default.yaml.
// The flow itself comes from the server's starter_graph — this is only the
// text that goes with it.
const DEFAULT_GREETING = "Hello! How can I help you today?";
const DEFAULT_SYSTEM_PROMPT =
  "You are a helpful voice assistant on a phone call. Answer in at most 2-3 short " +
  "spoken sentences. Plain conversational speech only — no markdown, no lists.";

export default function WorkflowsPage() {
  const router = useRouter();
  const [agents, setAgents] = useState<AgentWithTenant[]>([]);
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [newTenant, setNewTenant] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    listTenants()
      .then(async (ts) => {
        setTenants(ts);
        if (ts.length > 0) setNewTenant(ts[0].slug);
        setAgents(await listAllAgents(ts));
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  }, []);

  const rows = useMemo(() => {
    const q = search.trim().toLowerCase();
    const matched = q
      ? agents.filter((a) => `${a.name} ${a.tenantName}`.toLowerCase().includes(q))
      : agents;
    // Agents that actually run a flow first — this page is about flows, so
    // the ones with nothing to show shouldn't be what you scroll past.
    const rank: Record<FlowState, number> = { live: 0, draft: 1, none: 2 };
    return [...matched].sort(
      (a, b) => rank[flowState(a)] - rank[flowState(b)] || a.name.localeCompare(b.name),
    );
  }, [agents, search]);

  const open = (a: AgentWithTenant) => router.push(`/workflows/${a.tenantSlug}/${a.slug}`);

  const handleCreate = async () => {
    const slug = slugify(newName);
    if (!slug || !newTenant) return;
    setBusy(true);
    setCreateError(null);
    try {
      // One call: the server creates the agent and its starter flow in the
      // same transaction (services/config/agents.py's create_agent), so
      // there's no window where a half-made agent exists.
      const agent = await createAgent(newTenant, {
        slug,
        name: newName.trim(),
        greeting: DEFAULT_GREETING,
        system_prompt: DEFAULT_SYSTEM_PROMPT,
      });
      router.push(`/workflows/${newTenant}/${agent.slug}`);
    } catch (e) {
      setCreateError(e instanceof ApiError ? e.detail : String(e));
      setBusy(false);
    }
  };

  return (
    <>
      <div className="card">
        <div className="card-hdr">
          <span className="card-title">Your Agents</span>
          <input
            className="form-input"
            style={{ width: 200, marginLeft: "auto" }}
            placeholder="Search agents…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <button className="btn btn-primary btn-sm" onClick={() => setCreating(true)}>
            + New agent
          </button>
        </div>

        {loading ? (
          <div className="empty-state">Loading…</div>
        ) : error ? (
          <div className="card-body"><div className="error-banner">{error}</div></div>
        ) : rows.length === 0 ? (
          <div className="empty-state">
            No agents yet. Create one to draw its first conversation flow.
          </div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Agent</th><th>Account</th><th>Flow</th><th>Steps</th><th />
              </tr>
            </thead>
            <tbody>
              {rows.map((a) => {
                const state = flowState(a);
                const steps = stepCount(a);
                return (
                  <tr key={a.id} onClick={() => open(a)}>
                    <td className="bold">{a.name}</td>
                    <td>{a.tenantName}</td>
                    <td>
                      <span className={`badge ${STATE_BADGE[state]}`}>{STATE_LABEL[state]}</span>
                    </td>
                    <td className="mono">{steps === null ? "—" : steps}</td>
                    <td style={{ textAlign: "right" }}>
                      <button className="btn btn-ghost btn-sm" onClick={(e) => { e.stopPropagation(); open(a); }}>
                        {state === "none" ? "Build a flow" : "Open"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="form-hint" style={{ marginTop: 10 }}>
        A flow splits a call into steps, each with its own instructions and its own tools, so the
        agent can&apos;t book before it has verified. Agents marked <strong>Single prompt</strong> run
        one instruction for the whole call — which is the right choice for simple agents. Voice,
        model, tools and phone number are under <strong>Settings</strong> inside each agent.
      </div>

      <Modal
        open={creating}
        title="New agent"
        onClose={() => { if (!busy) setCreating(false); }}
        footer={
          <>
            <button className="btn btn-ghost" disabled={busy} onClick={() => setCreating(false)}>
              Cancel
            </button>
            <button
              className="btn btn-primary"
              disabled={busy || !slugify(newName) || !newTenant}
              onClick={handleCreate}
            >
              {busy ? "Creating…" : "Create"}
            </button>
          </>
        }
      >
        {createError && <div className="error-banner">{createError}</div>}
        <div className="form-group">
          <label className="form-label">Name <span className="required">*</span></label>
          <input
            className="form-input"
            autoFocus
            value={newName}
            placeholder="Booking Bot"
            onChange={(e) => setNewName(e.target.value)}
          />
          {newName.trim() !== "" && (
            <div className="form-hint">
              Address: <span className="mono">{slugify(newName) || "—"}</span>
            </div>
          )}
        </div>
        <div className="form-group" style={{ marginBottom: 0 }}>
          <label className="form-label">Account <span className="required">*</span></label>
          <select
            className="form-select"
            value={newTenant}
            onChange={(e) => setNewTenant(e.target.value)}
          >
            {tenants.map((t) => (
              <option key={t.id} value={t.slug}>{t.name}</option>
            ))}
          </select>
          <div className="form-hint">
            You land on the canvas with a starter flow drawn. Everything else — voice, model,
            tools, number — is under Settings once it exists.
          </div>
        </div>
      </Modal>
    </>
  );
}
