"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Check, ChevronDown, ChevronRight } from "lucide-react";
import { ApiError, listProviders, ProviderConfig } from "@/lib/api";
import {
  AgentKnowledgeBase,
  assignKnowledgeBase,
  createKnowledgeBase,
  deleteDocument,
  detachKnowledgeBase,
  getRetrievalPolicy,
  KbDocument,
  KnowledgeBase,
  listAgentKnowledgeBases,
  listDocuments,
  listKnowledgeBases,
  setKnowledgeBaseEnabled,
  setRetrievalPolicy,
  updateDocument,
  uploadDocument,
} from "@/lib/knowledgeApi";
import { Modal } from "@/components/Modal";

interface PolicyForm {
  top_k: number;
  minimum_score: number;
  max_tokens: number;
  include_citations: boolean;
}

const DEFAULT_POLICY: PolicyForm = { top_k: 5, minimum_score: 0, max_tokens: 1000, include_citations: true };

// Without agentId: authoring (create/upload/delete). With agentId: attach-only plus the
// retrieval-policy card.
export function KnowledgeBasePanel({ tenantId, agentId }: { tenantId: string; agentId?: string }) {
  const [allKbs, setAllKbs] = useState<KnowledgeBase[]>([]);
  const [kbsError, setKbsError] = useState<string | null>(null);
  const [assignments, setAssignments] = useState<AgentKnowledgeBase[]>([]);
  const [assignmentsError, setAssignmentsError] = useState<string | null>(null);
  const [providersError, setProvidersError] = useState<string | null>(null);
  const [docsByKb, setDocsByKb] = useState<Record<string, KbDocument[]>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [embeddingProviders, setEmbeddingProviders] = useState<ProviderConfig[]>([]);
  const [loading, setLoading] = useState(true);

  const [policyForm, setPolicyForm] = useState<PolicyForm>(DEFAULT_POLICY);
  const [policySaving, setPolicySaving] = useState(false);
  const [policySaved, setPolicySaved] = useState(false);

  const [createOpen, setCreateOpen] = useState(false);
  const [createForm, setCreateForm] = useState({ slug: "", name: "", description: "", embedding_config_id: "" });
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const [uploadKbId, setUploadKbId] = useState<string | null>(null);
  const [uploadTitle, setUploadTitle] = useState("");
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  // Each source is caught independently so one 403 doesn't blank the others.
  const refresh = async () => {
    setLoading(true);
    let kbs: KnowledgeBase[] = [];
    let assigns: AgentKnowledgeBase[] = [];
    await Promise.allSettled([
      listKnowledgeBases(tenantId)
        .then((ks) => {
          kbs = ks;
          setAllKbs(ks);
        })
        .then(() => setKbsError(null))
        .catch((e) => setKbsError(e instanceof ApiError ? e.detail : String(e))),
      agentId
        ? listAgentKnowledgeBases(agentId)
            .then((a) => {
              assigns = a;
              setAssignments(a);
            })
            .then(() => setAssignmentsError(null))
            .catch((e) => setAssignmentsError(e instanceof ApiError ? e.detail : String(e)))
        : Promise.resolve(),
      listProviders(tenantId, { role: "embedding" })
        .then(setEmbeddingProviders)
        .then(() => setProvidersError(null))
        .catch((e) => setProvidersError(e instanceof ApiError ? e.detail : String(e))),
    ]);

    const kbIdsForDocs = [...new Set([...kbs.map((kb) => kb.id), ...assigns.map((a) => a.kb_id)])];
    const docResults = await Promise.allSettled(kbIdsForDocs.map((id) => listDocuments(id)));
    setDocsByKb(
      Object.fromEntries(
        kbIdsForDocs.map((id, i) => {
          const r = docResults[i];
          return [id, r.status === "fulfilled" ? r.value : []];
        }),
      ),
    );
    setLoading(false);
  };

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
    if (agentId) {
      getRetrievalPolicy(agentId)
        .then((p) =>
          setPolicyForm({
            top_k: p.top_k ?? DEFAULT_POLICY.top_k,
            minimum_score: p.minimum_score ?? DEFAULT_POLICY.minimum_score,
            max_tokens: p.max_tokens ?? DEFAULT_POLICY.max_tokens,
            include_citations: p.include_citations ?? DEFAULT_POLICY.include_citations,
          }),
        )
        .catch((e) => setAssignmentsError(e instanceof ApiError ? e.detail : String(e)));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, agentId]);

  const assignedKbIds = new Set(assignments.map((a) => a.kb_id));
  const unassignedKbs = allKbs.filter((kb) => !assignedKbIds.has(kb.id));
  // Retrieval is on exactly when at least one attached KB is enabled.
  const ragEnabled = assignments.some((a) => a.enabled);

  const withErrorHandling = async (fn: () => Promise<unknown>, onError: (msg: string) => void) => {
    try {
      await fn();
      await refresh();
    } catch (e) {
      onError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const handleAttach = (kbId: string) => {
    if (!agentId) return;
    return withErrorHandling(() => assignKnowledgeBase(agentId, kbId), setAssignmentsError);
  };
  const handleToggleEnabled = (kbId: string, enabled: boolean) => {
    if (!agentId) return;
    return withErrorHandling(() => setKnowledgeBaseEnabled(agentId, kbId, enabled), setAssignmentsError);
  };
  const handleDetach = (kbId: string) => {
    if (!agentId) return;
    if (!confirm("Detach this knowledge base from the agent? Documents themselves are not deleted.")) return;
    withErrorHandling(() => detachKnowledgeBase(agentId, kbId), setAssignmentsError);
  };
  const handleUsageModeToggle = (doc: KbDocument) =>
    withErrorHandling(
      () => updateDocument(doc.id, { usage_mode: doc.usage_mode === "auto" ? "prompt" : "auto" }),
      setKbsError,
    );
  const handleDeleteDoc = (doc: KbDocument) => {
    if (!confirm(`Delete document "${doc.title}"?`)) return;
    withErrorHandling(() => deleteDocument(doc.id), setKbsError);
  };

  const handleCreateKb = async () => {
    setCreating(true);
    setCreateError(null);
    try {
      await createKnowledgeBase(tenantId, {
        slug: createForm.slug,
        name: createForm.name,
        description: createForm.description,
        embedding_config_id: createForm.embedding_config_id || undefined,
      });
      setCreateOpen(false);
      setCreateForm({ slug: "", name: "", description: "", embedding_config_id: "" });
      await refresh();
    } catch (e) {
      setCreateError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setCreating(false);
    }
  };

  const handleUpload = async () => {
    if (!uploadKbId || !uploadFile) return;
    setUploading(true);
    setUploadError(null);
    try {
      await uploadDocument(uploadKbId, uploadFile, uploadTitle || uploadFile.name);
      setUploadKbId(null);
      setUploadTitle("");
      setUploadFile(null);
      await refresh();
    } catch (e) {
      setUploadError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setUploading(false);
    }
  };

  const handleSavePolicy = async () => {
    if (!agentId) return;
    setPolicySaving(true);
    setPolicySaved(false);
    try {
      await setRetrievalPolicy(agentId, policyForm);
      setPolicySaved(true);
      setTimeout(() => setPolicySaved(false), 2000);
    } catch (e) {
      setAssignmentsError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setPolicySaving(false);
    }
  };

  const statusBadge = (status: KbDocument["status"]) => {
    const cls = status === "ready" ? "green" : status === "failed" ? "red" : status === "processing" ? "amber" : "gray";
    return <span className={`badge ${cls}`}>{status}</span>;
  };

  if (loading) return <div className="empty-state">Loading…</div>;

  type Row = { id: string; name: string; enabled: boolean | null; attached: boolean };
  // Authoring: every tenant KB. Attach: assigned KBs, plus the rest as "available to add".
  const rows: Row[] = agentId
    ? assignments.map((a) => ({ id: a.kb_id, name: a.kb_name, enabled: a.enabled, attached: true }))
    : allKbs.map((kb) => ({ id: kb.id, name: kb.name, enabled: null, attached: false }));
  const availableRows: Row[] = agentId
    ? unassignedKbs.map((kb) => ({ id: kb.id, name: kb.name, enabled: null, attached: false }))
    : [];

  const renderRow = (row: Row) => {
    const docs = docsByKb[row.id] || [];
    const isExpanded = expanded[row.id] ?? true;
    return (
      <div key={row.id}>
        <div className="kb-row">
          <button
            className="btn btn-ghost btn-sm btn-icon"
            aria-label={isExpanded ? "Collapse" : "Expand"}
            onClick={() => setExpanded({ ...expanded, [row.id]: !isExpanded })}
          >
            {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </button>
          <div style={{ flex: 1 }}>
            <div style={{ fontWeight: 500 }}>{row.name}</div>
            <div style={{ fontSize: ".7rem", color: "var(--text-3)" }}>
              {docs.length} document{docs.length === 1 ? "" : "s"}
            </div>
          </div>
          {agentId && row.attached && (
            <label className="toggle-switch" title={row.enabled ? "On: used when it's relevant" : "Off: not used"}>
              <input
                type="checkbox"
                checked={!!row.enabled}
                onChange={(e) => handleToggleEnabled(row.id, e.target.checked)}
              />
              <span className="toggle-slider" />
            </label>
          )}
          {!agentId && (
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => {
                setUploadKbId(row.id);
                setUploadTitle("");
                setUploadFile(null);
                setUploadError(null);
              }}
            >
              + Document
            </button>
          )}
          {agentId && row.attached && (
            <button className="btn btn-danger btn-sm" onClick={() => handleDetach(row.id)}>
              Remove
            </button>
          )}
          {agentId && !row.attached && (
            <button className="btn btn-primary btn-sm" onClick={() => handleAttach(row.id)}>
              + Use in this agent
            </button>
          )}
        </div>
        {isExpanded &&
          (docs.length === 0 ? (
            <div className="kb-doc-row" style={{ color: "var(--text-3)" }}>
              No documents yet.
            </div>
          ) : (
            docs.map((doc) => (
              <div key={doc.id} className="kb-doc-row">
                <div style={{ flex: 1 }}>
                  <span className="mono">{doc.title}</span>
                  {doc.error && <div style={{ color: "var(--red)", fontSize: ".68rem" }}>{doc.error}</div>}
                </div>
                {statusBadge(doc.status)}
                {agentId ? (
                  doc.usage_mode === "prompt" && (
                    <span className="badge gray" title="The agent reads this on every call">
                      always used
                    </span>
                  )
                ) : (
                  <label
                    style={{ display: "flex", alignItems: "center", gap: 6, color: "var(--text-3)" }}
                    title="The agent reads this whole document on every call, not just the parts that match the question"
                  >
                    <input
                      type="checkbox"
                      checked={doc.usage_mode === "prompt"}
                      onChange={() => handleUsageModeToggle(doc)}
                      disabled={doc.status !== "ready"}
                    />
                    Always read this
                  </label>
                )}
                {!agentId && (
                  <button className="btn btn-danger btn-sm" onClick={() => handleDeleteDoc(doc)}>
                    Delete
                  </button>
                )}
              </div>
            ))
          ))}
      </div>
    );
  };

  return (
    <div className="cols">
      <div className="col-main">
        {kbsError && <div className="error-banner">{kbsError}</div>}
        {agentId && assignmentsError && <div className="error-banner">{assignmentsError}</div>}
        {providersError && <div className="error-banner">{providersError}</div>}

        <div className="card">
          <div className="card-hdr">
            <div className="card-title">{agentId ? "Documents this agent uses" : "Knowledge Bases"}</div>
            <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
              {!agentId && (
                <button className="btn btn-primary btn-sm" onClick={() => setCreateOpen(true)}>
                  + New Knowledge Base
                </button>
              )}
            </div>
          </div>

          {rows.length === 0 ? (
            <div className="empty-state">
              {agentId
                ? availableRows.length > 0
                  ? "This agent isn't using any documents yet. Pick one from the list below."
                  : (
                    <>
                      No documents yet. <Link href="/knowledge-bases?add=1" style={{ color: "var(--cyan)" }}>Add one on the Knowledge page</Link>, then come back here.
                    </>
                  )
                : "No knowledge bases yet."}
            </div>
          ) : (
            rows.map(renderRow)
          )}
        </div>

        {availableRows.length > 0 && (
          <div className="card">
            <div className="card-hdr">
              <div className="card-title">Available to add</div>
              <div className="card-sub">from your Knowledge library</div>
            </div>
            {availableRows.map(renderRow)}
          </div>
        )}
      </div>

      {agentId && (
        <div className="col-side">
          <div className="card">
            <div className="card-hdr">
              <div className="card-title">Lookup settings</div>
              {ragEnabled && <div className="card-sub">how the agent searches your documents</div>}
            </div>
            <div className="card-body">
              {!ragEnabled ? (
                <div style={{ fontSize: ".76rem", color: "var(--text-3)", lineHeight: 1.5 }}>
                  Turn on at least one knowledge base to adjust how the agent searches it.
                </div>
              ) : (
                <>
                  <div className="form-group">
                    <label className="form-label">
                      Snippets to read <span className="hint">matching passages checked per question</span>
                    </label>
                    <input
                      className="form-input"
                      type="number"
                      min={1}
                      max={50}
                      value={policyForm.top_k}
                      onChange={(e) => setPolicyForm({ ...policyForm, top_k: Number(e.target.value) })}
                    />
                  </div>
                  <div className="form-group">
                    <label className="form-label">
                      Max text to read <span className="hint">characters per question</span>
                    </label>
                    <input
                      className="form-input"
                      type="number"
                      min={100}
                      step={100}
                      value={policyForm.max_tokens}
                      onChange={(e) => setPolicyForm({ ...policyForm, max_tokens: Number(e.target.value) })}
                    />
                  </div>
                  <div className="form-group">
                    <label className="form-label">
                      How close a match must be <span className="hint">stricter means fewer but more relevant snippets</span>
                    </label>
                    <input
                      className="form-range"
                      style={{ width: "100%" }}
                      type="range"
                      min={0}
                      max={1}
                      step={0.05}
                      value={policyForm.minimum_score}
                      onChange={(e) => setPolicyForm({ ...policyForm, minimum_score: Number(e.target.value) })}
                    />
                    <div className="form-range-row" style={{ justifyContent: "space-between" }}>
                      <span className="form-range-label">Stricter</span>
                      <span className="form-range-label">Looser</span>
                    </div>
                  </div>
                  <div className="form-group" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <label className="toggle-switch">
                      <input
                        type="checkbox"
                        checked={policyForm.include_citations}
                        onChange={(e) => setPolicyForm({ ...policyForm, include_citations: e.target.checked })}
                      />
                      <span className="toggle-slider" />
                    </label>
                    <span className="form-label" style={{ margin: 0 }}>
                      Mention where answers come from
                    </span>
                  </div>
                  <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 4 }}>
                    {policySaved && <span className="saved-note">Saved <Check size={13} /></span>}
                    <button className="btn btn-primary btn-sm" onClick={handleSavePolicy} disabled={policySaving}>
                      {policySaving ? "Saving…" : "Save"}
                    </button>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {!agentId && (
        <Modal
          open={createOpen}
          title="New Knowledge Base"
          onClose={() => setCreateOpen(false)}
          footer={
            <>
              <button className="btn btn-ghost btn-sm" onClick={() => setCreateOpen(false)}>
                Cancel
              </button>
              <button
                className="btn btn-primary btn-sm"
                onClick={handleCreateKb}
                disabled={creating || !createForm.slug || !createForm.name}
              >
                {creating ? "Creating…" : "Create"}
              </button>
            </>
          }
        >
          {createError && <div className="error-banner">{createError}</div>}
          <div className="form-group">
            <label className="form-label">
              Name <span className="required">*</span>
            </label>
            <input
              className="form-input"
              value={createForm.name}
              onChange={(e) => setCreateForm({ ...createForm, name: e.target.value })}
              placeholder="Reception FAQ"
            />
          </div>
          <div className="form-group">
            <label className="form-label">
              Slug <span className="required">*</span>
            </label>
            <input
              className="form-input"
              style={{ fontFamily: "var(--mono)" }}
              value={createForm.slug}
              onChange={(e) => setCreateForm({ ...createForm, slug: e.target.value })}
              placeholder="reception-faq"
            />
          </div>
          <div className="form-group">
            <label className="form-label">Description</label>
            <textarea
              className="form-textarea"
              value={createForm.description}
              onChange={(e) => setCreateForm({ ...createForm, description: e.target.value })}
            />
          </div>
          <div className="form-group">
            <label className="form-label">
              Search model <span className="hint">needed for anything longer than a short paragraph</span>
            </label>
            <select
              className="form-select"
              value={createForm.embedding_config_id}
              onChange={(e) => setCreateForm({ ...createForm, embedding_config_id: e.target.value })}
            >
              <option value="">None (short documents only)</option>
              {embeddingProviders.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>
        </Modal>
      )}

      {!agentId && (
        <Modal
          open={!!uploadKbId}
          title="Upload Document"
          onClose={() => setUploadKbId(null)}
          footer={
            <>
              <button className="btn btn-ghost btn-sm" onClick={() => setUploadKbId(null)}>
                Cancel
              </button>
              <button className="btn btn-primary btn-sm" onClick={handleUpload} disabled={uploading || !uploadFile}>
                {uploading ? "Uploading…" : "Upload"}
              </button>
            </>
          }
        >
          {uploadError && <div className="error-banner">{uploadError}</div>}
          <div className="form-group">
            <label className="form-label">Title</label>
            <input
              className="form-input"
              value={uploadTitle}
              onChange={(e) => setUploadTitle(e.target.value)}
              placeholder="defaults to filename"
            />
          </div>
          <div className="form-group">
            <label className="form-label">
              File <span className="hint">.txt or .md only, for now</span>
            </label>
            <input
              className="form-input"
              type="file"
              accept=".txt,.md,text/plain,text/markdown"
              onChange={(e) => setUploadFile(e.target.files?.[0] || null)}
            />
          </div>
        </Modal>
      )}
    </div>
  );
}
