"use client";

import { useEffect, useId, useRef, useState } from "react";
import { ApiError, Tenant } from "@/lib/api";
import {
  createKnowledgeBase,
  deleteKnowledgeBase,
  KnowledgeBase,
  listKnowledgeBases,
  uploadDocument,
} from "@/lib/knowledgeApi";
import { Modal } from "@/components/Modal";

// Upload uses the existing document API; other methods are not available yet.
const SOURCE_CARDS = [
  {
    key: "upload",
    label: "Upload a file",
    hint: "PDF, Word, text, Markdown, CSV…",
    enabled: true,
  },
  {
    key: "website",
    label: "From a website",
    hint: "Coming soon",
    enabled: false,
  },
  { key: "text", label: "Paste text", hint: "Coming soon", enabled: false },
  {
    key: "integration",
    label: "From Connected Apps",
    hint: "Coming soon",
    enabled: false,
  },
] as const;

export const ACCEPTED_DOC_EXTENSIONS = [
  ".txt",
  ".md",
  ".pdf",
  ".docx",
  ".pptx",
  ".xlsx",
  ".html",
  ".htm",
  ".csv",
  ".tsv",
];
export const ACCEPTED_DOC_ACCEPT = ACCEPTED_DOC_EXTENSIONS.join(",");

// A cheap first filter only (drag-and-drop bypasses <input accept>): the server detects the real type from the
// file's bytes, so the browser-declared MIME type is not checked here.

export function rejectionReasonFor(file: File): string | null {
  const name = file.name.toLowerCase();
  const hasAcceptedExtension = ACCEPTED_DOC_EXTENSIONS.some((ext) =>
    name.endsWith(ext),
  );
  if (!hasAcceptedExtension) {
    return `"${file.name}" isn't a supported file type (text, Markdown, HTML or CSV; PDF, Word, Excel and PowerPoint only if your administrator has turned on document parsing).`;
  }
  return null;
}

function deriveSlug(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

export function AddSourceModal({
  tenants,
  knowledgeBases,
  canManage,
  onClose,
  onCreated,
  defaultTenantId,
}: {
  tenants: Tenant[];
  knowledgeBases: KnowledgeBase[];
  canManage: boolean;
  onClose: () => void;
  onCreated: () => void;
  defaultTenantId?: string;
}) {
  const [card, setCard] = useState<(typeof SOURCE_CARDS)[number]["key"] | null>(
    null,
  );

  const [tenantId, setTenantId] = useState(
    defaultTenantId ?? (tenants.length === 1 ? tenants[0].id : ""),
  );
  const fieldId = useId();
  const fileInput = useRef<HTMLInputElement>(null);
  const [target, setTarget] = useState<"new" | string>("new");
  const [newName, setNewName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const [folderList, setFolderList] = useState<{
    tenantId: string;
    rows: KnowledgeBase[];
    loading: boolean;
    error: string | null;
  }>({
    tenantId,
    rows: knowledgeBases.filter((kb) => kb.tenant_id === tenantId),
    loading: true,
    error: null,
  });
  const [folderReload, setFolderReload] = useState(0);
  useEffect(() => {
    let cancelled = false;
    if (!tenantId) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setFolderList({ tenantId, rows: [], loading: true, error: null });
    listKnowledgeBases(tenantId)
      .then((rows) => {
        if (!cancelled)
          setFolderList({ tenantId, rows, loading: false, error: null });
      })
      .catch((error) => {
        if (!cancelled)
          setFolderList({
            tenantId,
            rows: [],
            loading: false,
            error: error instanceof ApiError ? error.detail : String(error),
          });
      });
    return () => {
      cancelled = true;
    };
  }, [tenantId, folderReload]);
  const tenantKbs = folderList.tenantId === tenantId ? folderList.rows : [];
  const foldersLoading = folderList.tenantId !== tenantId || folderList.loading;

  const handleFileChange = (f: File | null) => {
    setFile(null);
    setFileError(null);
    if (!f) return;
    const reason = rejectionReasonFor(f);
    if (reason) {
      setFileError(reason);
      return;
    }
    setFile(f);
  };

  const canSubmit =
    canManage &&
    !!tenantId &&
    !foldersLoading &&
    !folderList.error &&
    !!file &&
    !fileError &&
    (target === "new"
      ? !!newName.trim()
      : tenantKbs.some((kb) => kb.id === target));

  const handleSubmit = async () => {
    if (!file || !canSubmit || submitting) return;
    setSubmitting(true);
    setSubmitError(null);
    // A newly created KB is rolled back if the upload fails, so no empty KB is left behind.
    let createdKbId: string | null = null;
    try {
      const kb =
        target === "new"
          ? await createKnowledgeBase(tenantId, {
              slug: `${deriveSlug(newName).slice(0, 60) || "folder"}-${crypto.randomUUID().slice(0, 8)}`,
              name: newName.trim(),
            })
          : { id: target };
      if (target === "new") createdKbId = kb.id;
      await uploadDocument(kb.id, file, file.name);
      createdKbId = null;
      onCreated();
      onClose();
    } catch (e) {
      setSubmitError(e instanceof ApiError ? e.detail : String(e));
      if (createdKbId) {
        await deleteKnowledgeBase(createdKbId).catch(() => {
          // Best effort: the upload error above is the one worth showing,
          // and the leftover is visible on the Knowledge page either way.
        });
      }
    } finally {
      setSubmitting(false);
    }
  };

  if (card === null) {
    return (
      <Modal
        open
        key="method"
        title="Add a file"
        onClose={onClose}
        trapFocus
        className="knowledge-dialog"
      >
        <div className="knowledge-methods">
          {SOURCE_CARDS.map((c) => (
            <button
              key={c.key}
              className="agent-template-card"
              style={{
                opacity: c.enabled ? 1 : 0.5,
                cursor: c.enabled ? "pointer" : "not-allowed",
              }}
              disabled={!c.enabled}
              onClick={() => setCard(c.key)}
            >
              <div className="agent-template-title">{c.label}</div>
              <div className="agent-template-blurb">{c.hint}</div>
            </button>
          ))}
        </div>
      </Modal>
    );
  }

  return (
    <Modal
      open
      key="upload"
      title="Upload a file"
      onClose={() => {
        if (!submitting) onClose();
      }}
      trapFocus
      className="knowledge-dialog"
      footer={
        <>
          <button
            className="btn btn-ghost btn-sm"
            disabled={submitting}
            onClick={() => setCard(null)}
          >
            Back
          </button>
          <button
            className="btn btn-primary btn-sm"
            onClick={handleSubmit}
            disabled={!canSubmit || submitting}
          >
            {submitting ? "Adding…" : "Add file"}
          </button>
        </>
      }
    >
      {submitError && (
        <div className="error-banner" role="alert">
          {submitError}
        </div>
      )}

      <fieldset className="knowledge-fields" disabled={submitting}>
        {tenants.length > 1 && (
          <div className="form-group">
            <label className="form-label" htmlFor={`${fieldId}-account`}>
              Account
            </label>
            <select
              id={`${fieldId}-account`}
              className="form-select"
              value={tenantId}
              onChange={(e) => {
                setTenantId(e.target.value);
                setTarget("new");
              }}
            >
              <option value="">— select —</option>
              {tenants.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </div>
        )}

        <div className="form-group">
          <label className="form-label" htmlFor={`${fieldId}-folder`}>
            Put it in this folder
          </label>
          <select
            id={`${fieldId}-folder`}
            className="form-select"
            value={target}
            disabled={!tenantId || foldersLoading || !!folderList.error}
            onChange={(e) => setTarget(e.target.value)}
          >
            <option value="new">
              {foldersLoading && tenantId
                ? "Loading folders…"
                : "+ Create a new folder"}
            </option>
            {tenantKbs.map((kb) => (
              <option key={kb.id} value={kb.id}>
                {kb.name}
              </option>
            ))}
          </select>
        </div>

        {folderList.error && (
          <div className="error-banner" role="alert">
            Couldn’t load folders: {folderList.error}{" "}
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => setFolderReload((value) => value + 1)}
            >
              Try again
            </button>
          </div>
        )}

        {target === "new" && (
          <div className="form-group">
            <label className="form-label" htmlFor={`${fieldId}-name`}>
              New folder name <span className="required">*</span>
            </label>
            <input
              id={`${fieldId}-name`}
              className="form-input"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="Reception FAQ"
            />
            <div className="form-hint">
              Group related files in a folder. You can turn it on for an agent
              later.
            </div>
          </div>
        )}

        <div className="form-group">
          <label className="form-label" htmlFor={`${fieldId}-file`}>
            File <span className="required">*</span>
          </label>
          <div
            className="knowledge-drop"
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              if (!submitting)
                handleFileChange(e.dataTransfer.files[0] ?? null);
            }}
          >
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => fileInput.current?.click()}
            >
              {file
                ? "Choose another file"
                : "Drop a file here or click to choose"}
            </button>
            <input
              ref={fileInput}
              id={`${fieldId}-file`}
              hidden
              type="file"
              accept={ACCEPTED_DOC_ACCEPT}
              onChange={(e) => {
                handleFileChange(e.target.files?.[0] || null);
                e.target.value = "";
              }}
            />
            {file && <div className="knowledge-selected-file">{file.name}</div>}
            <div className="form-hint">
              Text, Markdown, HTML, CSV or TSV. PDF, Word, Excel and PowerPoint
              when enabled for your account.
            </div>
          </div>
          {fileError && (
            <div className="error-banner" role="alert">
              {fileError}
            </div>
          )}
        </div>
        <div className="form-hint">
          We’ll prepare your file, then show when it’s ready for agents.
        </div>
      </fieldset>
    </Modal>
  );
}
