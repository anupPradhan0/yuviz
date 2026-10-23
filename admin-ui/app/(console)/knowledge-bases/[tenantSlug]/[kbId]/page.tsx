"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  ArrowLeft,
  FileText,
  FolderOpen,
  Search,
  Upload,
  Users,
} from "lucide-react";
import { Modal } from "@/components/Modal";
import { FILE_STATUS } from "@/lib/knowledgePresentation";
import { useParams } from "next/navigation";
import { Agent, ApiError, listAgents, listTenants, Tenant } from "@/lib/api";
import {
  assignKnowledgeBase,
  deleteDocument,
  detachKnowledgeBase,
  KbAgent,
  KbDocument,
  KnowledgeBase,
  listDocuments,
  listKbAgents,
  listKnowledgeBases,
  retryDocument,
  updateDocument,
  uploadDocument,
} from "@/lib/knowledgeApi";
import {
  ACCEPTED_DOC_ACCEPT,
  rejectionReasonFor,
} from "@/components/AddSourceModal";

export default function KnowledgeBaseDetailPage() {
  const params = useParams<{ tenantSlug: string; kbId: string }>();
  const { tenantSlug, kbId } = params;

  const [tenant, setTenant] = useState<Tenant | null>(null);
  const [kb, setKb] = useState<KnowledgeBase | null>(null);
  const [docs, setDocs] = useState<KbDocument[]>([]);
  const [agents, setAgents] = useState<KbAgent[]>([]);
  const [tenantAgents, setTenantAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [docsError, setDocsError] = useState<string | null>(null);
  const [agentsError, setAgentsError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadFileError, setUploadFileError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [managedId, setManagedId] = useState<string | null>(null);
  const [displayName, setDisplayName] = useState("");
  const [busy, setBusy] = useState(false);
  const uploadInput = useRef<HTMLInputElement>(null);
  const replaceInput = useRef<HTMLInputElement>(null);

  const refreshAgents = () => {
    if (!kbId) return;
    return listKbAgents(kbId)
      .then((rows) => {
        setAgents(rows);
        setAgentsError(null);
      })
      .catch((e) =>
        setAgentsError(e instanceof ApiError ? e.detail : String(e)),
      );
  };

  const refreshDocs = () => {
    if (!kbId) return;
    return listDocuments(kbId)
      .then((rows) => {
        setDocs(rows);
        setDocsError(null);
      })
      .catch((e) => setDocsError(e instanceof ApiError ? e.detail : String(e)));
  };

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    setNotFound(false);
    listTenants()
      .then(async (tenants) => {
        const t = tenants.find((x) => x.slug === tenantSlug);
        if (!t) {
          if (!cancelled) setNotFound(true);
          return;
        }
        if (!cancelled) setTenant(t);
        const kbs = await listKnowledgeBases(t.id);
        const found = kbs.find((x) => x.id === kbId);
        if (cancelled) return;
        if (!found) {
          setNotFound(true);
          return;
        }
        setKb(found);
        await Promise.all([
          refreshDocs(),
          refreshAgents(),
          listAgents(t.slug)
            .then(setTenantAgents)
            .catch((e) =>
              setAgentsError(e instanceof ApiError ? e.detail : String(e)),
            ),
        ]);
      })
      .catch(() => setNotFound(true))
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantSlug, kbId]);

  useEffect(() => {
    if (
      !docs.some(
        (doc) => doc.status === "pending" || doc.status === "processing",
      )
    )
      return;
    const timer = setTimeout(refreshDocs, 4000);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docs, kbId]);

  const handleDeleteDoc = async (doc: KbDocument) => {
    try {
      const affected = await listKbAgents(kbId);
      const names = affected.map((a) => a.agent_name).join(", ");
      if (
        !confirm(
          `Remove file "${doc.title}" permanently?${names ? ` Agents linked to this folder will lose access to it: ${names}.` : " No agents use this folder."}`,
        )
      )
        return;
    } catch {
      setActionError(
        "Couldn’t check which agents use this file. Please try again before removing it.",
      );
      return;
    }
    try {
      await deleteDocument(doc.id);
      setManagedId(null);
      await refreshDocs();
    } catch (e) {
      setActionError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const handleUploadFileChange = (f: File | null) => {
    setUploadFile(null);
    setUploadFileError(null);
    if (!f) return;
    const reason = rejectionReasonFor(f);
    if (reason) {
      setUploadFileError(reason);
      return;
    }
    setUploadFile(f);
  };

  const handleUpload = async () => {
    if (!uploadFile || !kbId || uploading) return;
    setUploading(true);
    setActionError(null);
    try {
      await uploadDocument(kbId, uploadFile, uploadFile.name);
      setUploadFile(null);
      setUploadOpen(false);
      await refreshDocs();
    } catch (e) {
      setActionError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setUploading(false);
    }
  };

  const handleAttach = async (agentId: string) => {
    if (!kbId) return;
    try {
      await assignKnowledgeBase(agentId, kbId);
      refreshAgents();
    } catch (e) {
      setActionError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const handleDetach = async (agent: KbAgent) => {
    if (!kbId) return;
    if (
      !confirm(
        `Turn off and remove this folder from "${agent.agent_name}"? The agent will stop using its files. The files stay in Knowledge.`,
      )
    )
      return;
    try {
      await detachKnowledgeBase(agent.agent_id, kbId);
      refreshAgents();
    } catch (e) {
      setActionError(e instanceof ApiError ? e.detail : String(e));
    }
  };

  const managed = docs.find((doc) => doc.id === managedId);
  const runFileAction = async (action: () => Promise<unknown>) => {
    if (busy) return;
    setBusy(true);
    setActionError(null);
    try {
      await action();
      await refreshDocs();
    } catch (e) {
      setActionError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusy(false);
    }
  };

  const replaceFile = async (file: File | null) => {
    if (!file || !managed) return;
    const reason = rejectionReasonFor(file);
    if (reason) {
      setActionError(reason);
      return;
    }
    await runFileAction(async () => {
      const fresh = await uploadDocument(kbId, file, managed.title, {
        language: managed.language,
        tags: managed.tags,
      });
      if (managed.usage_mode !== "auto")
        await updateDocument(fresh.id, { usage_mode: managed.usage_mode });
      try {
        await deleteDocument(managed.id);
      } catch {
        throw new Error(
          "The replacement was uploaded, but the old file could not be removed. Both copies are in this folder; remove the old one when you’re ready.",
        );
      }
      setManagedId(null);
    });
  };

  const docTypeLabel = (contentType: string) => {
    if (contentType === "text/markdown") return "Markdown";
    if (contentType === "text/plain") return "Text";
    if (contentType === "application/pdf") return "PDF";
    if (contentType.endsWith("wordprocessingml.document")) return "Word";
    if (contentType.endsWith("presentationml.presentation"))
      return "PowerPoint";
    if (contentType.endsWith("spreadsheetml.sheet")) return "Excel";
    if (contentType === "text/html") return "HTML";
    if (
      contentType === "text/csv" ||
      contentType === "text/tab-separated-values"
    )
      return "CSV";
    return contentType || "Unknown";
  };

  if (loading) return <div className="empty-state">Loading…</div>;
  if (notFound || !kb || !tenant)
    return <div className="empty-state">Folder not found.</div>;

  const attachedAgentIds = new Set(agents.map((a) => a.agent_id));
  const unattachedAgents = tenantAgents.filter(
    (a) => !attachedAgentIds.has(a.id),
  );

  const filteredDocs = docs.filter((doc) =>
    doc.title.toLowerCase().includes(search.trim().toLowerCase()),
  );
  const enabledAgents = agents.filter((agent) => agent.enabled);

  return (
    <div className="knowledge-folder-page">
      <Link className="knowledge-folder-back" href="/knowledge-bases">
        <ArrowLeft size={14} /> All files
      </Link>
      <div className="knowledge-heading knowledge-folder-heading">
        <div className="knowledge-folder-title">
          <span className="knowledge-folder-icon">
            <FolderOpen size={22} />
          </span>
          <div>
            <p className="knowledge-folder-eyebrow">
              Knowledge folder · {tenant.name}
            </p>
            <h1>{kb.name}</h1>
            <p>
              {kb.description ||
                "Keep related files together so your agents can answer from them."}
            </p>
          </div>
        </div>
      </div>
      <div className="knowledge-folder-summary">
        <span>
          <FileText size={14} /> {docs.length}{" "}
          {docs.length === 1 ? "file" : "files"}
        </span>
        <span>
          <span className="knowledge-ready-dot" />{" "}
          {docs.filter((doc) => doc.status === "ready").length} ready for agents
        </span>
        <span>
          <Users size={14} /> {enabledAgents.length}{" "}
          {enabledAgents.length === 1 ? "agent uses" : "agents use"} this folder
        </span>
      </div>
      {actionError && !managed && !uploadOpen && (
        <div className="error-banner" role="alert">
          {actionError}
        </div>
      )}

      <div className="card knowledge-card">
        <div className="card-hdr">
          <h2 className="card-title">Your files</h2>
          <span className="card-sub">
            {docs.length} {docs.length === 1 ? "file" : "files"}
          </span>
          <label className="knowledge-search">
            <Search size={14} />
            <input
              aria-label="Search files in folder"
              placeholder="Search files…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
          <button
            className="btn btn-ghost btn-sm"
            onClick={() => {
              setActionError(null);
              setUploadFileError(null);
              setUploadFile(null);
              setUploadOpen(true);
            }}
          >
            <Upload size={14} /> Upload file
          </button>
        </div>
        {docsError && (
          <div className="error-banner" role="alert">
            {docsError}
          </div>
        )}
        {docs.length === 0 ? (
          <div className="empty-state knowledge-empty">
            <FileText size={28} />
            <h3>No files in this folder yet</h3>
            <p>Upload a file so your agents can answer from your docs.</p>
          </div>
        ) : filteredDocs.length === 0 ? (
          <div className="empty-state">No files match your search.</div>
        ) : (
          <div className="knowledge-table-wrap">
            <table className="tbl">
              <thead>
                <tr>
                  <th>File</th>
                  <th>Status</th>
                  <th>Last updated</th>
                  <th>Reading preference</th>
                  <th>Manage</th>
                </tr>
              </thead>
              <tbody>
                {filteredDocs.map((doc) => (
                  <tr key={doc.id}>
                    <td>
                      <div className="knowledge-folder-file">
                        <FileText size={17} />
                        <div>
                          <div className="bold">{doc.title}</div>
                          <div className="kb-source-sub">
                            {docTypeLabel(doc.content_type)}
                            {doc.byte_size != null
                              ? ` · ${(doc.byte_size / 1024).toFixed(1)} KB`
                              : ""}
                          </div>
                        </div>
                      </div>
                    </td>
                    <td>
                      <span
                        className={`badge ${FILE_STATUS[doc.status].color}`}
                      >
                        {FILE_STATUS[doc.status].label}
                      </span>
                    </td>
                    <td className="kb-source-sub">
                      {new Date(
                        doc.updated_at || doc.created_at,
                      ).toLocaleDateString()}
                    </td>
                    <td className="kb-source-sub">
                      {doc.usage_mode === "prompt"
                        ? "Every question"
                        : "When relevant"}
                    </td>
                    <td>
                      <button
                        className="btn btn-ghost btn-sm"
                        onClick={() => {
                          setActionError(null);
                          setManagedId(doc.id);
                          setDisplayName(doc.title);
                        }}
                      >
                        Manage
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card knowledge-card knowledge-folder-agents">
        <div className="card-hdr">
          <h2 className="card-title">Agents using this folder</h2>
          <span className="card-sub">
            Turn this folder on in an agent’s Knowledge settings.
          </span>
          {unattachedAgents.length > 0 && (
            <select
              aria-label="Add agent to folder"
              className="form-select knowledge-folder-agent-select"
              defaultValue=""
              onChange={(e) => {
                if (e.target.value) handleAttach(e.target.value);
                e.target.value = "";
              }}
            >
              <option value="">+ Add agent…</option>
              {unattachedAgents.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          )}
        </div>
        {agentsError && (
          <div className="error-banner" role="alert">
            {agentsError}
          </div>
        )}
        {agents.length === 0 ? (
          <div className="empty-state knowledge-empty">
            <Users size={25} />
            <h3>No agents use this folder yet</h3>
            <p>Open an agent → Knowledge → turn this folder on.</p>
            <Link className="btn btn-ghost btn-sm" href="/agents">
              Go to agents
            </Link>
          </div>
        ) : (
          agents.map((a) => (
            <div key={a.agent_id} className="kb-row knowledge-folder-agent-row">
              <span className="knowledge-folder-agent-icon">
                <Users size={16} />
              </span>
              <div className="knowledge-folder-agent-name">
                <Link
                  href={`/agents/${tenantSlug}/${a.agent_slug}?section=knowledge`}
                >
                  {a.agent_name}
                </Link>
                <div className="kb-source-sub">
                  {a.enabled
                    ? "Can answer from this folder"
                    : "This folder is turned off"}
                </div>
              </div>
              <span className={`badge ${a.enabled ? "green" : "gray"}`}>
                {a.enabled ? "On" : "Off"}
              </span>
              <button
                className="btn btn-ghost btn-sm"
                onClick={() => handleDetach(a)}
              >
                Remove from agent
              </button>
            </div>
          ))
        )}
      </div>

      <Modal
        open={uploadOpen}
        title="Upload a file"
        trapFocus
        className="knowledge-dialog"
        onClose={() => {
          if (!uploading) setUploadOpen(false);
        }}
        footer={
          <>
            <button
              className="btn btn-ghost btn-sm"
              disabled={uploading}
              onClick={() => setUploadOpen(false)}
            >
              Cancel
            </button>
            <button
              className="btn btn-primary btn-sm"
              disabled={!uploadFile || uploading}
              onClick={handleUpload}
            >
              {uploading ? "Uploading…" : "Upload file"}
            </button>
          </>
        }
      >
        <p className="knowledge-upload-folder">
          Adding to <strong>{kb.name}</strong>
        </p>
        {actionError && (
          <div className="error-banner" role="alert">
            {actionError}
          </div>
        )}
        <div
          className="knowledge-drop"
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault();
            if (!uploading)
              handleUploadFileChange(e.dataTransfer.files[0] ?? null);
          }}
        >
          <Upload size={24} />
          <p>Drop a file here</p>
          <button
            className="btn btn-ghost btn-sm"
            disabled={uploading}
            onClick={() => uploadInput.current?.click()}
          >
            {uploadFile ? "Choose another file" : "Choose a file"}
          </button>
          <input
            ref={uploadInput}
            type="file"
            hidden
            accept={ACCEPTED_DOC_ACCEPT}
            onChange={(e) => {
              handleUploadFileChange(e.target.files?.[0] ?? null);
              e.target.value = "";
            }}
          />
          {uploadFile && (
            <div className="knowledge-selected-file">{uploadFile.name}</div>
          )}
          <p className="form-hint">
            Text, Markdown, HTML, CSV or TSV. PDF, Word, Excel and PowerPoint
            when enabled for your account.
          </p>
        </div>
        {uploadFileError && (
          <div className="error-banner" role="alert">
            {uploadFileError}
          </div>
        )}
        <p className="form-hint knowledge-handoff">
          We’ll prepare your file and show when it’s ready.
        </p>
      </Modal>

      <Modal
        open={!!managed}
        title={managed?.title ?? "Manage file"}
        trapFocus
        className="knowledge-dialog knowledge-manage-dialog"
        onClose={() => {
          if (!busy) setManagedId(null);
        }}
        footer={
          <>
            <button
              className="btn btn-ghost btn-sm"
              disabled={busy}
              onClick={() => setManagedId(null)}
            >
              Close
            </button>
            <button
              className="btn btn-primary btn-sm"
              disabled={busy || !displayName.trim()}
              onClick={() =>
                runFileAction(async () => {
                  if (managed)
                    await updateDocument(managed.id, {
                      title: displayName.trim(),
                    });
                  setManagedId(null);
                })
              }
            >
              {busy ? "Working…" : "Save"}
            </button>
          </>
        }
      >
        {managed && (
          <>
            {actionError && (
              <div className="error-banner" role="alert">
                {actionError}
              </div>
            )}
            <p className="knowledge-upload-folder">
              {kb.name} ·{" "}
              <span className={`badge ${FILE_STATUS[managed.status].color}`}>
                {FILE_STATUS[managed.status].label}
              </span>
            </p>
            <label className="form-label" htmlFor="folder-file-name">
              Display name
            </label>
            <input
              id="folder-file-name"
              className="form-input"
              value={displayName}
              disabled={busy}
              onChange={(e) => setDisplayName(e.target.value)}
            />
            <label className="knowledge-reading-option">
              <input
                type="checkbox"
                checked={managed.usage_mode === "prompt"}
                disabled={
                  busy ||
                  (managed.status !== "ready" &&
                    managed.status !== "ready_degraded")
                }
                onChange={() =>
                  runFileAction(() =>
                    updateDocument(managed.id, {
                      usage_mode:
                        managed.usage_mode === "prompt" ? "auto" : "prompt",
                    }),
                  )
                }
              />
              <span>
                Read for every question
                <small>
                  Otherwise, agents read this file when it’s relevant to the
                  caller’s question.
                </small>
              </span>
            </label>
            {(managed.status === "failed" ||
              managed.status === "ready_degraded") && (
              <p className="knowledge-file-warning">
                {managed.status === "failed"
                  ? "We couldn’t prepare this file. Replace it or try again when available."
                  : "Some of this file may be unreadable. Check the original and replace it if needed."}
              </p>
            )}
            <div className="knowledge-manage-actions">
              <button
                className="btn btn-ghost btn-sm"
                disabled={
                  busy ||
                  managed.status === "pending" ||
                  managed.status === "processing"
                }
                onClick={() => replaceInput.current?.click()}
              >
                Replace file
              </button>
              {managed.retryable && (
                <button
                  className="btn btn-ghost btn-sm"
                  disabled={busy}
                  onClick={() => runFileAction(() => retryDocument(managed.id))}
                >
                  Try again
                </button>
              )}
              <button
                className="btn btn-danger btn-sm"
                disabled={busy}
                onClick={() => runFileAction(() => handleDeleteDoc(managed))}
              >
                Remove file
              </button>
            </div>
          </>
        )}
      </Modal>
      <input
        ref={replaceInput}
        type="file"
        accept={ACCEPTED_DOC_ACCEPT}
        hidden
        onChange={(e) => {
          const file = e.target.files?.[0] ?? null;
          e.target.value = "";
          void replaceFile(file);
        }}
      />
    </div>
  );
}
