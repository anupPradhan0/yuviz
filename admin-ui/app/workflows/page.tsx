"use client";

// Workflows list — create drops you on the canvas; voice/model/tools/number
// live at ./[tenant]/[agent]/settings.

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

type FlowState = "live" | "unpublished" | "draft" | "none";

function flowState(a: AgentWithTenant): FlowState {
  if (a.has_workflow) return a.workflow_diverged ? "unpublished" : "live";
  if (a.has_workflow_draft) return "draft";
  return "none";
}

const STATE_LABEL: Record<FlowState, string> = {
  live: "Live",
  unpublished: "Unpublished changes",
  draft: "Draft — not published",
  none: "Single prompt",
};

const STATE_BADGE: Record<FlowState, string> = {
  live: "green",
  unpublished: "amber",
  draft: "amber",
  none: "gray",
};

function stepCount(a: AgentWithTenant): number | null {
  return a.workflow_node_count ?? null;
}

function slugify(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function initial(name: string): string {
  const trimmed = name.trim();
  return trimmed ? trimmed[0]!.toUpperCase() : "?";
}

// Non-empty global prompt so pipeline date grounding / [[END_CALL]] attach.
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
    if (new URLSearchParams(window.location.search).get("new") !== "1") return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setCreating(true);
  }, []);

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
      ? agents.filter((a) => `${a.name} ${a.tenantName} ${a.slug}`.toLowerCase().includes(q))
      : agents;
    const rank: Record<FlowState, number> = { live: 0, unpublished: 1, draft: 2, none: 3 };
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
    <div className="agents-page">
      <div className="agents-hero">
        <div className="agents-hero-copy">
          <h1 className="agents-hero-title">Workflows</h1>
          <p className="agents-hero-sub">
            Open a flow to edit it, or create a new one.
          </p>
        </div>
        <div className="agents-hero-actions">
          {agents.length > 0 && (
            <div className="agents-search">
              <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden>
                <circle cx="7" cy="7" r="4.5" />
                <path d="M10.5 10.5L14 14" strokeLinecap="round" />
              </svg>
              <input
                className="form-input"
                placeholder="Search workflows…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                aria-label="Search workflows"
              />
            </div>
          )}
          <button className="btn btn-primary" onClick={() => setCreating(true)}>
            + New workflow
          </button>
        </div>
      </div>

      <div className="card agents-card">
        {loading ? (
          <div className="empty-state">Loading…</div>
        ) : error ? (
          <div className="card-body"><div className="error-banner">{error}</div></div>
        ) : agents.length === 0 ? (
          <div className="agents-empty">
            <div className="agents-empty-title">No workflows yet</div>
            <p>Create one to draw its first conversation flow.</p>
            <button className="btn btn-primary btn-sm" onClick={() => setCreating(true)}>
              + New workflow
            </button>
          </div>
        ) : rows.length === 0 ? (
          <div className="agents-empty">
            <div className="agents-empty-title">No matches</div>
            <p>Nothing matches “{search.trim()}”.</p>
            <button className="btn btn-ghost btn-sm" onClick={() => setSearch("")}>
              Clear search
            </button>
          </div>
        ) : (
          <ul className="agents-list">
            {rows.map((a) => {
              const state = flowState(a);
              const steps = stepCount(a);
              return (
                <li key={a.id}>
                  <button type="button" className="agents-row" onClick={() => open(a)}>
                    <span className="agents-avatar" aria-hidden>{initial(a.name)}</span>
                    <span className="agents-row-main">
                      <span className="agents-row-name">{a.name}</span>
                      <span className="agents-row-meta">
                        <span>{a.tenantName}</span>
                        {steps !== null && (
                          <>
                            <span className="agents-dot" aria-hidden>·</span>
                            <span>{steps} step{steps === 1 ? "" : "s"}</span>
                          </>
                        )}
                      </span>
                    </span>
                    <span className={`badge ${STATE_BADGE[state]}`}>{STATE_LABEL[state]}</span>
                    <span className="agents-row-action">
                      {state === "none" ? "Build flow" : "Open"}
                      <span aria-hidden>→</span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <Modal
        open={creating}
        title="New workflow"
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
            Opens on the canvas with a starter flow. Voice, model, tools and number are under
            Settings after you create it.
          </div>
        </div>
      </Modal>
    </div>
  );
}
