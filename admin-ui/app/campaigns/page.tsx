"use client";

import { useEffect, useRef, useState } from "react";
import {
  addDncNumber,
  AgentWithTenant,
  ApiError,
  Campaign,
  CampaignContact,
  CampaignCreate,
  CampaignProgress,
  CampaignStatus,
  CampaignWithTenant,
  createCampaign,
  DncNumber,
  getCampaignProgress,
  listAllAgents,
  listAllCampaigns,
  listCampaignContacts,
  listDncNumbers,
  listTenants,
  pauseCampaign,
  removeDncNumber,
  resumeCampaign,
  startCampaign,
  Tenant,
  uploadCampaignContacts,
} from "@/lib/api";
import { Modal } from "@/components/Modal";

const STAT_ICONS: Record<string, React.ReactNode> = {
  total: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M1.5 6.5v3L5 10.5V5.5L1.5 6.5z" />
      <path d="M5 5.5l8.5-3v11l-8.5-3" />
    </svg>
  ),
  running: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M3 2h3l1.5 4-2 1.5a10 10 0 004.5 4.5L11.5 10l4 1.5v3a2 2 0 01-2 2C7.5 16.5 -0.5 8.5 1 3a2 2 0 012-1z" />
    </svg>
  ),
  success: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M1.5 12.5l4.5-5 3 3 5.5-6.5" />
      <path d="M10.5 4h4v4" />
    </svg>
  ),
  active: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="8" cy="8" r="6.5" />
      <path d="M8 4.5V8l2.5 1.5" />
    </svg>
  ),
  completed: (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="8" cy="8" r="6.5" />
      <path d="M5.2 8.3l1.9 1.9 3.7-4" />
    </svg>
  ),
};

function StatCard({
  icon, label, value, accent,
}: {
  icon: keyof typeof STAT_ICONS; label: string; value: string; accent: string;
}) {
  return (
    <div className="card" style={{ padding: "14px 16px", flex: "1 1 160px", minWidth: 150 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
        <span style={{ width: 15, height: 15, color: accent, flexShrink: 0 }}>{STAT_ICONS[icon]}</span>
        <span style={{ fontSize: ".68rem", fontWeight: 700, textTransform: "uppercase", letterSpacing: ".07em", color: "var(--text-3)" }}>
          {label}
        </span>
      </div>
      <div style={{ fontSize: "1.6rem", fontWeight: 700, color: accent, fontVariantNumeric: "tabular-nums" }}>{value}</div>
    </div>
  );
}

const STATUS_TABS: { label: string; value: CampaignStatus | "all" }[] = [
  { label: "All", value: "all" },
  { label: "Draft", value: "draft" },
  { label: "Running", value: "running" },
  { label: "Paused", value: "paused" },
  { label: "Completed", value: "completed" },
];

function statusBadgeClass(status: CampaignStatus): string {
  switch (status) {
    case "running":
      return "green";
    case "paused":
      return "amber";
    case "completed":
      return "cyan";
    default:
      return "gray";
  }
}

function contactBadgeClass(status: CampaignContact["status"]): string {
  switch (status) {
    case "completed":
      return "green";
    case "calling":
      return "amber";
    case "failed":
    case "no_answer":
      return "red";
    case "blocked":
      return "indigo";
    default:
      return "gray";
  }
}

const EMPTY_FORM: CampaignCreate = {
  agent_id: "",
  name: "",
  caller_id: "",
  max_concurrent_calls: 1,
  pacing_seconds: 5,
  max_attempts: 1,
  calling_hours_start: "",
  calling_hours_end: "",
  calling_hours_timezone: "UTC",
};

export default function CampaignsPage() {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [agents, setAgents] = useState<AgentWithTenant[]>([]);
  const [campaigns, setCampaigns] = useState<CampaignWithTenant[]>([]);
  const [progressById, setProgressById] = useState<Record<string, CampaignProgress>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusTab, setStatusTab] = useState<CampaignStatus | "all">("all");
  const [search, setSearch] = useState("");

  const [modalOpen, setModalOpen] = useState(false);
  const [createTenantId, setCreateTenantId] = useState("");
  const [form, setForm] = useState<CampaignCreate>(EMPTY_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const [detail, setDetail] = useState<CampaignWithTenant | null>(null);
  const [progress, setProgress] = useState<CampaignProgress | null>(null);
  const [contacts, setContacts] = useState<CampaignContact[]>([]);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadNotice, setUploadNotice] = useState<string | null>(null);
  const [actionSubmitting, setActionSubmitting] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [dncModalOpen, setDncModalOpen] = useState(false);
  const [dncTenantId, setDncTenantId] = useState("");
  const [dncNumbers, setDncNumbers] = useState<DncNumber[]>([]);
  const [dncLoading, setDncLoading] = useState(false);
  const [dncError, setDncError] = useState<string | null>(null);
  const [dncPhone, setDncPhone] = useState("");
  const [dncReason, setDncReason] = useState("");
  const [dncSubmitting, setDncSubmitting] = useState(false);

  useEffect(() => {
    listTenants().then((ts) => {
      setTenants(ts);
      if (ts.length > 0) setCreateTenantId(ts[0].id);
    });
  }, []);

  const refresh = () => {
    if (tenants.length === 0) return;
    setLoading(true);
    Promise.all([listAllCampaigns(tenants), listAllAgents(tenants)])
      .then(async ([cs, ags]) => {
        setCampaigns(cs);
        setAgents(ags);
        // Per-campaign progress powers the summary stat cards (running
        // calls in flight, aggregate success rate) — no aggregate endpoint
        // exists server-side, so this composes client-side same as
        // listAllCampaigns() itself does across tenants.
        const entries = await Promise.all(
          cs.map(async (c) => [c.id, await getCampaignProgress(c.id)] as const),
        );
        setProgressById(Object.fromEntries(entries));
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  };

  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(refresh, [tenants]);

  const agentsForTenant = (tenantId: string) =>
    agents.filter((a) => tenants.find((t) => t.id === tenantId)?.slug === a.tenantSlug);

  const agentName = (id: string) => agents.find((a) => a.id === id)?.name || id;

  const statusCounts = STATUS_TABS.reduce<Record<string, number>>((acc, tab) => {
    acc[tab.value] = tab.value === "all" ? campaigns.length : campaigns.filter((c) => c.status === tab.value).length;
    return acc;
  }, {});

  const visibleCampaigns = campaigns
    .filter((c) => statusTab === "all" || c.status === statusTab)
    .filter((c) => {
      const q = search.trim().toLowerCase();
      if (!q) return true;
      return c.name.toLowerCase().includes(q) || c.tenantName.toLowerCase().includes(q) || agentName(c.agent_id).toLowerCase().includes(q);
    });

  const aggregateProgress = Object.values(progressById).reduce(
    (acc, p) => ({
      total: acc.total + p.total,
      calling: acc.calling + p.calling,
      completed: acc.completed + p.completed,
    }),
    { total: 0, calling: 0, completed: 0 },
  );
  const avgSuccessPct = aggregateProgress.total > 0 ? Math.round((aggregateProgress.completed / aggregateProgress.total) * 100) : 0;

  useEffect(() => {
    if (modalOpen) {
      const first = agentsForTenant(createTenantId)[0];
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setForm({ ...EMPTY_FORM, agent_id: first?.id || "" });
      setFormError(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modalOpen, createTenantId]);

  const handleCreate = async () => {
    setSubmitting(true);
    setFormError(null);
    try {
      await createCampaign(createTenantId, form);
      setModalOpen(false);
      refresh();
    } catch (e) {
      setFormError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const openDetail = (c: CampaignWithTenant) => {
    setDetail(c);
    setDetailError(null);
    setUploadNotice(null);
    loadDetail(c.id);
  };

  const loadDetail = (campaignId: string) => {
    setDetailLoading(true);
    Promise.all([getCampaignProgress(campaignId), listCampaignContacts(campaignId)])
      .then(([p, cs]) => {
        setProgress(p);
        setContacts(cs);
      })
      .catch((e) => setDetailError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setDetailLoading(false));
  };

  const handleUpload = async (file: File) => {
    if (!detail) return;
    setUploading(true);
    setDetailError(null);
    setUploadNotice(null);
    try {
      const result = await uploadCampaignContacts(detail.id, file);
      setUploadNotice(
        result.skipped_dnc > 0
          ? `Added ${result.inserted} contact${result.inserted === 1 ? "" : "s"} — skipped ${result.skipped_dnc} on the do-not-call list.`
          : `Added ${result.inserted} contact${result.inserted === 1 ? "" : "s"}.`,
      );
      loadDetail(detail.id);
    } catch (e) {
      setDetailError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const runAction = async (action: (id: string) => Promise<Campaign>) => {
    if (!detail) return;
    setActionSubmitting(true);
    setDetailError(null);
    try {
      const updated = await action(detail.id);
      setDetail({ ...detail, status: updated.status });
      setCampaigns((cs) => cs.map((c) => (c.id === detail.id ? { ...c, status: updated.status } : c)));
      loadDetail(detail.id);
      getCampaignProgress(detail.id).then((p) => setProgressById((prev) => ({ ...prev, [detail.id]: p })));
    } catch (e) {
      setDetailError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setActionSubmitting(false);
    }
  };

  const openDncModal = () => {
    const tenantId = dncTenantId || tenants[0]?.id || "";
    setDncTenantId(tenantId);
    setDncModalOpen(true);
    setDncError(null);
    setDncPhone("");
    setDncReason("");
    if (tenantId) loadDncNumbers(tenantId);
  };

  const loadDncNumbers = (tenantId: string) => {
    setDncLoading(true);
    listDncNumbers(tenantId)
      .then(setDncNumbers)
      .catch((e) => setDncError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setDncLoading(false));
  };

  const handleAddDnc = async () => {
    if (!dncPhone.trim()) return;
    setDncSubmitting(true);
    setDncError(null);
    try {
      await addDncNumber(dncTenantId, dncPhone.trim(), dncReason.trim() || undefined);
      setDncPhone("");
      setDncReason("");
      loadDncNumbers(dncTenantId);
    } catch (e) {
      setDncError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setDncSubmitting(false);
    }
  };

  const handleRemoveDnc = async (id: string) => {
    try {
      await removeDncNumber(id);
      loadDncNumbers(dncTenantId);
    } catch (e) {
      setDncError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  return (
    <>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 12, marginBottom: 16 }}>
        <StatCard icon="total" label="Total Campaigns" value={loading ? "—" : String(campaigns.length)} accent="var(--text)" />
        <StatCard icon="running" label="Running Calls" value={loading ? "—" : String(aggregateProgress.calling)} accent="var(--cyan)" />
        <StatCard icon="success" label="Avg Success" value={loading ? "—" : `${avgSuccessPct}%`} accent="var(--green)" />
        <StatCard icon="active" label="Active" value={loading ? "—" : String(statusCounts.running || 0)} accent="var(--amber)" />
        <StatCard icon="completed" label="Completed" value={loading ? "—" : String(statusCounts.completed || 0)} accent="var(--indigo)" />
      </div>

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14, gap: 10, flexWrap: "wrap" }}>
        <div className="tabs" style={{ display: "flex", gap: 4 }}>
          {STATUS_TABS.map((tab) => (
            <button
              key={tab.value}
              className={`btn btn-sm ${statusTab === tab.value ? "btn-primary" : "btn-ghost"}`}
              onClick={() => setStatusTab(tab.value)}
            >
              {tab.label} <span style={{ opacity: 0.7 }}>({statusCounts[tab.value] || 0})</span>
            </button>
          ))}
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <input
            className="form-input"
            style={{ width: 220 }}
            placeholder="Search campaigns…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <button className="btn btn-ghost btn-sm" onClick={openDncModal} disabled={tenants.length === 0}>
            Do-Not-Call List
          </button>
          <button className="btn btn-primary btn-sm" onClick={() => setModalOpen(true)} disabled={tenants.length === 0}>
            + New Campaign
          </button>
        </div>
      </div>

      {error && <div className="error-banner">{error}</div>}

      <div className="card">
        {loading ? (
          <div className="empty-state">Loading…</div>
        ) : visibleCampaigns.length === 0 ? (
          <div className="empty-state">
            {campaigns.length === 0 ? "No outbound campaigns yet." : "No campaigns match your current filters or search."}
          </div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Name</th>
                <th>Account</th>
                <th>Agent</th>
                <th>Status</th>
                <th>Caller ID</th>
                <th>Pacing</th>
                <th>Concurrency</th>
              </tr>
            </thead>
            <tbody>
              {visibleCampaigns.map((c) => (
                <tr key={c.id} onClick={() => openDetail(c)}>
                  <td className="bold" style={{ color: "var(--text)" }}>
                    {c.name}
                  </td>
                  <td>{c.tenantName}</td>
                  <td>{agentName(c.agent_id)}</td>
                  <td>
                    <span className={`badge ${statusBadgeClass(c.status)}`}>{c.status}</span>
                  </td>
                  <td className="mono">{c.caller_id || "—"}</td>
                  <td>{c.pacing_seconds}s</td>
                  <td>{c.max_concurrent_calls}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <Modal
        open={modalOpen}
        title="New Campaign"
        onClose={() => setModalOpen(false)}
        footer={
          <>
            <button className="btn btn-ghost btn-sm" onClick={() => setModalOpen(false)}>
              Cancel
            </button>
            <button
              className="btn btn-primary btn-sm"
              onClick={handleCreate}
              disabled={submitting || !form.name || !form.agent_id}
            >
              {submitting ? "Creating…" : "Create"}
            </button>
          </>
        }
      >
        {formError && <div className="error-banner">{formError}</div>}
        <div className="form-group">
          <label className="form-label">
            Account <span className="required">*</span>
          </label>
          <select className="form-select" value={createTenantId} onChange={(e) => setCreateTenantId(e.target.value)}>
            {tenants.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        </div>
        <div className="form-group">
          <label className="form-label">
            Name <span className="required">*</span>
          </label>
          <input
            className="form-input"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="e.g. Renewal reminders — July"
          />
        </div>
        <div className="form-group">
          <label className="form-label">
            Agent <span className="required">*</span>
            <span className="hint">the voice agent placing the calls</span>
          </label>
          <select
            className="form-select"
            value={form.agent_id}
            onChange={(e) => setForm({ ...form, agent_id: e.target.value })}
          >
            <option value="">— select an agent —</option>
            {agentsForTenant(createTenantId).map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        </div>
        <div className="form-group">
          <label className="form-label">
            Caller ID <span className="hint">the agent&apos;s own inbound DID — required before Start</span>
          </label>
          <input
            className="form-input"
            style={{ fontFamily: "var(--mono)" }}
            value={form.caller_id || ""}
            onChange={(e) => setForm({ ...form, caller_id: e.target.value })}
            placeholder="5000"
          />
        </div>
        <div style={{ display: "flex", gap: 12 }}>
          <div className="form-group" style={{ flex: 1 }}>
            <label className="form-label">Pacing (seconds)</label>
            <input
              type="number"
              min={0}
              className="form-input"
              value={form.pacing_seconds}
              onChange={(e) => setForm({ ...form, pacing_seconds: Number(e.target.value) })}
            />
          </div>
          <div className="form-group" style={{ flex: 1 }}>
            <label className="form-label">Max concurrent calls</label>
            <input
              type="number"
              min={1}
              className="form-input"
              value={form.max_concurrent_calls}
              onChange={(e) => setForm({ ...form, max_concurrent_calls: Number(e.target.value) })}
            />
          </div>
          <div className="form-group" style={{ flex: 1 }}>
            <label className="form-label">
              Max attempts <span className="hint">retries a failed/no-answer contact this many times total</span>
            </label>
            <input
              type="number"
              min={1}
              className="form-input"
              value={form.max_attempts}
              onChange={(e) => setForm({ ...form, max_attempts: Number(e.target.value) })}
            />
          </div>
        </div>

        <div className="form-group">
          <label className="form-label">
            Calling hours <span className="hint">leave both blank to allow any time — outside this window, contacts are skipped, not failed</span>
          </label>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <input
              type="time"
              className="form-input"
              style={{ width: 120 }}
              value={form.calling_hours_start || ""}
              onChange={(e) => setForm({ ...form, calling_hours_start: e.target.value })}
            />
            <span style={{ color: "var(--text-3)" }}>to</span>
            <input
              type="time"
              className="form-input"
              style={{ width: 120 }}
              value={form.calling_hours_end || ""}
              onChange={(e) => setForm({ ...form, calling_hours_end: e.target.value })}
            />
            <input
              className="form-input"
              style={{ flex: 1, fontFamily: "var(--mono)" }}
              placeholder="Timezone, e.g. America/New_York"
              value={form.calling_hours_timezone || ""}
              onChange={(e) => setForm({ ...form, calling_hours_timezone: e.target.value })}
            />
          </div>
        </div>
      </Modal>

      <Modal
        open={detail !== null}
        title={detail ? `Campaign — ${detail.name}` : ""}
        onClose={() => setDetail(null)}
        footer={
          detail && (
            <>
              {detail.status === "draft" && (
                <button
                  className="btn btn-primary btn-sm"
                  disabled={actionSubmitting || !detail.caller_id}
                  title={!detail.caller_id ? "Set a caller ID before starting" : undefined}
                  onClick={() => runAction(startCampaign)}
                >
                  Start
                </button>
              )}
              {detail.status === "running" && (
                <button className="btn btn-ghost btn-sm" disabled={actionSubmitting} onClick={() => runAction(pauseCampaign)}>
                  Pause
                </button>
              )}
              {detail.status === "paused" && (
                <button className="btn btn-primary btn-sm" disabled={actionSubmitting} onClick={() => runAction(resumeCampaign)}>
                  Resume
                </button>
              )}
            </>
          )
        }
      >
        {detailError && <div className="error-banner">{detailError}</div>}
        {detail && (
          <>
            <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginBottom: 14 }}>
              <span>
                Status <span className={`badge ${statusBadgeClass(detail.status)}`} style={{ marginLeft: 4 }}>{detail.status}</span>
              </span>
              <span>
                Caller ID <span className="mono" style={{ color: "var(--text)" }}>{detail.caller_id || "not set"}</span>
              </span>
              <span>
                Pacing <strong style={{ color: "var(--text)" }}>{detail.pacing_seconds}s</strong>
              </span>
              <span>
                Concurrency <strong style={{ color: "var(--text)" }}>{detail.max_concurrent_calls}</strong>
              </span>
              <span>
                Max attempts <strong style={{ color: "var(--text)" }}>{detail.max_attempts}</strong>
              </span>
              <span>
                Calling hours{" "}
                <strong style={{ color: "var(--text)" }}>
                  {detail.calling_hours_start && detail.calling_hours_end
                    ? `${detail.calling_hours_start}–${detail.calling_hours_end} (${detail.calling_hours_timezone})`
                    : "any time"}
                </strong>
              </span>
            </div>

            <div style={{ fontSize: ".65rem", fontWeight: 700, textTransform: "uppercase", letterSpacing: ".09em", color: "var(--text-3)", marginBottom: 6 }}>
              Progress
            </div>
            {detailLoading && !progress ? (
              <div className="empty-state">Loading…</div>
            ) : progress ? (
              <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginBottom: 16 }}>
                <span>Total <strong style={{ color: "var(--text)" }}>{progress.total}</strong></span>
                <span><span className="badge gray">pending</span> {progress.pending}</span>
                <span><span className="badge amber">calling</span> {progress.calling}</span>
                <span><span className="badge green">completed</span> {progress.completed}</span>
                <span><span className="badge red">failed</span> {progress.failed}</span>
                <span><span className="badge red">no_answer</span> {progress.no_answer}</span>
                <span><span className="badge indigo">blocked</span> {progress.blocked}</span>
              </div>
            ) : null}

            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
              <div style={{ fontSize: ".65rem", fontWeight: 700, textTransform: "uppercase", letterSpacing: ".09em", color: "var(--text-3)" }}>
                Contacts
              </div>
              <label className="btn btn-ghost btn-sm" style={{ cursor: "pointer" }}>
                {uploading ? "Uploading…" : "Upload CSV"}
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".csv"
                  style={{ display: "none" }}
                  disabled={uploading}
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    if (file) handleUpload(file);
                  }}
                />
              </label>
            </div>
            <div className="hint" style={{ marginBottom: 8 }}>
              CSV with a <code>phone_number</code> column (and optional <code>name</code>). Numbers on the do-not-call list are skipped automatically.
            </div>
            {uploadNotice && <div className="hint" style={{ marginBottom: 8, color: "var(--green)" }}>{uploadNotice}</div>}
            {contacts.length === 0 ? (
              <div className="empty-state">No contacts uploaded yet.</div>
            ) : (
              <div style={{ maxHeight: 260, overflowY: "auto" }}>
                <table className="tbl">
                  <thead>
                    <tr>
                      <th>Phone</th>
                      <th>Name</th>
                      <th>Status</th>
                      <th>Attempts</th>
                      <th>Call</th>
                    </tr>
                  </thead>
                  <tbody>
                    {contacts.map((ct) => (
                      <tr key={ct.id}>
                        <td className="mono">{ct.phone_number}</td>
                        <td>{ct.name || "—"}</td>
                        <td>
                          <span className={`badge ${contactBadgeClass(ct.status)}`}>{ct.status}</span>
                        </td>
                        <td>{ct.attempt_count}</td>
                        <td className="mono" style={{ fontSize: ".68rem" }} title={ct.call_session_id || undefined}>
                          {ct.call_session_id ? `${ct.call_session_id.slice(0, 8)}…` : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </Modal>

      <Modal open={dncModalOpen} title="Do-Not-Call List" onClose={() => setDncModalOpen(false)}>
        {dncError && <div className="error-banner">{dncError}</div>}
        <div className="form-group">
          <label className="form-label">Account</label>
          <select
            className="form-select"
            value={dncTenantId}
            onChange={(e) => {
              setDncTenantId(e.target.value);
              loadDncNumbers(e.target.value);
            }}
          >
            {tenants.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        </div>

        <div style={{ display: "flex", gap: 8, marginBottom: 14 }}>
          <input
            className="form-input"
            style={{ flex: 1, fontFamily: "var(--mono)" }}
            placeholder="Phone number"
            value={dncPhone}
            onChange={(e) => setDncPhone(e.target.value)}
          />
          <input
            className="form-input"
            style={{ flex: 1 }}
            placeholder="Reason (optional)"
            value={dncReason}
            onChange={(e) => setDncReason(e.target.value)}
          />
          <button className="btn btn-primary btn-sm" onClick={handleAddDnc} disabled={dncSubmitting || !dncPhone.trim()}>
            {dncSubmitting ? "Adding…" : "Add"}
          </button>
        </div>

        {dncLoading ? (
          <div className="empty-state">Loading…</div>
        ) : dncNumbers.length === 0 ? (
          <div className="empty-state">No numbers on this account&apos;s do-not-call list.</div>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Phone</th>
                <th>Reason</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {dncNumbers.map((d) => (
                <tr key={d.id}>
                  <td className="mono">{d.phone_number}</td>
                  <td>{d.reason || "—"}</td>
                  <td>
                    <button className="btn btn-danger btn-sm" onClick={() => handleRemoveDnc(d.id)}>
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Modal>
    </>
  );
}
