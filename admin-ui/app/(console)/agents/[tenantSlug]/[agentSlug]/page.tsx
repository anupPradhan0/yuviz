"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft, ArrowRight, Check, Mic } from "lucide-react";
import { Agent, AgentUpdate, ApiError, deleteAgent, getAgent, getCurrentUser, getLiveCalls, listProviders, ProviderConfig, undoPrompt, updateAgent } from "@/lib/api";
import { KnowledgeBaseTabs } from "@/components/KnowledgeBaseTabs";
import { ToolsPanel } from "@/components/ToolsPanel";
import { Modal } from "@/components/Modal";
import { SipPanel } from "@/components/SipPanel";
import { AgentVoiceSettings } from "@/components/AgentVoiceSettings";
import { LANGUAGES, OTHER } from "@/lib/engineCatalog";

type Tab = "identity" | "prompt" | "voice" | "knowledge" | "advanced" | "limits" | "sip";

const TABS: { key: Tab; label: string }[] = [
  { key: "identity", label: "Basics" },
  { key: "prompt", label: "Instructions" },
  { key: "voice", label: "Voice & Language" },
  { key: "knowledge", label: "Knowledge & Tools" },
  { key: "advanced", label: "Ending & Transfers" },
  { key: "limits", label: "Call Limits" },
  { key: "sip", label: "Phone Numbers" },
];

// Knowledge & Tools and Phone Numbers save through their own panels.
const SAVE_BAR_TABS = new Set<Tab>(["identity", "prompt", "voice", "advanced", "limits"]);

const TRANSFER_TYPES: { value: NonNullable<AgentUpdate["transfer_type"]>; title: string; blurb: string }[] = [
  { value: "none", title: "Don't transfer", blurb: "The agent handles every call on its own." },
  { value: "cold", title: "Connect directly", blurb: "The caller is put straight through to a person." },
  { value: "warm", title: "Introduce first", blurb: "The person picks up first, then the caller joins." },
];

const GRACE_OPTIONS_MS = [0, 500, 1000, 1500, 2000, 3000, 5000];
const ESCALATION_OPTIONS = [1, 2, 3, 4, 5];

const formatSeconds = (ms: number) => (ms === 0 ? "No pause" : `${ms / 1000} second${ms === 1000 ? "" : "s"}`);

export default function AgentDetailPage() {
  const params = useParams<{ tenantSlug: string; agentSlug: string }>();
  const router = useRouter();
  const searchParams = useSearchParams();
  const { tenantSlug, agentSlug } = params;

  const [agent, setAgent] = useState<Agent | null>(null);
  const [providers, setProviders] = useState<ProviderConfig[]>([]);
  const [tab, setTab] = useState<Tab>("identity");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  // null = still checking.
  const [liveCallCount, setLiveCallCount] = useState<number | null>(null);
  const [deleteChecking, setDeleteChecking] = useState(false);
  const [isSuperadmin, setIsSuperadmin] = useState(false);

  const [form, setForm] = useState<AgentUpdate>({});
  const [languageChoice, setLanguageChoice] = useState<string>("");
  const [customLanguage, setCustomLanguage] = useState("");
  const [baseline, setBaseline] = useState("");

  const snapshot = JSON.stringify({ form, languageChoice, customLanguage });
  const dirty = baseline !== "" && snapshot !== baseline;

  // ?test=1 comes from the creation wizard.
  useEffect(() => {
    if (searchParams.get("test") === "1") {
      router.replace(`/agents/${tenantSlug}/${agentSlug}/test`);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    getCurrentUser().then((me) => setIsSuperadmin(me.role === "superadmin")).catch(() => setIsSuperadmin(false));
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    getAgent(tenantSlug, agentSlug)
      .then(async (a) => {
        setAgent(a);
        const initialForm: AgentUpdate = {
          name: a.name,
          greeting: a.greeting,
          system_prompt: a.system_prompt,
          goodbye_grace_ms: a.goodbye_grace_ms,
          language: a.language,
          stt_config_id: a.stt_config_id,
          llm_config_id: a.llm_config_id,
          tts_config_id: a.tts_config_id,
          transfer_type: a.transfer_type,
          transfer_destination: a.transfer_destination,
          queue_id: a.queue_id,
          escalation_threshold: a.escalation_threshold,
          caller_id_policy: a.caller_id_policy,
          platform_did: a.platform_did,
          custom_caller_id: a.custom_caller_id,
          transfer_waiting_experience: a.transfer_waiting_experience,
          max_call_duration_s: a.max_call_duration_s,
          status: a.status,
          end_call_prompt: a.end_call_prompt,
          transfer_prompt: a.transfer_prompt,
          farewell_message: a.farewell_message,
          transfer_announcement: a.transfer_announcement,
        };
        let choice = "";
        let custom = "";
        if (a.language && LANGUAGES.some((l) => l.value === a.language)) {
          choice = a.language;
        } else if (a.language) {
          choice = OTHER;
          custom = a.language;
        }
        setForm(initialForm);
        setLanguageChoice(choice);
        setCustomLanguage(custom);
        setBaseline(JSON.stringify({ form: initialForm, languageChoice: choice, customLanguage: custom }));
        const provs = await listProviders(a.tenant_id);
        setProviders(provs);
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
  }, [tenantSlug, agentSlug]);

  const handleSave = async () => {
    if (!agent) return;
    setSaving(true);
    setSaveError(null);
    setSaved(false);
    try {
      const language =
        languageChoice === "" ? null : languageChoice === OTHER ? customLanguage.trim() || null : languageChoice;
      const updated = await updateAgent(tenantSlug, agent.id, { ...form, language });
      setAgent(updated);
      setBaseline(snapshot);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      setSaveError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSaving(false);
    }
  };

  const handleUndo = async () => {
    if (!agent) return;
    setSaving(true);
    setSaveError(null);
    try {
      const restored = await undoPrompt(tenantSlug, agent.id);
      setAgent(restored);
      setForm((f) => ({ ...f, system_prompt: restored.system_prompt }));
      const base = JSON.parse(baseline);
      base.form.system_prompt = restored.system_prompt;
      setBaseline(JSON.stringify(base));
    } catch (e) {
      setSaveError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setSaving(false);
    }
  };

  const openDeleteConfirm = async () => {
    if (!agent) return;
    setDeleteConfirmOpen(true);
    setSaveError(null);
    // Live Calls is superadmin-only; other roles rely on the DELETE's 409.
    if (!isSuperadmin) {
      setLiveCallCount(0);
      return;
    }
    setLiveCallCount(null);
    setDeleteChecking(true);
    try {
      // LiveCall has no agent_id, so match by name; only a pre-check — the DELETE is authoritative.
      const snapshot = await getLiveCalls(tenantSlug);
      setLiveCallCount(snapshot.items.filter((c) => c.agent_name === agent.name).length);
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) setLiveCallCount(0);
      else setSaveError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setDeleteChecking(false);
    }
  };

  const handleDelete = async () => {
    if (!agent) return;
    setDeleting(true);
    try {
      await deleteAgent(tenantSlug, agent.id);
      router.push("/agents");
    } catch (e) {
      // 409 = a call started since the pre-check; switch to the blocked variant.
      if (e instanceof ApiError && e.status === 409 && e.body?.live_call_count !== undefined) {
        setLiveCallCount(Number(e.body.live_call_count));
        setDeleting(false);
        return;
      }
      setSaveError(e instanceof ApiError ? e.detail : String(e));
      setDeleting(false);
    }
  };

  if (loading) return <div className="empty-state">Loading…</div>;
  if (error) return <div className="error-banner">{error}</div>;
  if (!agent) return null;

  const isActive = (form.status || "active") === "active";
  const transferType = form.transfer_type || "none";
  const graceMs = form.goodbye_grace_ms ?? 0;
  const graceOptions = GRACE_OPTIONS_MS.includes(graceMs) ? GRACE_OPTIONS_MS : [...GRACE_OPTIONS_MS, graceMs].sort((a, b) => a - b);
  const escalation = form.escalation_threshold ?? null;
  const escalationOptions =
    escalation === null || ESCALATION_OPTIONS.includes(escalation) ? ESCALATION_OPTIONS : [...ESCALATION_OPTIONS, escalation].sort((a, b) => a - b);

  return (
    <>
      <div className="agent-page-hdr">
        <button className="btn btn-ghost btn-sm" onClick={() => router.push("/agents")}>
          <ArrowLeft size={13} /> All agents
        </button>
        <div className="agent-page-actions">
          <button className="btn btn-ghost btn-sm" onClick={() => router.push(`/workflows/${tenantSlug}/${agentSlug}`)}>
            Call flow <ArrowRight size={13} />
          </button>
          <button className="btn btn-primary btn-sm" onClick={() => router.push(`/agents/${tenantSlug}/${agentSlug}/test`)}>
            <Mic size={13} /> Test agent
          </button>
        </div>
      </div>

      <div className="agent-page-title">
        <h1>{agent.name}</h1>
        <span className={`badge ${agent.status === "active" ? "green" : "gray"}`}>
          {agent.status === "active" ? "Taking calls" : "Paused"}
        </span>
      </div>

      <div className="tabs">
        {TABS.map((t) => (
          <button key={t.key} className={`tab${tab === t.key ? " active" : ""}`} onClick={() => setTab(t.key)}>
            {t.label}
          </button>
        ))}
      </div>

      {saveError && <div className="error-banner">{saveError}</div>}

      {tab === "identity" && (
        <div className="cols">
          <div className="col-main">
            <div className="card" style={{ marginBottom: 14 }}>
              <div className="card-hdr">
                <div className="card-title">Basics</div>
                <div className="card-sub">how this agent shows up in your console</div>
              </div>
              <div className="card-body">
                <div className="form-group">
                  <label className="form-label">
                    Agent name <span className="required">*</span>
                  </label>
                  <input
                    className="form-input"
                    value={form.name || ""}
                    placeholder="e.g. Front desk"
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                  />
                  <div className="form-hint">Only you and your team see this name. Callers don&apos;t.</div>
                </div>
                <div className="agent-toggle-row">
                  <div>
                    <div className="form-label" style={{ margin: 0 }}>Taking calls</div>
                    <div className="form-hint">
                      {isActive
                        ? "This agent answers calls on its phone numbers."
                        : "Paused. This agent won't answer calls, but its settings are kept."}
                    </div>
                  </div>
                  <label className="toggle-switch">
                    <input
                      type="checkbox"
                      checked={isActive}
                      aria-label="Taking calls"
                      onChange={(e) => setForm({ ...form, status: e.target.checked ? "active" : "inactive" })}
                    />
                    <span className="toggle-slider" />
                  </label>
                </div>
              </div>
            </div>

            <div className="card agent-danger-card">
              <div className="card-body agent-danger-body">
                <div>
                  <div className="form-label" style={{ margin: 0 }}>Delete this agent</div>
                  <div className="form-hint">Permanently removes the agent. This can&apos;t be undone.</div>
                </div>
                <button className="btn btn-danger btn-sm" onClick={openDeleteConfirm} disabled={deleting}>
                  {deleting ? "Deleting…" : "Delete agent"}
                </button>
              </div>
            </div>
          </div>
          <div className="col-side">
            <div className="card">
              <div className="card-hdr">
                <div className="card-title">Details</div>
              </div>
              <div className="card-body agent-details">
                <div><span>Account</span><b>{tenantSlug}</b></div>
                <div><span>Agent ID</span><b className="mono">{agent.slug}</b></div>
                <div><span>Version</span><b>{agent.config_version}</b></div>
              </div>
            </div>
          </div>
        </div>
      )}

      {tab === "prompt" && (
        <div className="cols">
          <div className="col-main">
            <div className="card">
              <div className="card-hdr">
                <div className="card-title">Instructions</div>
                <div className="card-sub">what the agent says first, and how it should behave</div>
              </div>
              <div className="card-body">
                <div className="form-group">
                  <label className="form-label">Opening line</label>
                  <input
                    className="form-input"
                    value={form.greeting ?? ""}
                    placeholder="Hi, thanks for calling Acme Dental. How can I help you today?"
                    onChange={(e) => setForm({ ...form, greeting: e.target.value })}
                  />
                  <div className="form-hint">The first thing callers hear when the agent picks up.</div>
                </div>
                <div className="form-group" style={{ marginBottom: 0 }}>
                  <label className="form-label">How the agent should behave</label>
                  <textarea
                    className="form-textarea"
                    style={{ minHeight: 240 }}
                    value={form.system_prompt ?? ""}
                    placeholder={"You are the friendly receptionist for Acme Dental.\nHelp callers book, move or cancel appointments and answer questions about opening hours.\nNever give medical advice. If you're unsure, offer to take a message."}
                    onChange={(e) => setForm({ ...form, system_prompt: e.target.value })}
                  />
                  <div className="form-hint">Changes here also update this agent&apos;s call flow.</div>
                  {agent.can_undo && (
                    <div className="form-hint">
                      <button type="button" className="btn btn-ghost btn-sm" onClick={handleUndo} disabled={saving}>
                        Undo last change
                      </button>
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>
          <div className="col-side">
            <div className="card">
              <div className="card-hdr">
                <div className="card-title">Writing tips</div>
              </div>
              <div className="card-body">
                <ul className="agent-tips">
                  <li>Say who the agent is and which business it works for.</li>
                  <li>List what it should help callers with.</li>
                  <li>Say what it must never do or promise.</li>
                  <li>Write it like you&apos;re briefing a new teammate: short and specific.</li>
                </ul>
              </div>
            </div>
          </div>
        </div>
      )}

      {tab === "voice" && (
        <div className="cols">
          <div className="col-main">
            <AgentVoiceSettings
              tenantId={agent.tenant_id}
              providers={providers}
              setProviders={setProviders}
              languageChoice={languageChoice}
              onLanguageChoice={setLanguageChoice}
              customLanguage={customLanguage}
              onCustomLanguage={setCustomLanguage}
              sttId={form.stt_config_id}
              llmId={form.llm_config_id}
              ttsId={form.tts_config_id}
              onAssign={(role, id) => setForm((prev) => ({ ...prev, [`${role}_config_id`]: id }))}
              onError={setSaveError}
            />
          </div>
        </div>
      )}

      {tab === "knowledge" && (
        <>
          <KnowledgeBaseTabs tenantId={agent.tenant_id} agentId={agent.id} />
          <div style={{ marginTop: 16 }}>
            <ToolsPanel tenantId={agent.tenant_id} agentId={agent.id} />
          </div>
        </>
      )}

      {tab === "advanced" && (
        <div className="cols">
          <div className="col-main">
            <div className="card" style={{ marginBottom: 14 }}>
              <div className="card-hdr">
                <div className="card-title">Ending the call</div>
                <div className="card-sub">when the agent hangs up, and what it says</div>
              </div>
              <div className="card-body">
                <div className="form-group">
                  <label className="form-label">When should the agent end the call?</label>
                  <textarea
                    className="form-textarea"
                    style={{ minHeight: 56 }}
                    value={form.end_call_prompt || ""}
                    onChange={(e) => setForm({ ...form, end_call_prompt: e.target.value || null })}
                    placeholder="When the caller says goodbye, has no more questions, or their issue is sorted."
                  />
                  <div className="form-hint">Describe the moment, not the words. Leave blank to use the default.</div>
                </div>
                <div className="form-group" style={{ marginBottom: 0 }}>
                  <label className="form-label">Goodbye message</label>
                  <textarea
                    className="form-textarea"
                    style={{ minHeight: 56 }}
                    value={form.farewell_message || ""}
                    onChange={(e) => setForm({ ...form, farewell_message: e.target.value || null })}
                    placeholder="Thanks for calling. Have a great day. Goodbye!"
                  />
                  <div className="form-hint">Spoken exactly as written. Leave blank to let the agent choose its own words.</div>
                </div>
              </div>
            </div>

            <div className="card">
              <div className="card-hdr">
                <div className="card-title">Transfer to a person</div>
                <div className="card-sub">hand the caller to someone on your team</div>
              </div>
              <div className="card-body">
                <div className="voice-engine-row" role="radiogroup" aria-label="Transfer type">
                  {TRANSFER_TYPES.map((t) => (
                    <button
                      key={t.value}
                      type="button"
                      role="radio"
                      aria-checked={transferType === t.value}
                      className={`voice-engine-card${transferType === t.value ? " on" : ""}`}
                      onClick={() => setForm({ ...form, transfer_type: t.value })}
                    >
                      <div className="agent-template-title">{t.title}</div>
                      <div className="agent-template-blurb">{t.blurb}</div>
                    </button>
                  ))}
                </div>

                {transferType !== "none" && (
                  <>
                    <div className="form-group">
                      <label className="form-label">
                        Transfer to <span className="required">*</span>
                      </label>
                      <input
                        className="form-input"
                        value={form.transfer_destination || ""}
                        onChange={(e) => setForm({ ...form, transfer_destination: e.target.value || null })}
                        placeholder="+1 800 555 0100"
                      />
                      <div className="form-hint">A phone number with country code. A SIP address also works.</div>
                    </div>
                    <div className="form-group">
                      <label className="form-label">When should it transfer?</label>
                      <textarea
                        className="form-textarea"
                        style={{ minHeight: 56 }}
                        value={form.transfer_prompt || ""}
                        onChange={(e) => setForm({ ...form, transfer_prompt: e.target.value || null })}
                        placeholder="If the caller asks to speak to a person."
                      />
                      <div className="form-hint">Describe the moment, not the words. Leave blank to use the default.</div>
                    </div>
                    <div className="form-group" style={{ marginBottom: transferType === "warm" ? 14 : 0 }}>
                      <label className="form-label">What it says before transferring</label>
                      <textarea
                        className="form-textarea"
                        style={{ minHeight: 56 }}
                        value={form.transfer_announcement || ""}
                        onChange={(e) => setForm({ ...form, transfer_announcement: e.target.value || null })}
                        placeholder="Please hold while I connect you."
                      />
                      <div className="form-hint">Spoken exactly as written. Leave blank to let the agent choose its own words.</div>
                    </div>
                  </>
                )}

                {transferType === "warm" && (
                  <div className="form-row" style={{ flexWrap: "wrap" }}>
                    <div className="form-group">
                      <label className="form-label">Number your teammate sees</label>
                      <select
                        className="form-select"
                        value={form.caller_id_policy || "original"}
                        onChange={(e) => setForm({ ...form, caller_id_policy: e.target.value as AgentUpdate["caller_id_policy"] })}
                      >
                        <option value="original">The caller&apos;s number</option>
                        <option value="platform">One of your business numbers</option>
                        <option value="custom">Another number</option>
                      </select>
                    </div>
                    {form.caller_id_policy === "platform" && (
                      <div className="form-group">
                        <label className="form-label">Business number</label>
                        <input
                          className="form-input"
                          value={form.platform_did || ""}
                          onChange={(e) => setForm({ ...form, platform_did: e.target.value || null })}
                          placeholder="+1 800 555 0100"
                        />
                      </div>
                    )}
                    {form.caller_id_policy === "custom" && (
                      <div className="form-group">
                        <label className="form-label">Number to show</label>
                        <input
                          className="form-input"
                          value={form.custom_caller_id || ""}
                          onChange={(e) => setForm({ ...form, custom_caller_id: e.target.value || null })}
                          placeholder="+1 800 555 0100"
                        />
                      </div>
                    )}
                    <div className="form-group">
                      <label className="form-label">While the caller waits</label>
                      <select
                        className="form-select"
                        value={form.transfer_waiting_experience || "announcement_moh"}
                        onChange={(e) =>
                          setForm({ ...form, transfer_waiting_experience: e.target.value as AgentUpdate["transfer_waiting_experience"] })
                        }
                      >
                        <option value="announcement_moh">Short message, then hold music</option>
                        <option value="announcement_silence">Short message, then silence</option>
                      </select>
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      {tab === "limits" && (
        <div className="cols">
          <div className="col-main">
            <div className="card">
              <div className="card-hdr">
                <div className="card-title">Call limits</div>
                <div className="card-sub">safety caps that apply to every call</div>
              </div>
              <div className="card-body">
                <div className="form-row" style={{ flexWrap: "wrap" }}>
                  <div className="form-group">
                    <label className="form-label">Longest a call can last</label>
                    <div className="agent-unit-input">
                      <input
                        className="form-input"
                        type="number"
                        min={1}
                        max={120}
                        placeholder="No limit"
                        value={form.max_call_duration_s == null ? "" : Math.round(form.max_call_duration_s / 60)}
                        onChange={(e) => {
                          const minutes = Number(e.target.value);
                          setForm({
                            ...form,
                            max_call_duration_s:
                              e.target.value === "" || !(minutes > 0) ? null : Math.min(7200, Math.max(30, Math.round(minutes * 60))),
                          });
                        }}
                      />
                      <span>minutes</span>
                    </div>
                    <div className="form-hint">The agent wraps up politely, then ends the call. Leave blank for no limit.</div>
                  </div>
                  <div className="form-group">
                    <label className="form-label">Pause before hanging up</label>
                    <select
                      className="form-select"
                      value={graceMs}
                      onChange={(e) => setForm({ ...form, goodbye_grace_ms: Number(e.target.value) })}
                    >
                      {graceOptions.map((ms) => (
                        <option key={ms} value={ms}>{formatSeconds(ms)}</option>
                      ))}
                    </select>
                    <div className="form-hint">Gives the caller a moment to say goodbye back.</div>
                  </div>
                </div>
                <div className="form-group" style={{ marginBottom: 0 }}>
                  <label className="form-label">Hand off to a person after repeated problems</label>
                  <select
                    className="form-select"
                    style={{ maxWidth: 260 }}
                    value={escalation ?? ""}
                    onChange={(e) => setForm({ ...form, escalation_threshold: e.target.value === "" ? null : Number(e.target.value) })}
                  >
                    <option value="">Never</option>
                    {escalationOptions.map((n) => (
                      <option key={n} value={n}>After {n} in a row</option>
                    ))}
                  </select>
                  <div className="form-hint">
                    If the caller keeps asking for things the agent isn&apos;t allowed to help with, pass them to someone on your team.
                  </div>
                  {escalation !== null && transferType === "none" && (
                    <div className="voice-missing">
                      This needs a transfer set up first.{" "}
                      <a href="#" onClick={(e) => { e.preventDefault(); setTab("advanced"); }}>Set up a transfer</a>
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {tab === "sip" && <SipPanel tenantId={agent.tenant_id} agentId={agent.id} />}

      {SAVE_BAR_TABS.has(tab) && (
        <div className="agent-save-bar">
          <span className="agent-save-status">
            {saved ? (
              <span className="saved-note">Saved <Check size={13} /></span>
            ) : dirty ? (
              "You have unsaved changes"
            ) : (
              "All changes saved"
            )}
          </span>
          {dirty && (
            <button
              className="btn btn-ghost btn-sm"
              disabled={saving}
              onClick={() => {
                const prev = JSON.parse(baseline) as { form: AgentUpdate; languageChoice: string; customLanguage: string };
                setForm(prev.form);
                setLanguageChoice(prev.languageChoice);
                setCustomLanguage(prev.customLanguage);
              }}
            >
              Discard
            </button>
          )}
          <button className="btn btn-primary btn-sm" onClick={handleSave} disabled={saving || !dirty}>
            {saving ? "Saving…" : "Save changes"}
          </button>
        </div>
      )}

      <Modal
        open={deleteConfirmOpen}
        title={
          deleteChecking
            ? `Checking "${agent.name}"…`
            : liveCallCount
              ? `Can't delete "${agent.name}" right now`
              : `Delete "${agent.name}"?`
        }
        onClose={() => setDeleteConfirmOpen(false)}
        footer={
          deleteChecking ? (
            <button className="btn btn-ghost btn-sm" onClick={() => setDeleteConfirmOpen(false)}>Cancel</button>
          ) : liveCallCount ? (
            // Deliberately no "force delete": this would cut off a live call.
            <>
              <button className="btn btn-ghost btn-sm" onClick={() => setDeleteConfirmOpen(false)}>Cancel</button>
              {isSuperadmin && <Link href="/live-calls" className="btn btn-ghost btn-sm">View live calls</Link>}
            </>
          ) : (
            <>
              <button className="btn btn-ghost btn-sm" onClick={() => setDeleteConfirmOpen(false)}>Cancel</button>
              <button className="btn btn-danger btn-sm" onClick={handleDelete} disabled={deleting}>
                {deleting ? "Deleting…" : "Delete agent"}
              </button>
            </>
          )
        }
      >
        {deleteChecking ? (
          <p style={{ fontSize: ".78rem", color: "var(--text-3)" }}>Checking whether anyone is on a call with this agent…</p>
        ) : liveCallCount ? (
          <p style={{
            fontSize: ".78rem", color: "var(--text-2)", lineHeight: 1.5,
            borderLeft: "2px solid var(--red-border)", padding: "6px 0 6px 10px", margin: 0,
          }}>
            <b>{`${liveCallCount} call${liveCallCount === 1 ? " is" : "s are"} happening`}</b>{" "}
            {`on this agent right now. Deleting it would cut ${liveCallCount === 1 ? "that caller" : "those callers"} off. Wait for the call to end, then try again.`}
          </p>
        ) : (
          <p style={{
            fontSize: ".78rem", color: "var(--text-2)", lineHeight: 1.5,
            borderLeft: "2px solid var(--green-border)", padding: "6px 0 6px 10px", margin: 0,
          }}>
            {isSuperadmin
              ? "No one is on a call with this agent."
              : "If a call is in progress on this agent, deletion will be refused until it ends."}{" "}
            Phone numbers that use it will switch to their backup agent, or to your account&apos;s default agent.
          </p>
        )}
      </Modal>
    </>
  );
}
