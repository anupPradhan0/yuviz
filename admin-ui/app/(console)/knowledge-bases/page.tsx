"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import {
  ArrowUpRight,
  FileText,
  FolderOpen,
  Plus,
  RefreshCw,
  BookOpen,
  Search,
  Users,
} from "lucide-react";
import { ApiError, getCurrentUser } from "@/lib/api";
import { useActiveTenant } from "@/lib/useActiveTenant";
import {
  KbAgent,
  KbDocument,
  KnowledgeBase,
  deleteDocument,
  deleteKnowledgeBase,
  listDocuments,
  listKbAgents,
  listKnowledgeBases,
  retryDocument,
  updateDocument,
  uploadDocument,
} from "@/lib/knowledgeApi";
import { FILE_STATUS } from "@/lib/knowledgePresentation";
import {
  AddSourceModal,
  ACCEPTED_DOC_ACCEPT,
  rejectionReasonFor,
} from "@/components/AddSourceModal";
import { Modal } from "@/components/Modal";

interface KbRow extends KnowledgeBase {
  tenantName: string;
  tenantSlug: string;
}

interface SourceRow extends KbDocument {
  kbName: string;
  tenantName: string;
  // null when the agent lookup failed: unknown, not "unused".
  agents: KbAgent[] | null;
}

function timeAgo(iso: string): string {
  const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function sourceType(doc: KbDocument): string {
  const ct = (doc.content_type || "").toLowerCase();
  if (ct.includes("pdf")) return "PDF";
  if (ct.includes("markdown") || ct.includes("md")) return "Markdown";
  if (ct.includes("csv")) return "CSV";
  if (ct.includes("plain") || ct.includes("text")) return "Text";
  if (ct.includes("word") || ct.includes("docx")) return "Word";
  if (ct.includes("presentation")) return "PowerPoint";
  if (ct.includes("spreadsheet")) return "Excel";
  return "File";
}

export default function KnowledgeBasesPage() {
  const {
    tenant,
    allTenants,
    isAllTenants,
    loading: tenantLoading,
    error: accountError,
  } = useActiveTenant();
  const [kbs, setKbs] = useState<KbRow[]>([]);
  const [sources, setSources] = useState<SourceRow[]>([]);
  const [emptyKbs, setEmptyKbs] = useState<KbRow[]>([]);
  const [removingKb, setRemovingKb] = useState<string | null>(null);
  const [removingSource, setRemovingSource] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [managedId, setManagedId] = useState<string | null>(null);
  const [displayName, setDisplayName] = useState("");
  const [saving, setSaving] = useState(false);
  const [tenantErrors, setTenantErrors] = useState<string[]>([]);
  const [canManage, setCanManage] = useState(false);
  const [loading, setLoading] = useState(true);
  const [pageError, setPageError] = useState<string | null>(null);
  const [addSourceOpen, setAddSourceOpen] = useState(false);
  const searchParams = useSearchParams();
  const router = useRouter();

  const [deleteTarget, setDeleteTarget] = useState<SourceRow | null>(null);

  const [retryingSource, setRetryingSource] = useState<string | null>(null);
  const [replacingSource, setReplacingSource] = useState<string | null>(null);
  const replaceInputRef = useRef<HTMLInputElement>(null);
  const pendingReplaceId = useRef<string | null>(null);
  const refreshId = useRef<symbol | null>(null);

  const refresh = async (background = false) => {
    const requestId = Symbol();
    refreshId.current = requestId;
    if (!background) setLoading(true);
    setPageError(null);
    setTenantErrors([]);
    // Each tenant's fetch fails independently below (Promise.allSettled).
    const fetchedTenants = isAllTenants ? allTenants : tenant ? [tenant] : [];
    if (fetchedTenants.length === 0) {
      setKbs([]);
      setSources([]);
      setEmptyKbs([]);
      setLoading(false);
      return;
    }
    try {
      const kbResults = await Promise.allSettled(
        fetchedTenants.map((t) => listKnowledgeBases(t.id)),
      );
      if (requestId !== refreshId.current) return;
      const nextKbs: KbRow[] = [];
      const nextTenantErrors: string[] = [];
      kbResults.forEach((result, i) => {
        const t = fetchedTenants[i];
        if (result.status === "fulfilled") {
          nextKbs.push(
            ...result.value.map((kb) => ({
              ...kb,
              tenantName: t.name,
              tenantSlug: t.slug,
            })),
          );
        } else {
          const reason = result.reason;
          nextTenantErrors.push(
            `Couldn't load folders for ${t.name}: ${reason instanceof ApiError ? reason.detail : String(reason)}`,
          );
        }
      });

      const [docResults, agentResults] = await Promise.all([
        Promise.allSettled(nextKbs.map((kb) => listDocuments(kb.id))),
        Promise.allSettled(nextKbs.map((kb) => listKbAgents(kb.id))),
      ]);

      if (requestId !== refreshId.current) return;
      setKbs(nextKbs);
      const nextSources: SourceRow[] = [];
      docResults.forEach((result, i) => {
        if (result.status !== "fulfilled") {
          nextTenantErrors.push(
            `Couldn’t load files in ${nextKbs[i].name}. Please refresh to try again.`,
          );
          return;
        }
        const kb = nextKbs[i];
        const agents =
          agentResults[i].status === "fulfilled"
            ? (agentResults[i] as PromiseFulfilledResult<KbAgent[]>).value
            : null;
        nextSources.push(
          ...result.value.map((d) => ({
            ...d,
            kbName: kb.name,
            tenantName: kb.tenantName,
            agents,
          })),
        );
      });
      setSources(nextSources);
      // Empty KBs (usually failed uploads) are listed so they can be removed.
      setEmptyKbs(
        nextKbs.filter(
          (_, i) =>
            docResults[i].status === "fulfilled" &&
            (docResults[i] as PromiseFulfilledResult<KbDocument[]>).value
              .length === 0,
        ),
      );

      setTenantErrors(nextTenantErrors);
    } catch (e) {
      if (requestId === refreshId.current)
        setPageError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      if (requestId === refreshId.current) setLoading(false);
    }
  };

  useEffect(() => {
    if (tenantLoading) return;
    setManagedId(null);
    setDeleteTarget(null);
    setAddSourceOpen(false);
    refresh();
    getCurrentUser()
      .then((me) =>
        setCanManage(me.role === "superadmin" || me.role === "admin"),
      )
      .catch(() => {
        // Leave canManage false — same "fail closed on write controls" as
        // every other page's canManage check.
      });
    return () => {
      refreshId.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenant, allTenants, isAllTenants, tenantLoading]);

  const preparing = sources.some(
    (s) => s.status === "pending" || s.status === "processing",
  );
  useEffect(() => {
    if (!preparing) return;
    const timer = setTimeout(() => {
      void refresh(true);
    }, 4000);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preparing, sources, tenant, allTenants, isAllTenants]);

  useEffect(() => {
    const id = searchParams.get("manage");
    const file = sources.find((s) => s.id === id);
    if (!file) return;
    setManagedId(file.id);
    setDisplayName(file.title);
    router.replace("/knowledge-bases");
  }, [searchParams, sources, router]);

  const filtered = sources.filter((s) =>
    `${s.title} ${s.kbName} ${s.tenantName}`
      .toLowerCase()
      .includes(search.trim().toLowerCase()),
  );
  const managed = sources.find((s) => s.id === managedId);
  const busy =
    saving || !!retryingSource || !!replacingSource || !!removingSource;
  const folderHref = (kbId: string) => {
    const kb = kbs.find((kb) => kb.id === kbId);
    return kb
      ? `/knowledge-bases/${kb.tenantSlug}/${kb.id}`
      : "/knowledge-bases";
  };
  const saveName = async () => {
    if (!managed || !displayName.trim() || busy) return;
    setSaving(true);
    try {
      await updateDocument(managed.id, { title: displayName.trim() });
      await refresh(true);
      setManagedId(null);
    } catch (e) {
      setTenantErrors((errs) => [
        ...errs,
        e instanceof ApiError ? e.detail : String(e),
      ]);
    } finally {
      setSaving(false);
    }
  };

  const removeEmptyKb = async (kb: KbRow) => {
    try {
      const agents = await listKbAgents(kb.id);
      const affected = agents.length
        ? ` Agents linked to this folder: ${agents.map((a) => a.agent_name).join(", ")}.`
        : "";
      if (
        !window.confirm(
          `Remove the empty folder "${kb.name}"?${affected} This cannot be undone.`,
        )
      )
        return;
    } catch {
      setTenantErrors((errs) => [
        ...errs,
        "Couldn’t check which agents use this folder. Please try again before removing it.",
      ]);
      return;
    }
    setRemovingKb(kb.id);
    try {
      await deleteKnowledgeBase(kb.id);
      await refresh();
    } catch (e) {
      setTenantErrors((errs) => [
        ...errs,
        e instanceof ApiError ? e.detail : String(e),
      ]);
    } finally {
      setRemovingKb(null);
    }
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    const id = deleteTarget.id;
    setDeleteTarget(null);
    setManagedId(null);
    setRemovingSource(id);
    try {
      await deleteDocument(id);
      await refresh();
    } catch (e) {
      setTenantErrors((errs) => [
        ...errs,
        e instanceof ApiError ? e.detail : String(e),
      ]);
    } finally {
      setRemovingSource(null);
    }
  };

  const handleRetry = async (source: SourceRow) => {
    setRetryingSource(source.id);
    try {
      await retryDocument(source.id);
      await refresh();
    } catch (e) {
      setTenantErrors((errs) => [
        ...errs,
        e instanceof ApiError ? e.detail : String(e),
      ]);
    } finally {
      setRetryingSource(null);
    }
  };

  const handleReplaceClick = (source: SourceRow) => {
    pendingReplaceId.current = source.id;
    replaceInputRef.current?.click();
  };

  const handleReplaceFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    const docId = pendingReplaceId.current;
    if (!file || !docId) return;
    e.target.value = "";
    const reason = rejectionReasonFor(file);
    if (reason) {
      setTenantErrors((errs) => [...errs, reason]);
      return;
    }
    const source = sources.find((s) => s.id === docId);
    if (!source) return;
    setReplacingSource(docId);
    const message = (e: unknown) =>
      e instanceof ApiError ? e.detail : String(e);
    let failure: string | null = null;
    try {
      const fresh = await uploadDocument(source.kb_id, file, source.title, {
        language: source.language,
        tags: source.tags,
      });
      if (source.usage_mode !== "auto")
        await updateDocument(fresh.id, { usage_mode: source.usage_mode });
      try {
        await deleteDocument(docId);
      } catch (e) {
        failure = `New file uploaded, but the old copy of "${source.title}" could not be removed: ${message(e)}`;
      }
    } catch (e) {
      failure = message(e);
    }
    await refresh();
    if (failure) setTenantErrors((errs) => [...errs, failure!]);
    setReplacingSource(null);
    pendingReplaceId.current = null;
  };

  return (
    <div className="knowledge-library">
      <div className="knowledge-heading">
        <div>
          <h1>Knowledge</h1>
          <p>
            Files your agents use to answer questions — FAQs, prices, policies.
          </p>
        </div>
      </div>
      {(pageError || accountError) && (
        <div className="error-banner" role="alert">
          {pageError || accountError}
        </div>
      )}
      {tenantErrors.map((msg, i) => (
        <div key={i} className="error-banner" role="alert">
          {msg}
        </div>
      ))}
      <div className="card knowledge-card">
        <div className="card-hdr">
          <h2 className="card-title">Your files</h2>
          <span className="card-sub">
            {loading || tenantLoading ? (
              "Loading…"
            ) : (
              <>
                {sources.length} {sources.length === 1 ? "file" : "files"}
                {" · "}
                {kbs.length} {kbs.length === 1 ? "folder" : "folders"}
              </>
            )}
          </span>
          <label className="knowledge-search">
            <Search size={14} />
            <input
              aria-label="Search files"
              placeholder="Search files…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </label>
          <button
            className="btn btn-ghost btn-sm"
            disabled={loading}
            onClick={() => refresh()}
          >
            <RefreshCw
              size={13}
              className={loading ? "knowledge-refreshing" : undefined}
            />{" "}
            Refresh
          </button>
        </div>
        {loading || tenantLoading ? (
          <div className="empty-state">Loading files…</div>
        ) : sources.length === 0 ? (
          <div className="empty-state knowledge-empty">
            <FileText size={28} />
            <h3>No files yet</h3>
            <p>Add a file so your agents can answer from your docs.</p>
            {canManage && (
              <button
                className="btn btn-primary btn-sm"
                onClick={() => setAddSourceOpen(true)}
                disabled={!allTenants.length}
              >
                <Plus size={14} /> Add file
              </button>
            )}
          </div>
        ) : filtered.length === 0 ? (
          <div className="empty-state">No files match your search.</div>
        ) : (
          <div className="knowledge-table-wrap">
            <table className="tbl" role="table" aria-label="Your files">
              <thead role="rowgroup">
                <tr role="row">
                  <th scope="col" role="columnheader">
                    File
                  </th>
                  <th scope="col" role="columnheader">
                    Folder
                  </th>
                  <th scope="col" role="columnheader">
                    Status
                  </th>
                  <th scope="col" role="columnheader">
                    Used by
                  </th>
                  <th scope="col" role="columnheader">
                    Added
                  </th>
                  <th scope="col" role="columnheader">
                    <span className="sr-only">Manage</span>
                  </th>
                </tr>
              </thead>
              <tbody role="rowgroup">
                {filtered.map((s) => {
                  const agents = s.agents?.filter((a) => a.enabled);
                  return (
                    <tr key={s.id} role="row">
                      <td role="cell" data-label="File">
                        <div className="knowledge-library-file">
                          <span className="knowledge-library-file-icon">
                            <FileText size={18} />
                          </span>
                          <div>
                            <div className="bold">{s.title}</div>
                            <div className="kb-source-sub">
                              {sourceType(s)}
                              {s.byte_size != null
                                ? ` · ${(s.byte_size / 1024).toFixed(1)} KB`
                                : ""}
                            </div>
                          </div>
                        </div>
                      </td>
                      <td role="cell" data-label="Folder">
                        <Link
                          className="knowledge-library-folder"
                          href={folderHref(s.kb_id)}
                        >
                          <FolderOpen size={13} />
                          {s.kbName}
                        </Link>
                        {isAllTenants && (
                          <div className="kb-source-sub">{s.tenantName}</div>
                        )}
                      </td>
                      <td role="cell" data-label="Status">
                        <span
                          className={`badge ${FILE_STATUS[s.status].color}`}
                        >
                          {FILE_STATUS[s.status].label}
                        </span>
                      </td>
                      <td role="cell" data-label="Used by">
                        {agents == null ? (
                          <span className="knowledge-library-muted">
                            Couldn’t check
                          </span>
                        ) : agents.length ? (
                          <span
                            className="knowledge-library-used"
                            title={agents.map((a) => a.agent_name).join(", ")}
                          >
                            <Users size={13} />
                            <span>
                              {agents.length === 1
                                ? agents[0].agent_name
                                : `${agents.length} agents`}
                            </span>
                          </span>
                        ) : (
                          <span className="knowledge-library-muted">
                            Not used yet
                          </span>
                        )}
                      </td>
                      <td
                        className="kb-source-sub"
                        role="cell"
                        data-label="Added"
                      >
                        {timeAgo(s.created_at)}
                      </td>
                      <td role="cell" data-label="Manage">
                        <button
                          className="btn btn-ghost btn-sm"
                          disabled={removingSource === s.id}
                          onClick={() => {
                            setManagedId(s.id);
                            setDisplayName(s.title);
                          }}
                        >
                          Manage
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {emptyKbs.length > 0 && (
        <details className="knowledge-empty-folders">
          <summary>Empty folders ({emptyKbs.length})</summary>
          {emptyKbs.map((kb) => (
            <div className="kb-row" key={kb.id}>
              <div style={{ flex: 1 }}>
                <Link href={folderHref(kb.id)}>{kb.name}</Link>
                <div className="kb-source-sub">
                  No files yet{isAllTenants ? ` · ${kb.tenantName}` : ""}
                </div>
              </div>
              {canManage && (
                <button
                  className="btn btn-danger btn-sm"
                  disabled={removingKb === kb.id}
                  onClick={() => removeEmptyKb(kb)}
                >
                  {removingKb === kb.id ? "Removing…" : "Remove folder"}
                </button>
              )}
            </div>
          ))}
        </details>
      )}
      <div className="knowledge-library-guide">
        <span className="knowledge-library-guide-icon">
          <BookOpen size={18} />
        </span>
        <div>
          <strong>Put your knowledge to work</strong>
          <p>
            Open an agent → Knowledge → turn a folder on so it can answer from
            your files.
          </p>
        </div>
        <Link href="/agents">
          Go to agents <ArrowUpRight size={14} />
        </Link>
      </div>

      <Modal
        open={!!managed && !deleteTarget}
        title={managed?.title ?? "Manage file"}
        onClose={() => {
          if (!busy) setManagedId(null);
        }}
        trapFocus
        className="knowledge-dialog knowledge-manage-dialog"
        footer={
          <>
            <button
              className="btn btn-ghost btn-sm"
              disabled={busy}
              onClick={() => setManagedId(null)}
            >
              Close
            </button>
            {canManage && (
              <button
                className="btn btn-primary btn-sm"
                disabled={busy || !displayName.trim()}
                onClick={saveName}
              >
                {saving ? "Saving…" : "Save"}
              </button>
            )}
          </>
        }
      >
        {managed && (
          <>
            {tenantErrors.map((message, i) => (
              <div key={i} className="error-banner" role="alert">
                {message}
              </div>
            ))}
            <div className="knowledge-manage-meta">
              <div className="knowledge-manage-folder">
                <FolderOpen size={17} />
                <div>
                  <span>Folder</span>
                  <Link href={folderHref(managed.kb_id)}>{managed.kbName}</Link>
                </div>
              </div>
              <span className={`badge ${FILE_STATUS[managed.status].color}`}>
                {FILE_STATUS[managed.status].label}
              </span>
            </div>
            <div className="knowledge-manage-section">
              <label className="form-label" htmlFor="file-display-name">
                Display name
              </label>
              <input
                id="file-display-name"
                className="form-input"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                disabled={!canManage || busy}
              />
            </div>
            <div className="knowledge-manage-section">
              <h3 className="knowledge-manage-label">Used by</h3>
              <div className="knowledge-manage-agents">
                {managed.agents === null ? (
                  <p>Couldn’t check which agents use this folder.</p>
                ) : managed.agents.some((a) => a.enabled) ? (
                  managed.agents
                    .filter((a) => a.enabled)
                    .map((a) => {
                      const kb = kbs.find((kb) => kb.id === managed.kb_id);
                      return (
                        <Link
                          className="knowledge-manage-agent"
                          key={a.agent_id}
                          href={`/agents/${kb?.tenantSlug}/${a.agent_slug}?section=knowledge`}
                        >
                          <span className="knowledge-manage-agent-icon">
                            <Users size={15} />
                          </span>
                          <span>{a.agent_name}</span>
                          <ArrowUpRight size={14} />
                        </Link>
                      );
                    })
                ) : (
                  <p>No agents use this file yet.</p>
                )}
              </div>
              <p className="knowledge-manage-hint">
                Turn this folder on or off in the agent’s Knowledge settings.
              </p>
              <Link
                className="knowledge-manage-folder-link"
                href={folderHref(managed.kb_id)}
              >
                Manage this folder’s files and agents <ArrowUpRight size={13} />
              </Link>
            </div>
            {(managed.status === "pending" ||
              managed.status === "processing") && (
              <p className="knowledge-manage-preparing">
                We’re preparing this file. You can replace it once preparation
                finishes.
              </p>
            )}
            {(managed.status === "failed" ||
              managed.status === "ready_degraded") && (
              <p className="knowledge-file-warning">
                {managed.status === "failed"
                  ? "We couldn’t prepare this file. Replace it, or try again when available."
                  : "Some of this file may be unreadable. Check the original and replace it if needed."}
              </p>
            )}
            {canManage && (
              <div className="knowledge-manage-file-actions">
                <h3 className="knowledge-manage-label">File actions</h3>
                <div className="knowledge-manage-actions">
                  <button
                    className="btn btn-ghost btn-sm"
                    disabled={
                      busy ||
                      managed.status === "pending" ||
                      managed.status === "processing"
                    }
                    onClick={() => handleReplaceClick(managed)}
                  >
                    {replacingSource ? "Replacing…" : "Replace file"}
                  </button>
                  {managed.retryable && (
                    <button
                      className="btn btn-ghost btn-sm"
                      disabled={busy}
                      onClick={() => handleRetry(managed)}
                    >
                      {retryingSource ? "Trying again…" : "Try again"}
                    </button>
                  )}
                  <button
                    className="btn btn-danger btn-sm"
                    disabled={busy}
                    onClick={() => setDeleteTarget(managed)}
                  >
                    Remove
                  </button>
                </div>
              </div>
            )}
          </>
        )}
      </Modal>

      <input
        ref={replaceInputRef}
        type="file"
        accept={ACCEPTED_DOC_ACCEPT}
        hidden
        onChange={handleReplaceFile}
      />

      {(addSourceOpen || (canManage && searchParams.get("add") === "1")) && (
        <AddSourceModal
          tenants={allTenants}
          defaultTenantId={tenant?.id}
          knowledgeBases={kbs}
          canManage={canManage}
          onClose={() => {
            setAddSourceOpen(false);
            if (searchParams.has("add")) router.replace("/knowledge-bases");
          }}
          onCreated={refresh}
        />
      )}

      <Modal
        open={!!deleteTarget}
        trapFocus
        className="knowledge-dialog"
        title={`Remove "${deleteTarget?.title}"?`}
        onClose={() => setDeleteTarget(null)}
        footer={
          <>
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => setDeleteTarget(null)}
            >
              Cancel
            </button>
            <button className="btn btn-danger btn-sm" onClick={confirmDelete}>
              Remove file
            </button>
          </>
        }
      >
        {deleteTarget && (
          <div
            style={{
              fontSize: ".82rem",
              color: "var(--text-2)",
              lineHeight: 1.6,
            }}
          >
            {deleteTarget.agents === null ? (
              <p style={{ margin: 0 }}>
                Couldn&apos;t check which agents use this file. Any agent using
                it will lose it permanently.
              </p>
            ) : deleteTarget.agents.length === 0 ? (
              <p style={{ margin: 0 }}>
                This file is not currently used by any agents. Removing it will
                remove it from the folder permanently.
              </p>
            ) : (
              <>
                <p style={{ margin: "0 0 10px" }}>
                  This file is currently used by{" "}
                  <strong>
                    {deleteTarget.agents.length} agent
                    {deleteTarget.agents.length !== 1 ? "s" : ""}
                  </strong>
                  . Removing it will remove it from{" "}
                  {deleteTarget.agents.length === 1
                    ? "that agent"
                    : "all of them"}{" "}
                  permanently.
                </p>
                <div
                  style={{
                    borderLeft: "2px solid var(--red-border)",
                    padding: "6px 0 6px 10px",
                    display: "flex",
                    flexDirection: "column",
                    gap: 4,
                  }}
                >
                  <div style={{ fontWeight: 600, marginBottom: 2 }}>
                    Agents affected:
                  </div>
                  {deleteTarget.agents.map((a) => (
                    <div key={a.agent_id}>{a.agent_name}</div>
                  ))}
                </div>
              </>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}
