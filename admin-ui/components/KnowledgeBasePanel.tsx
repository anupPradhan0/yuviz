"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Check, FileText, Upload } from "lucide-react";
import { ApiError } from "@/lib/api";
import {
  AgentKnowledgeBase,
  assignKnowledgeBase,
  createKnowledgeBase,
  deleteKnowledgeBase,
  getRetrievalPolicy,
  KbDocument,
  KnowledgeBase,
  listAgentKnowledgeBases,
  listDocuments,
  listKnowledgeBases,
  setKnowledgeBaseEnabled,
  setRetrievalPolicy,
  uploadDocument,
} from "@/lib/knowledgeApi";
import { ACCEPTED_DOC_ACCEPT, rejectionReasonFor } from "@/components/AddSourceModal";

interface PolicyForm {
  top_k: number;
  minimum_score: number;
  max_tokens: number;
  include_citations: boolean;
}

const DEFAULT_POLICY: PolicyForm = { top_k: 5, minimum_score: 0, max_tokens: 1000, include_citations: true };
const POLL_MS = 4000;

const STATUS_LABEL: Record<KbDocument["status"], { text: string; cls: string }> = {
  ready: { text: "Ready", cls: "green" },
  pending: { text: "Getting ready…", cls: "amber" },
  processing: { text: "Getting ready…", cls: "amber" },
  failed: { text: "Couldn't read this file", cls: "red" },
};

function slugFor(fileName: string): string {
  const base = fileName.replace(/\.[^.]+$/, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  return `${base || "doc"}-${Math.random().toString(36).slice(2, 7)}`;
}

// Agent-scoped picker: every document in the account with an on/off switch for this agent.
export function KnowledgeBasePanel({ tenantId, agentId }: { tenantId: string; agentId: string }) {
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [assignments, setAssignments] = useState<AgentKnowledgeBase[]>([]);
  const [docsByKb, setDocsByKb] = useState<Record<string, KbDocument[]>>({});
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyKb, setBusyKb] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const [policyForm, setPolicyForm] = useState<PolicyForm>(DEFAULT_POLICY);
  const [policySaving, setPolicySaving] = useState(false);
  const [policySaved, setPolicySaved] = useState(false);

  const refresh = async () => {
    const [kbRes, assignRes] = await Promise.allSettled([listKnowledgeBases(tenantId), listAgentKnowledgeBases(agentId)]);
    const nextKbs = kbRes.status === "fulfilled" ? kbRes.value : [];
    const nextAssigns = assignRes.status === "fulfilled" ? assignRes.value : [];
    const failed = [kbRes, assignRes].find((r) => r.status === "rejected") as PromiseRejectedResult | undefined;
    setError(failed ? (failed.reason instanceof ApiError ? failed.reason.detail : String(failed.reason)) : null);

    const ids = [...new Set([...nextKbs.map((kb) => kb.id), ...nextAssigns.map((a) => a.kb_id)])];
    const docResults = await Promise.allSettled(ids.map((id) => listDocuments(id)));
    setKbs(nextKbs);
    setAssignments(nextAssigns);
    setDocsByKb(Object.fromEntries(ids.map((id, i) => [id, docResults[i].status === "fulfilled" ? (docResults[i] as PromiseFulfilledResult<KbDocument[]>).value : []])));
    setLoading(false);
  };

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
    getRetrievalPolicy(agentId)
      .then((p) =>
        setPolicyForm({
          top_k: p.top_k ?? DEFAULT_POLICY.top_k,
          minimum_score: p.minimum_score ?? DEFAULT_POLICY.minimum_score,
          max_tokens: p.max_tokens ?? DEFAULT_POLICY.max_tokens,
          include_citations: p.include_citations ?? DEFAULT_POLICY.include_citations,
        }),
      )
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId, agentId]);

  // One request per KB adds up fast (upload makes a KB per file), so the poll only re-reads unfinished ones.
  const processingKbIds = Object.entries(docsByKb)
    .filter(([, docs]) => docs.some((d) => d.status === "pending" || d.status === "processing"))
    .map(([id]) => id);
  useEffect(() => {
    if (processingKbIds.length === 0) return;
    const t = setTimeout(async () => {
      const results = await Promise.allSettled(processingKbIds.map((id) => listDocuments(id)));
      setDocsByKb((prev) => {
        const next = { ...prev };
        results.forEach((r, i) => {
          if (r.status === "fulfilled") next[processingKbIds[i]] = r.value;
        });
        return next;
      });
    }, POLL_MS);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docsByKb]);

  const assignmentByKb = new Map(assignments.map((a) => [a.kb_id, a]));

  const handleToggle = async (kbId: string, on: boolean) => {
    setBusyKb(kbId);
    try {
      if (assignmentByKb.has(kbId)) await setKnowledgeBaseEnabled(agentId, kbId, on);
      else if (on) await assignKnowledgeBase(agentId, kbId);
      await refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setBusyKb(null);
    }
  };

  const handleUpload = async (file: File | null) => {
    if (!file) return;
    const reason = rejectionReasonFor(file);
    if (reason) {
      setError(reason);
      return;
    }
    setUploading(true);
    setError(null);
    // The new knowledge base is rolled back if the upload fails, so no empty one is left behind.
    let createdKbId: string | null = null;
    try {
      const kb = await createKnowledgeBase(tenantId, { slug: slugFor(file.name), name: file.name.replace(/\.[^.]+$/, "") });
      createdKbId = kb.id;
      await uploadDocument(kb.id, file, file.name);
      createdKbId = null;
      await assignKnowledgeBase(agentId, kb.id);
      await refresh();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
      if (createdKbId) await deleteKnowledgeBase(createdKbId).catch(() => {});
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  const handleSavePolicy = async () => {
    setPolicySaving(true);
    setPolicySaved(false);
    try {
      await setRetrievalPolicy(agentId, policyForm);
      setPolicySaved(true);
      setTimeout(() => setPolicySaved(false), 2000);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setPolicySaving(false);
    }
  };

  if (loading) return <div className="empty-state">Loading…</div>;

  const kbNames = new Map<string, string>([
    ...kbs.map((kb) => [kb.id, kb.name] as [string, string]),
    ...assignments.map((a) => [a.kb_id, a.kb_name] as [string, string]),
  ]);
  // Empty, unused knowledge bases are leftovers from failed uploads; nothing to pick there.
  const rows = [...kbNames.entries()]
    .map(([id, name]) => ({ id, name, docs: docsByKb[id] || [], on: !!assignmentByKb.get(id)?.enabled }))
    .filter((r) => r.docs.length > 0 || assignmentByKb.has(r.id))
    .sort((a, b) => Number(b.on) - Number(a.on) || a.name.localeCompare(b.name));
  const usedCount = rows.filter((r) => r.on).length;

  return (
    <div className="card">
      <div className="card-hdr">
        <div>
          <div className="card-title">Documents</div>
          <div className="card-sub" style={{ marginLeft: 0 }}>
            {rows.length === 0
              ? "Upload a file and the agent will answer callers from it."
              : `Switch on what the agent should answer from. Using ${usedCount} of ${rows.length}.`}
          </div>
        </div>
        <button
          className="btn btn-primary btn-sm"
          style={{ marginLeft: "auto" }}
          disabled={uploading}
          onClick={() => fileInput.current?.click()}
        >
          <Upload size={13} /> {uploading ? "Uploading…" : "Upload document"}
        </button>
        <input
          ref={fileInput}
          type="file"
          accept={ACCEPTED_DOC_ACCEPT}
          hidden
          onChange={(e) => handleUpload(e.target.files?.[0] || null)}
        />
      </div>

      {error && <div className="error-banner">{error}</div>}

      {rows.length === 0 ? (
        <div className="empty-state">
          No documents yet. Upload a price list, FAQ or policy as a .txt or .md file.
        </div>
      ) : (
        rows.map((row) => {
          const single = row.docs.length === 1 ? row.docs[0] : null;
          const status = single ? STATUS_LABEL[single.status] : null;
          return (
            <div key={row.id} className="kb-row">
              <FileText size={16} style={{ color: "var(--text-3)", flexShrink: 0 }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontWeight: 500, overflowWrap: "anywhere" }}>{single ? single.title : row.name}</div>
                <div style={{ fontSize: ".7rem", color: "var(--text-3)", display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
                  {status && <span className={`badge ${status.cls}`}>{status.text}</span>}
                  {single?.usage_mode === "prompt" && <span>Read on every call</span>}
                  {!single && `${row.docs.length} files: ${row.docs.map((d) => d.title).join(", ") || "none yet"}`}
                </div>
                {single?.error && <div style={{ color: "var(--red)", fontSize: ".68rem" }}>{single.error}</div>}
              </div>
              <label className="toggle-switch" title={row.on ? "The agent uses this" : "The agent ignores this"}>
                <input
                  type="checkbox"
                  checked={row.on}
                  disabled={busyKb === row.id}
                  onChange={(e) => handleToggle(row.id, e.target.checked)}
                />
                <span className="toggle-slider" />
              </label>
            </div>
          );
        })
      )}

      <div className="card-body" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {usedCount > 0 && (
          <details>
            <summary style={{ cursor: "pointer", fontSize: ".78rem", color: "var(--text-2)" }}>Search settings</summary>
            <div style={{ marginTop: 12 }}>
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
                  Max text to read <span className="hint">per question</span>
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
                  <span className="form-range-label">Looser</span>
                  <span className="form-range-label">Stricter</span>
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
              <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
                {policySaved && <span className="saved-note">Saved <Check size={13} /></span>}
                <button className="btn btn-ghost btn-sm" onClick={handleSavePolicy} disabled={policySaving}>
                  {policySaving ? "Saving…" : "Save"}
                </button>
              </div>
            </div>
          </details>
        )}
        <Link href="/knowledge-bases" style={{ fontSize: ".76rem", color: "var(--cyan)" }}>
          Manage all documents →
        </Link>
      </div>
    </div>
  );
}
