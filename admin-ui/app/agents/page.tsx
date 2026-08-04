"use client";

import { useEffect, useState } from "react";
import {
  AgentCreate,
  AgentStatus,
  AgentWithTenant,
  ApiError,
  createAgent,
  deleteAgent,
  listAllAgents,
  listTenants,
  Tenant,
  updateAgent,
} from "@/lib/api";
import { Modal } from "@/components/Modal";

const ALL_TENANTS = "__all__";

export default function AgentsPage() {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [filterTenant, setFilterTenant] = useState<string>(ALL_TENANTS);
  const [agents, setAgents] = useState<AgentWithTenant[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);

  const [createTenantSlug, setCreateTenantSlug] = useState("");
  const [form, setForm] = useState<AgentCreate>({ slug: "", name: "", greeting: "", system_prompt: "" });
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  useEffect(() => {
    listTenants().then((ts) => {
      setTenants(ts);
      if (ts.length > 0) setCreateTenantSlug(ts[0].slug);
    });
  }, []);

  const refresh = () => {
    if (tenants.length === 0) return;
    setLoading(true);
    listAllAgents(tenants)
      .then(setAgents)
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  };

  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(refresh, [tenants]);

  const handleCreate = async () => {
    setSubmitting(true);
    setFormError(null);
    try {
      await createAgent(createTenantSlug, form);
      setModalOpen(false);
      setForm({ slug: "", name: "", greeting: "", system_prompt: "" });
      refresh();
    } catch (e) {
      setFormError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const handleDelete = async (a: AgentWithTenant) => {
    if (!window.confirm(`Delete agent "${a.name}"? Any DIDs still pointing at it will fall back to their fallback agent, or default.`)) return;
    try {
      await deleteAgent(a.tenantSlug, a.id);
      refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const handleStatusChange = async (a: AgentWithTenant, status: AgentStatus) => {
    try {
      await updateAgent(a.tenantSlug, a.id, { status });
      refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const visibleAgents = filterTenant === ALL_TENANTS ? agents : agents.filter((a) => a.tenantSlug === filterTenant);

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 14, gap: 10 }}>
        <select className="form-select" style={{ width: 240 }} value={filterTenant} onChange={(e) => setFilterTenant(e.target.value)}>
          <option value={ALL_TENANTS}>All Accounts</option>
          {tenants.map((t) => (
            <option key={t.id} value={t.slug}>
              {t.name}
            </option>
          ))}
        </select>
        <button className="btn btn-primary btn-sm" onClick={() => setModalOpen(true)} disabled={tenants.length === 0}>
          + New Agent
        </button>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card">
        {loading ? (
          <div className="empty-state">Loading…</div>
        ) : visibleAgents.length === 0 ? (
          <div className="empty-state">No agents yet.</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Name</th>
                <th>Account</th>
                <th>Slug</th>
                <th>Status</th>
                <th>Version</th>
                <th>Created</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {visibleAgents.map((a) => (
                <tr key={a.id}>
                  <td className="bold">{a.name}</td>
                  <td>{a.tenantName}</td>
                  <td className="mono">{a.slug}</td>
                  <td onClick={(e) => e.stopPropagation()}>
                    <select
                      className={`form-select status-select ${a.status}`}
                      style={{ width: 100, padding: "3px 8px", fontSize: ".71rem" }}
                      value={a.status}
                      onChange={(ev) => handleStatusChange(a, ev.target.value as AgentStatus)}
                    >
                      <option value="active">active</option>
                      <option value="inactive">inactive</option>
                    </select>
                  </td>
                  <td>
                    <span className="ver-badge">v{a.config_version}</span>
                  </td>
                  <td style={{ fontSize: ".71rem", color: "var(--text-3)" }}>{new Date(a.created_at).toLocaleDateString()}</td>
                  <td style={{ display: "flex", gap: 6 }}>
                    <a className="btn btn-ghost btn-sm" href={`/agents/${a.tenantSlug}/${a.slug}`}>
                      Edit
                    </a>
                    <button className="btn btn-danger btn-sm" onClick={() => handleDelete(a)}>
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <Modal
        open={modalOpen}
        title="New Agent"
        onClose={() => setModalOpen(false)}
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={() => setModalOpen(false)}>
              Cancel
            </button>
            <button className="btn btn-primary btn-sm" onClick={handleCreate} disabled={submitting || !form.slug || !form.name}>
              {submitting ? "Creating…" : "Create Agent"}
            </button>
          </>
        }
      >
        {formError && <div className="error-banner">{formError}</div>}
        <div className="form-group">
          <label className="form-label">
            Account <span className="required">*</span>
          </label>
          <select className="form-select" value={createTenantSlug} onChange={(e) => setCreateTenantSlug(e.target.value)}>
            {tenants.map((t) => (
              <option key={t.id} value={t.slug}>
                {t.name}
              </option>
            ))}
          </select>
        </div>
        <div className="form-group">
          <label className="form-label">
            Display Name <span className="required">*</span>
          </label>
          <input className="form-input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Customer Support" />
        </div>
        <div className="form-group">
          <label className="form-label">
            Slug <span className="required">*</span>
            <span className="hint">used in the WS routing path — no spaces</span>
          </label>
          <input className="form-input" style={{ fontFamily: "var(--mono)" }} value={form.slug} onChange={(e) => setForm({ ...form, slug: e.target.value })} placeholder="support" />
        </div>
        <div className="form-group">
          <label className="form-label">Greeting</label>
          <textarea
            className="form-textarea"
            style={{ minHeight: 50 }}
            value={form.greeting}
            onChange={(e) => setForm({ ...form, greeting: e.target.value })}
            placeholder="Hi! How can I help you today?"
          />
        </div>
        <div className="form-group">
          <label className="form-label">System Prompt</label>
          <textarea
            className="form-textarea"
            value={form.system_prompt}
            onChange={(e) => setForm({ ...form, system_prompt: e.target.value })}
            placeholder="You are a helpful customer support agent."
          />
        </div>
      </Modal>
    </>
  );
}
