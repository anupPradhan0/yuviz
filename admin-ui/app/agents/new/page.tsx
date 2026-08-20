"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Agent,
  AgentUpdate,
  ApiError,
  Tenant,
  createAgent,
  listProviders,
  listTenants,
  ProviderConfig,
  updateAgent,
  updateProvider,
} from "@/lib/api";
import { KnowledgeBasePanel } from "@/components/KnowledgeBasePanel";
import { ToolsPanel } from "@/components/ToolsPanel";
import { SipPanel } from "@/components/SipPanel";
import { VoicePicker } from "@/components/VoicePicker";
import { LANGUAGES, OTHER } from "@/lib/engineCatalog";

type Step = "setup" | "tools" | "knowledge-base" | "sip";

// Same freeform/structured system-prompt editor as the agent detail page —
// duplicated rather than shared because the detail page keys it off an
// existing agent.system_prompt string and this page has no agent yet.
interface PromptSections {
  personality: string;
  environment: string;
  tone: string;
}

function composePromptSections(s: PromptSections): string {
  return `# Personality\n${s.personality}\n\n# Environment\n${s.environment}\n\n# Tone\n${s.tone}`;
}

export default function NewAgentPage() {
  const router = useRouter();

  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [tenantSlug, setTenantSlug] = useState("");
  const [providers, setProviders] = useState<ProviderConfig[]>([]);

  const [step, setStep] = useState<Step>("setup");
  const [createdAgent, setCreatedAgent] = useState<Agent | null>(null);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [partialWarning, setPartialWarning] = useState<string | null>(null);

  // Identity
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [languageChoice, setLanguageChoice] = useState("");
  const [customLanguage, setCustomLanguage] = useState("");

  // Behaviour
  const [greeting, setGreeting] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [promptMode, setPromptMode] = useState<"freeform" | "structured">("freeform");
  const [promptSections, setPromptSections] = useState<PromptSections>({ personality: "", environment: "", tone: "" });
  const [goodbyeGraceMs, setGoodbyeGraceMs] = useState(3000);
  const [endCallPrompt, setEndCallPrompt] = useState("");
  const [farewellMessage, setFarewellMessage] = useState("");
  const [sttConfigId, setSttConfigId] = useState<string | null>(null);
  const [llmConfigId, setLlmConfigId] = useState<string | null>(null);
  const [ttsConfigId, setTtsConfigId] = useState<string | null>(null);

  // Escalation
  const [transferType, setTransferType] = useState<AgentUpdate["transfer_type"]>("none");
  const [transferDestination, setTransferDestination] = useState("");
  const [transferPrompt, setTransferPrompt] = useState("");
  const [transferAnnouncement, setTransferAnnouncement] = useState("");
  const [queueId, setQueueId] = useState("");
  const [escalationThreshold, setEscalationThreshold] = useState<number | null>(null);
  const [callerIdPolicy, setCallerIdPolicy] = useState<AgentUpdate["caller_id_policy"]>("original");
  const [platformDid, setPlatformDid] = useState("");
  const [customCallerId, setCustomCallerId] = useState("");
  const [transferWaitingExperience, setTransferWaitingExperience] =
    useState<AgentUpdate["transfer_waiting_experience"]>("announcement_moh");

  useEffect(() => {
    listTenants().then((ts) => {
      setTenants(ts);
      if (ts.length > 0) setTenantSlug(ts[0].slug);
    });
  }, []);

  useEffect(() => {
    let ignore = false;
    const tenant = tenants.find((t) => t.slug === tenantSlug);
    if (!tenant) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setProviders([]);
      return;
    }
    listProviders(tenant.id)
      .then((provs) => {
        if (!ignore) setProviders(provs);
      })
      .catch(() => {
        if (!ignore) setProviders([]);
      });
    return () => {
      ignore = true;
    };
  }, [tenantSlug, tenants]);

  const byRole = (role: string) => providers.filter((p) => p.role === role);
  const selectedTenant = tenants.find((t) => t.slug === tenantSlug);

  const updatePromptSection = (key: keyof PromptSections, value: string) => {
    const next = { ...promptSections, [key]: value };
    setPromptSections(next);
    setSystemPrompt(composePromptSections(next));
  };

  const handleCreate = async () => {
    setCreating(true);
    setCreateError(null);
    setPartialWarning(null);
    try {
      const created = await createAgent(tenantSlug, {
        slug,
        name,
        greeting,
        system_prompt: systemPrompt,
        stt_config_id: sttConfigId,
        llm_config_id: llmConfigId,
        tts_config_id: ttsConfigId,
      });

      const language = languageChoice === "" ? null : languageChoice === OTHER ? customLanguage : languageChoice;
      try {
        const final = await updateAgent(tenantSlug, created.id, {
          goodbye_grace_ms: goodbyeGraceMs,
          language,
          end_call_prompt: endCallPrompt || null,
          farewell_message: farewellMessage || null,
          transfer_type: transferType,
          transfer_destination: transferDestination || null,
          transfer_prompt: transferPrompt || null,
          transfer_announcement: transferAnnouncement || null,
          queue_id: queueId || null,
          escalation_threshold: escalationThreshold,
          caller_id_policy: callerIdPolicy,
          platform_did: platformDid || null,
          custom_caller_id: customCallerId || null,
          transfer_waiting_experience: transferWaitingExperience,
        });
        setCreatedAgent(final);
      } catch (patchErr) {
        // Agent already exists at this point — don't block the flow, just
        // say so plainly. Behaviour/Escalation extras can still be finished
        // on the agent's own edit page; Tools/Knowledge Base/SIP below don't
        // depend on any of them.
        setCreatedAgent(created);
        setPartialWarning(
          `Agent created, but some Behaviour/Escalation settings didn't save (${
            patchErr instanceof ApiError ? patchErr.detail : String(patchErr)
          }). You can finish those on the agent's edit page.`,
        );
      }
      setStep("tools");
    } catch (e) {
      setCreateError(e instanceof ApiError ? e.detail : String(e));
    } finally {
      setCreating(false);
    }
  };

  const finish = () => {
    if (!createdAgent) return;
    router.push(`/agents/${tenantSlug}/${createdAgent.slug}`);
  };

  return (
    <>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
        <button className="btn btn-ghost btn-sm" onClick={() => router.push("/agents")}>
          ← Back to Agents
        </button>
      </div>

      <div className="tabs">
        <button className={`tab${step === "setup" ? " active" : ""}`} onClick={() => setStep("setup")}>
          Setup
        </button>
        <button
          className={`tab${step === "tools" ? " active" : ""}`}
          onClick={() => createdAgent && setStep("tools")}
          disabled={!createdAgent}
          title={createdAgent ? undefined : "Create the agent in Setup first"}
        >
          Tools
        </button>
        <button
          className={`tab${step === "knowledge-base" ? " active" : ""}`}
          onClick={() => createdAgent && setStep("knowledge-base")}
          disabled={!createdAgent}
          title={createdAgent ? undefined : "Create the agent in Setup first"}
        >
          Knowledge Base
        </button>
        <button
          className={`tab${step === "sip" ? " active" : ""}`}
          onClick={() => createdAgent && setStep("sip")}
          disabled={!createdAgent}
          title={createdAgent ? undefined : "Create the agent in Setup first"}
        >
          SIP
        </button>
      </div>

      {!createdAgent && (
        <div className="form-hint" style={{ marginTop: -10, marginBottom: 14 }}>
          Tools, Knowledge Base, and SIP unlock after you create the agent below — fill in Setup, then click &quot;Create Agent&quot;.
        </div>
      )}

      {createError && <div className="error-banner">{createError}</div>}
      {partialWarning && <div className="error-banner">{partialWarning}</div>}

      {step === "setup" && (
        <div className="cols">
          <div className="col-main">
            <div className="card" style={{ marginBottom: 14 }}>
              <div className="card-hdr">
                <div className="card-title">Identity</div>
              </div>
              <div className="card-body">
                <div className="form-row">
                  <div className="form-group">
                    <label className="form-label">
                      Account <span className="required">*</span>
                    </label>
                    <select
                      className="form-select"
                      value={tenantSlug}
                      onChange={(e) => {
                        setTenantSlug(e.target.value);
                        setSttConfigId(null);
                        setLlmConfigId(null);
                        setTtsConfigId(null);
                      }}
                      disabled={!!createdAgent}
                    >
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
                    <input className="form-input" value={name} onChange={(e) => setName(e.target.value)} disabled={!!createdAgent} />
                  </div>
                  <div className="form-group">
                    <label className="form-label">
                      Goodbye Grace <span className="hint">ms</span>
                    </label>
                    <input
                      className="form-input"
                      type="number"
                      min={0}
                      step={1}
                      style={{ fontFamily: "var(--mono)" }}
                      value={goodbyeGraceMs}
                      onChange={(e) => {
                        const v = Math.trunc(Number(e.target.value));
                        if (Number.isFinite(v) && v >= 0) setGoodbyeGraceMs(v);
                      }}
                      disabled={!!createdAgent}
                    />
                  </div>
                </div>
                <div className="form-group">
                  <label className="form-label">
                    Slug <span className="required">*</span>
                    <span className="hint">used in the WS routing path — no spaces</span>
                  </label>
                  <input
                    className="form-input"
                    style={{ fontFamily: "var(--mono)" }}
                    value={slug}
                    onChange={(e) => setSlug(e.target.value)}
                    placeholder="support"
                    disabled={!!createdAgent}
                  />
                </div>
                <div className="form-group" style={{ marginBottom: 0 }}>
                  <label className="form-label">
                    Language <span className="hint">overrides the STT/TTS provider&apos;s own language when set</span>
                  </label>
                  <select className="form-select" value={languageChoice} onChange={(e) => setLanguageChoice(e.target.value)} disabled={!!createdAgent}>
                    <option value="">— derive from provider —</option>
                    {LANGUAGES.map((l) => (
                      <option key={l.value} value={l.value}>
                        {l.label}
                      </option>
                    ))}
                    <option value={OTHER}>Other (custom)…</option>
                  </select>
                  {languageChoice === OTHER && (
                    <input
                      className="form-input"
                      style={{ marginTop: 6, fontFamily: "var(--mono)" }}
                      value={customLanguage}
                      onChange={(e) => setCustomLanguage(e.target.value)}
                      placeholder="e.g. nl-BE"
                      disabled={!!createdAgent}
                    />
                  )}
                </div>
              </div>
            </div>

            <div className="card" style={{ marginBottom: 14 }}>
              <div className="card-hdr">
                <div className="card-title">Conversation Identity</div>
              </div>
              <div className="card-body">
                <div className="form-group">
                  <label className="form-label">
                    Greeting <span className="required">*</span>
                  </label>
                  <textarea
                    className="form-textarea"
                    style={{ minHeight: 60 }}
                    value={greeting}
                    onChange={(e) => setGreeting(e.target.value)}
                    disabled={!!createdAgent}
                  />
                </div>
                <div className="form-group">
                  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                    <label className="form-label" style={{ marginBottom: 0 }}>
                      System Prompt <span className="required">*</span>
                    </label>
                    <div style={{ display: "flex", gap: 4 }}>
                      <button
                        type="button"
                        className={`btn btn-sm ${promptMode === "freeform" ? "btn-primary" : "btn-ghost"}`}
                        onClick={() => setPromptMode("freeform")}
                        disabled={!!createdAgent}
                      >
                        Freeform
                      </button>
                      <button
                        type="button"
                        className={`btn btn-sm ${promptMode === "structured" ? "btn-primary" : "btn-ghost"}`}
                        onClick={() => setPromptMode("structured")}
                        disabled={!!createdAgent}
                      >
                        Structured
                      </button>
                    </div>
                  </div>
                  {promptMode === "freeform" ? (
                    <textarea
                      className="form-textarea"
                      style={{ minHeight: 110, marginTop: 6 }}
                      value={systemPrompt}
                      onChange={(e) => setSystemPrompt(e.target.value)}
                      placeholder="You are a helpful customer support agent."
                      disabled={!!createdAgent}
                    />
                  ) : (
                    <div style={{ marginTop: 6, display: "flex", flexDirection: "column", gap: 10 }}>
                      <div>
                        <label className="form-label">Personality</label>
                        <textarea
                          className="form-textarea"
                          style={{ minHeight: 60 }}
                          value={promptSections.personality}
                          onChange={(e) => updatePromptSection("personality", e.target.value)}
                          disabled={!!createdAgent}
                        />
                      </div>
                      <div>
                        <label className="form-label">Environment</label>
                        <textarea
                          className="form-textarea"
                          style={{ minHeight: 60 }}
                          value={promptSections.environment}
                          onChange={(e) => updatePromptSection("environment", e.target.value)}
                          disabled={!!createdAgent}
                        />
                      </div>
                      <div>
                        <label className="form-label">Tone</label>
                        <textarea
                          className="form-textarea"
                          style={{ minHeight: 60 }}
                          value={promptSections.tone}
                          onChange={(e) => updatePromptSection("tone", e.target.value)}
                          disabled={!!createdAgent}
                        />
                      </div>
                    </div>
                  )}
                </div>
                <div className="form-group">
                  <label className="form-label">
                    End Call Condition <span className="hint">blank = default</span>
                  </label>
                  <textarea
                    className="form-textarea"
                    style={{ minHeight: 48 }}
                    value={endCallPrompt}
                    onChange={(e) => setEndCallPrompt(e.target.value)}
                    disabled={!!createdAgent}
                  />
                </div>
                <div className="form-group" style={{ marginBottom: 0 }}>
                  <label className="form-label">
                    Farewell Message <span className="hint">blank = AI chooses the wording</span>
                  </label>
                  <textarea
                    className="form-textarea"
                    style={{ minHeight: 48 }}
                    value={farewellMessage}
                    onChange={(e) => setFarewellMessage(e.target.value)}
                    disabled={!!createdAgent}
                  />
                </div>
              </div>
            </div>

            {selectedTenant && (
              <div className="card" style={{ marginBottom: 14 }}>
                <div className="card-hdr">
                  <div className="card-title">Voice</div>
                  <div className="card-sub">local engines only — sets the same TTS assignment as below</div>
                </div>
                <div className="card-body">
                  <VoicePicker
                    tenantId={selectedTenant.id}
                    providers={providers}
                    value={ttsConfigId}
                    onChange={setTtsConfigId}
                    onProviderCreated={(p) => setProviders((prev) => [...prev, p])}
                    disabled={!!createdAgent}
                  />
                  {(() => {
                    const selectedTts = providers.find((p) => p.id === ttsConfigId);
                    const speed = Number((selectedTts?.extra as Record<string, unknown> | null)?.speed ?? 1.0);
                    return (
                      <div className="form-group" style={{ marginTop: 12, marginBottom: 0 }}>
                        <label className="form-label">Speaking Speed</label>
                        <select
                          className="form-select"
                          style={{ width: 140 }}
                          value={String(speed)}
                          disabled={!selectedTts}
                          onChange={async (e) => {
                            if (!selectedTts) return;
                            const v = Number(e.target.value);
                            const updated = await updateProvider(selectedTts.id, {
                              extra: { ...((selectedTts.extra as Record<string, unknown>) || {}), speed: v },
                            });
                            setProviders(providers.map((p) => (p.id === updated.id ? updated : p)));
                          }}
                        >
                          {[0.7, 0.8, 0.9, 1.0, 1.1, 1.2].map((v) => (
                            <option key={v} value={String(v)}>
                              {v.toFixed(1)}
                              {v === 1.0 ? " (default)" : ""}
                            </option>
                          ))}
                        </select>
                      </div>
                    );
                  })()}
                </div>
              </div>
            )}

            <div className="card" style={{ marginBottom: 14 }}>
              <div className="card-hdr">
                <div className="card-title">Provider Assignments</div>
                <div className="card-sub">prod-first, dev warns</div>
              </div>
              <div className="card-body">
                <div className="form-row">
                  {([
                    ["stt", sttConfigId, setSttConfigId],
                    ["llm", llmConfigId, setLlmConfigId],
                    ["tts", ttsConfigId, setTtsConfigId],
                  ] as const).map(([role, value, setValue]) => (
                    <div className="form-group" key={role}>
                      <label className="form-label">{role.toUpperCase()}</label>
                      <select
                        className="form-select"
                        value={value || ""}
                        onChange={(e) => setValue(e.target.value || null)}
                        disabled={!!createdAgent}
                      >
                        <option value="">— none —</option>
                        {byRole(role).map((p) => (
                          <option key={p.id} value={p.id}>
                            {p.name} {p.environment !== "prod" ? "⚠ " + p.environment : ""}
                          </option>
                        ))}
                      </select>
                    </div>
                  ))}
                </div>
              </div>
            </div>

            <div className="card">
              <div className="card-hdr">
                <div className="card-title">Human Escalation</div>
                <div className="card-sub">AI-to-human transfer</div>
              </div>
              <div className="card-body">
                <div className="form-row">
                  <div className="form-group">
                    <label className="form-label">Transfer Type</label>
                    <select
                      className="form-select"
                      value={transferType || "none"}
                      onChange={(e) => setTransferType(e.target.value as AgentUpdate["transfer_type"])}
                      disabled={!!createdAgent}
                    >
                      <option value="none">Never Escalate</option>
                      <option value="cold">Cold Transfer</option>
                      <option value="warm">Warm Transfer</option>
                    </select>
                  </div>
                  <div className="form-group">
                    <label className="form-label">
                      Transfer Destination <span className="hint">phone number or SIP URI</span>
                    </label>
                    <input
                      className="form-input"
                      style={{ fontFamily: "var(--mono)", fontSize: ".75rem" }}
                      value={transferDestination}
                      onChange={(e) => setTransferDestination(e.target.value)}
                      placeholder="+18005550100 or sip:agent@example.com"
                      disabled={!!createdAgent || (transferType || "none") === "none"}
                    />
                  </div>
                  <div className="form-group">
                    <label className="form-label">
                      Transfer Condition <span className="hint">blank = default</span>
                    </label>
                    <textarea
                      className="form-textarea"
                      style={{ minHeight: 48 }}
                      value={transferPrompt}
                      onChange={(e) => setTransferPrompt(e.target.value)}
                      disabled={!!createdAgent || (transferType || "none") === "none"}
                    />
                  </div>
                  <div className="form-group">
                    <label className="form-label">
                      Transfer Announcement <span className="hint">blank = AI chooses the wording</span>
                    </label>
                    <textarea
                      className="form-textarea"
                      style={{ minHeight: 48 }}
                      value={transferAnnouncement}
                      onChange={(e) => setTransferAnnouncement(e.target.value)}
                      disabled={!!createdAgent || (transferType || "none") === "none"}
                    />
                  </div>
                  <div className="form-group">
                    <label className="form-label">Queue ID</label>
                    <input
                      className="form-input"
                      style={{ fontFamily: "var(--mono)", fontSize: ".75rem" }}
                      value={queueId}
                      onChange={(e) => setQueueId(e.target.value)}
                      disabled={!!createdAgent}
                    />
                  </div>
                  <div className="form-group">
                    <label className="form-label">
                      Escalate after <span className="hint">consecutive guardrail triggers</span>
                    </label>
                    <input
                      className="form-input"
                      style={{ fontFamily: "var(--mono)", width: 80 }}
                      value={escalationThreshold ?? ""}
                      onChange={(e) => setEscalationThreshold(e.target.value === "" ? null : Number(e.target.value))}
                      disabled={!!createdAgent}
                    />
                  </div>
                </div>

                {transferType === "warm" && (
                  <div className="form-row" style={{ marginTop: 12 }}>
                    <div className="form-group">
                      <label className="form-label">Caller ID</label>
                      <select
                        className="form-select"
                        value={callerIdPolicy || "original"}
                        onChange={(e) => setCallerIdPolicy(e.target.value as AgentUpdate["caller_id_policy"])}
                        disabled={!!createdAgent}
                      >
                        <option value="original">Original Caller</option>
                        <option value="platform">Platform DID</option>
                        <option value="custom">Custom</option>
                      </select>
                    </div>
                    {callerIdPolicy === "platform" && (
                      <div className="form-group">
                        <label className="form-label">Platform DID</label>
                        <input
                          className="form-input"
                          style={{ fontFamily: "var(--mono)", fontSize: ".75rem" }}
                          value={platformDid}
                          onChange={(e) => setPlatformDid(e.target.value)}
                          disabled={!!createdAgent}
                        />
                      </div>
                    )}
                    {callerIdPolicy === "custom" && (
                      <div className="form-group">
                        <label className="form-label">Custom Caller ID</label>
                        <input
                          className="form-input"
                          style={{ fontFamily: "var(--mono)", fontSize: ".75rem" }}
                          value={customCallerId}
                          onChange={(e) => setCustomCallerId(e.target.value)}
                          disabled={!!createdAgent}
                        />
                      </div>
                    )}
                    <div className="form-group">
                      <label className="form-label">Waiting Experience</label>
                      <select
                        className="form-select"
                        value={transferWaitingExperience || "announcement_moh"}
                        onChange={(e) =>
                          setTransferWaitingExperience(e.target.value as AgentUpdate["transfer_waiting_experience"])
                        }
                        disabled={!!createdAgent}
                      >
                        <option value="announcement_moh">Announcement + Hold Music</option>
                        <option value="announcement_silence">Announcement + Silence</option>
                      </select>
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      {step === "tools" && createdAgent && <ToolsPanel tenantId={createdAgent.tenant_id} agentId={createdAgent.id} />}

      {step === "knowledge-base" && createdAgent && (
        <KnowledgeBasePanel tenantId={createdAgent.tenant_id} agentId={createdAgent.id} />
      )}

      {step === "sip" && createdAgent && <SipPanel tenantId={createdAgent.tenant_id} agentId={createdAgent.id} />}

      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 14 }}>
        {step === "setup" && !createdAgent && (
          <button
            className="btn btn-primary btn-sm"
            onClick={handleCreate}
            disabled={creating || !tenantSlug || !slug || !name || !greeting.trim() || !systemPrompt.trim()}
          >
            {creating ? "Creating…" : "Create Agent"}
          </button>
        )}
        {createdAgent && (
          <button className="btn btn-primary btn-sm" onClick={finish}>
            Done — Go to Agent
          </button>
        )}
      </div>
    </>
  );
}
