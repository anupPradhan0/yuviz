"use client";

// Easy agent creation: six plain steps from "pick a job" to a live
// receptionist. The server owns what the receptionist says (shipped jobs,
// the prompt rules, Fix and Undo); this component only collects choices and
// shows results. Every visible string comes from lib/easyCopy, and a server
// `detail` is never rendered, only mapped to easyCopy text.

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  Agent,
  AgentFromTemplateRequest,
  AgentTemplateInfo,
  ApiError,
  PromptRevision,
  ProviderConfig,
  acceptPrompt,
  createAgentFromTemplate,
  listAgentTemplates,
  listAgents,
  listProviders,
  revisePrompt,
  undoPrompt,
  updateAgent,
} from "@/lib/api";
import { useActiveTenant } from "@/lib/useActiveTenant";
import { LANGUAGES } from "@/lib/engineCatalog";
import { easyCopy, easyErrorText } from "@/lib/easyCopy";
import { EasyTestStep } from "@/components/EasyTestStep";

type Role = "llm" | "stt" | "tts";

const ROLES: Role[] = ["llm", "stt", "tts"];
const ROLE_LABEL: Record<Role, string> = {
  tts: easyCopy.voiceLabel,
  llm: easyCopy.aiServiceLabel,
  stt: easyCopy.speechRecognitionLabel,
};
const MISSING_ROLE_TEXT: Record<Role, string> = {
  llm: easyCopy.connectAiService,
  stt: easyCopy.setUpSpeechRecognition,
  tts: easyCopy.setUpVoice,
};
const ADD_CONFIG_HREF = "/ai-voice";
const MAX_FACTS = 1000;

const STEP_JOB = 0;
const STEP_BUSINESS = 1;
const STEP_SPEAKS = 2;
const STEP_TEST = 3;
const STEP_FIX = 4;
const STEP_LIVE = 5;

export function EasyAgentFlow({ onAdvanced }: { onAdvanced: () => void }) {
  const router = useRouter();
  const { tenant, isAllTenants, loading: tenantLoading } = useActiveTenant();

  const [step, setStep] = useState(STEP_JOB);
  const [templates, setTemplates] = useState<AgentTemplateInfo[]>([]);
  const [providers, setProviders] = useState<ProviderConfig[] | null>(null);
  const [recentAgent, setRecentAgent] = useState<Agent | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [templateId, setTemplateId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [businessName, setBusinessName] = useState("");
  const [facts, setFacts] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [language, setLanguage] = useState("");
  const [picked, setPicked] = useState<Partial<Record<Role, string>>>({});

  const [agent, setAgent] = useState<Agent | null>(null);
  const [busy, setBusy] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const [testSessionId, setTestSessionId] = useState<string | null>(null);
  const [problem, setProblem] = useState("");
  const [revision, setRevision] = useState<PromptRevision | null>(null);
  const [fixError, setFixError] = useState<string | null>(null);
  const [fixNote, setFixNote] = useState<string | null>(null);
  const [exampleShown, setExampleShown] = useState(false);

  const [live, setLive] = useState(false);
  const [liveError, setLiveError] = useState<string | null>(null);

  // Independent loads, each failing on its own (lesson 21): the job list does
  // not depend on the account, and the recent-agent default is optional.
  useEffect(() => {
    listAgentTemplates()
      .then(setTemplates)
      .catch((e) => setLoadError(easyErrorText(e)));
  }, []);

  useEffect(() => {
    if (!tenant) return;
    listProviders(tenant.id)
      .then(setProviders)
      .catch((e) => setLoadError(easyErrorText(e)));
    listAgents(tenant.slug)
      .then((agents) => {
        const newest = [...agents].sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
        setRecentAgent(newest ?? null);
      })
      // Only a convenience default; without it the dropdowns start unselected.
      .catch(() => setRecentAgent(null));
  }, [tenant]);

  const template = templates.find((t) => t.id === templateId) ?? null;
  const configsFor = (role: Role) => (providers ?? []).filter((p) => p.role === role);
  const rolesNeeded = (t: AgentTemplateInfo) => ROLES.filter((r) => r === "llm" || t.needs.includes(r));
  const missingRole = (t: AgentTemplateInfo) => rolesNeeded(t).find((r) => configsFor(r).length === 0);

  // The assigned config per role: the only one when there is one, else the
  // user's pick, else the newest receptionist's config if it still exists here.
  const chosen = (role: Role): string => {
    const configs = configsFor(role);
    if (configs.length === 1) return configs[0].id;
    const fromRecent = recentAgent?.[`${role}_config_id`] ?? "";
    return picked[role] ?? (configs.some((c) => c.id === fromRecent) ? fromRecent : "");
  };
  const dropdownRoles = template ? rolesNeeded(template).filter((r) => configsFor(r).length > 1) : [];
  const speaksComplete = dropdownRoles.every((r) => chosen(r) !== "");

  const validateBusiness = (): string | null => {
    if (name.trim() === "" || !/[a-z0-9]/i.test(name)) return easyCopy.nameRequired;
    if (businessName.trim() === "") return easyCopy.businessNameRequired;
    if (facts.length > MAX_FACTS) return easyCopy.factsTooLong;
    return null;
  };

  const handleBusinessContinue = () => {
    const error = validateBusiness();
    setFieldError(error);
    if (!error) setStep(STEP_SPEAKS);
  };

  const handleCreate = async () => {
    if (!tenant || !template) return;
    const error = validateBusiness();
    if (error) {
      setFieldError(error);
      setStep(STEP_BUSINESS);
      return;
    }
    const gap = missingRole(template);
    if (gap) {
      setCreateError(MISSING_ROLE_TEXT[gap]);
      return;
    }
    const body: AgentFromTemplateRequest = {
      template_id: template.id,
      template_version: template.version,
      name: name.trim(),
      business_name: businessName.trim(),
      business_facts: facts,
      language: language || null,
    };
    for (const role of rolesNeeded(template)) body[`${role}_config_id`] = chosen(role);
    setBusy(true);
    setCreateError(null);
    try {
      setAgent(await createAgentFromTemplate(tenant.slug, body));
      setStep(STEP_TEST);
    } catch (e) {
      setCreateError(e instanceof ApiError && e.status === 409 ? easyCopy.nameTaken : easyErrorText(e));
    } finally {
      setBusy(false);
    }
  };

  const handleSuggestFix = async () => {
    if (!tenant || !agent || !testSessionId) return;
    setBusy(true);
    setFixError(null);
    setFixNote(null);
    setExampleShown(false);
    try {
      setRevision(await revisePrompt(tenant.slug, agent.id, { session_id: testSessionId, problem: problem.trim() }));
    } catch (e) {
      setFixError(easyErrorText(e));
      setExampleShown(e instanceof ApiError && e.detail === "customer_data");
    } finally {
      setBusy(false);
    }
  };

  const handleAccept = async () => {
    if (!tenant || !agent || !testSessionId || !revision) return;
    setBusy(true);
    setFixError(null);
    try {
      setAgent(
        await acceptPrompt(tenant.slug, agent.id, {
          session_id: testSessionId,
          problem: problem.trim(),
          proposed_prompt: revision.after,
          base_prompt_sha256: revision.base_prompt_sha256,
        }),
      );
      setRevision(null);
      setProblem("");
      setFixNote(easyCopy.fixAccepted);
    } catch (e) {
      setFixError(e instanceof ApiError && e.status === 400 ? easyCopy.acceptRefused : easyErrorText(e));
    } finally {
      setBusy(false);
    }
  };

  const handleDiscard = () => {
    setRevision(null);
    setFixError(null);
  };

  const handleUndo = async () => {
    if (!tenant || !agent) return;
    setBusy(true);
    setFixError(null);
    try {
      setAgent(await undoPrompt(tenant.slug, agent.id));
      setFixNote(easyCopy.fixUndone);
    } catch (e) {
      setFixError(easyErrorText(e));
    } finally {
      setBusy(false);
    }
  };

  const handlePutToWork = async () => {
    if (!tenant || !agent) return;
    setBusy(true);
    setLiveError(null);
    try {
      setAgent(await updateAgent(tenant.slug, agent.id, { status: "active" }));
      setLive(true);
    } catch (e) {
      setLiveError(easyErrorText(e));
    } finally {
      setBusy(false);
    }
  };

  const channelIsChat = template?.channel === "chat";

  const renderJob = (t: AgentTemplateInfo) => {
    const gap = providers === null ? undefined : missingRole(t);
    const selected = t.id === templateId;
    return (
      <div key={t.id} className="card" style={selected ? { borderColor: "var(--cyan)" } : undefined}>
        <button
          type="button"
          className="card-body"
          style={{ width: "100%", textAlign: "left", background: "none", border: 0, cursor: gap ? "default" : "pointer" }}
          disabled={providers === null || gap !== undefined}
          aria-pressed={selected}
          onClick={() => setTemplateId(t.id)}
        >
          <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 4 }}>
            <strong>{t.label}</strong>
            <span className="badge gray">{easyCopy.channelLabels[t.channel]}</span>
          </div>
          <div className="form-hint">{t.blurb}</div>
        </button>
        {gap && (
          <div className="card-body" style={{ paddingTop: 0 }}>
            <span className="form-hint">{MISSING_ROLE_TEXT[gap]} </span>
            <Link href={ADD_CONFIG_HREF}>{easyCopy.addItLink}</Link>
          </div>
        )}
      </div>
    );
  };

  if (!tenantLoading && tenant === null) {
    return (
      <>
        <TopBar onCancel={() => router.push("/agents")} />
        <div className="empty-state">{isAllTenants ? easyCopy.chooseAccount : easyCopy.generic}</div>
      </>
    );
  }

  return (
    <>
      <TopBar onCancel={() => router.push("/agents")} onAdvanced={agent ? undefined : onAdvanced} />

      <div className="tabs">
        {easyCopy.stepTitles.map((title, i) => (
          <div
            key={title}
            className={`tab${step === i ? " active" : ""}`}
            style={step === i ? { cursor: "default" } : { cursor: "default", opacity: 0.5 }}
            aria-current={step === i ? "step" : undefined}
          >
            {i + 1}. {title}
          </div>
        ))}
      </div>

      {loadError && <div className="error-banner">{loadError}</div>}

      {step === STEP_JOB && (
        <>
          <div style={{ display: "grid", gap: 10 }}>
            {templates.length === 0 && !loadError && <div className="empty-state">{easyCopy.loading}</div>}
            {templates.map(renderJob)}
          </div>
          {template && (
            <div className="card" style={{ marginTop: 14 }}>
              <div className="card-body" style={{ display: "grid", gap: 12 }}>
                <div>
                  <div className="form-label">{easyCopy.whatItDoes}</div>
                  <div>{template.does}</div>
                </div>
                <div>
                  <div className="form-label">{easyCopy.whatItWontDo}</div>
                  <div>{template.wont_do}</div>
                </div>
                <div>
                  <div className="form-label">{easyCopy.whenItHandsOff}</div>
                  <div>{template.handoff}</div>
                </div>
              </div>
            </div>
          )}
        </>
      )}

      {step === STEP_BUSINESS && (
        <div className="card">
          <div className="card-body">
            <div className="form-group">
              <label className="form-label" htmlFor="easy-name">{easyCopy.nameLabel}</label>
              <input
                id="easy-name"
                className="form-input"
                autoFocus
                maxLength={80}
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div className="form-group">
              <label className="form-label" htmlFor="easy-business">{easyCopy.businessNameLabel}</label>
              <input
                id="easy-business"
                className="form-input"
                maxLength={120}
                value={businessName}
                onChange={(e) => setBusinessName(e.target.value)}
              />
            </div>
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label className="form-label" htmlFor="easy-facts">
                {easyCopy.businessFactsLabel} <span className="hint">{easyCopy.businessFactsHint}</span>
              </label>
              <textarea
                id="easy-facts"
                className="form-textarea"
                style={{ minHeight: 110 }}
                value={facts}
                onChange={(e) => setFacts(e.target.value)}
              />
              <div className="form-hint">{facts.length} / {MAX_FACTS}</div>
            </div>
            {fieldError && <div className="error-banner" style={{ marginTop: 10 }}>{fieldError}</div>}
          </div>
        </div>
      )}

      {step === STEP_SPEAKS && (
        <div className="card">
          <div className="card-body">
            <div className="form-group" style={dropdownRoles.length ? undefined : { marginBottom: 0 }}>
              <label className="form-label" htmlFor="easy-language">{easyCopy.languageLabel}</label>
              <select
                id="easy-language"
                className="form-select"
                value={language}
                onChange={(e) => setLanguage(e.target.value)}
              >
                <option value="">{easyCopy.languageAutomatic}</option>
                {LANGUAGES.map((l) => (
                  <option key={l.value} value={l.value}>{l.label}</option>
                ))}
              </select>
            </div>
            {dropdownRoles.map((role, i) => (
              <div key={role} className="form-group" style={i === dropdownRoles.length - 1 ? { marginBottom: 0 } : undefined}>
                <label className="form-label" htmlFor={`easy-${role}`}>{ROLE_LABEL[role]}</label>
                <select
                  id={`easy-${role}`}
                  className="form-select"
                  value={chosen(role)}
                  onChange={(e) => setPicked((prev) => ({ ...prev, [role]: e.target.value }))}
                >
                  <option value="" disabled>{easyCopy.chooseOne}</option>
                  {configsFor(role).map((c) => (
                    <option key={c.id} value={c.id}>{c.name}</option>
                  ))}
                </select>
              </div>
            ))}
            {createError && <div className="error-banner" style={{ marginTop: 10 }}>{createError}</div>}
          </div>
        </div>
      )}

      {step === STEP_TEST && agent && tenant && template && (
        <EasyTestStep
          tenantSlug={tenant.slug}
          agent={agent}
          channel={channelIsChat ? "chat" : "voice"}
          onSession={setTestSessionId}
        />
      )}

      {step === STEP_FIX && agent && (
        <div className="card">
          <div className="card-body">
            {testSessionId === null ? (
              <div>{easyCopy.testFirst}</div>
            ) : !agent.prompt_fixable ? (
              <div>{easyCopy.notFixable}</div>
            ) : (
              <>
                <div className="form-group" style={{ marginBottom: 10 }}>
                  <label className="form-label" htmlFor="easy-problem">{easyCopy.problemLabel}</label>
                  <textarea
                    id="easy-problem"
                    className="form-textarea"
                    style={{ minHeight: 90 }}
                    maxLength={1000}
                    placeholder={easyCopy.problemPlaceholder}
                    value={problem}
                    disabled={revision !== null}
                    onChange={(e) => setProblem(e.target.value)}
                  />
                </div>
                {revision === null && (
                  <button
                    className="btn btn-primary btn-sm"
                    onClick={handleSuggestFix}
                    disabled={busy || problem.trim() === ""}
                  >
                    {busy ? easyCopy.working : easyCopy.suggestFix}
                  </button>
                )}
              </>
            )}
            {revision !== null && (
              <div style={{ marginTop: 12 }}>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                  <div>
                    <div className="form-label">{easyCopy.beforeLabel}</div>
                    <pre style={{ whiteSpace: "pre-wrap", fontSize: ".75rem" }}>{revision.before}</pre>
                  </div>
                  <div>
                    <div className="form-label">{easyCopy.afterLabel}</div>
                    <pre style={{ whiteSpace: "pre-wrap", fontSize: ".75rem" }}>{revision.after}</pre>
                  </div>
                </div>
                <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
                  <button className="btn btn-primary btn-sm" onClick={handleAccept} disabled={busy}>
                    {easyCopy.acceptFix}
                  </button>
                  <button className="btn btn-ghost btn-sm" onClick={handleDiscard} disabled={busy}>
                    {easyCopy.discardFix}
                  </button>
                </div>
              </div>
            )}
            {agent.can_undo && revision === null && (
              <button className="btn btn-ghost btn-sm" style={{ marginTop: 10 }} onClick={handleUndo} disabled={busy}>
                {easyCopy.undoFix}
              </button>
            )}
            {fixNote && <div className="form-hint" style={{ marginTop: 10 }} role="status">{fixNote}</div>}
            {fixError && <div className="error-banner" style={{ marginTop: 10 }}>{fixError}</div>}
            {exampleShown && <div className="form-hint">{easyCopy.customerDataExample}</div>}
          </div>
        </div>
      )}

      {step === STEP_LIVE && agent && tenant && (
        <div className="card">
          <div className="card-body">
            {live ? (
              <>
                <strong>{easyCopy.liveTitle}</strong>
                <p>{channelIsChat ? easyCopy.liveNoteChat : easyCopy.liveNoteCalls}</p>
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  <Link href={channelIsChat ? `/agents/${tenant.slug}/${agent.slug}` : "/phone-numbers"}>
                    {channelIsChat ? easyCopy.assignChatLink : easyCopy.assignNumberLink}
                  </Link>
                  <Link href={`/agents/${tenant.slug}/${agent.slug}`}>{easyCopy.passedOnCallsLink}</Link>
                </div>
              </>
            ) : (
              <button className="btn btn-primary btn-sm" onClick={handlePutToWork} disabled={busy}>
                {busy ? easyCopy.activating : easyCopy.putToWork}
              </button>
            )}
            {liveError && <div className="error-banner" style={{ marginTop: 10 }}>{liveError}</div>}
          </div>
        </div>
      )}

      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 14 }}>
        {step !== STEP_JOB && step !== STEP_TEST && !live && (
          <button className="btn btn-ghost btn-sm" onClick={() => setStep(step - 1)} disabled={busy}>
            {easyCopy.back}
          </button>
        )}
        {step === STEP_JOB && (
          <button className="btn btn-primary btn-sm" onClick={() => setStep(STEP_BUSINESS)} disabled={!template}>
            {easyCopy.continue}
          </button>
        )}
        {step === STEP_BUSINESS && (
          <button className="btn btn-primary btn-sm" onClick={handleBusinessContinue}>
            {easyCopy.continue}
          </button>
        )}
        {step === STEP_SPEAKS && (
          <button className="btn btn-primary btn-sm" onClick={handleCreate} disabled={busy || !speaksComplete}>
            {busy ? easyCopy.creating : easyCopy.continue}
          </button>
        )}
        {step === STEP_TEST && (
          <button className="btn btn-primary btn-sm" onClick={() => setStep(STEP_FIX)}>
            {easyCopy.continue}
          </button>
        )}
        {step === STEP_FIX && (
          <button className="btn btn-primary btn-sm" onClick={() => setStep(STEP_LIVE)} disabled={busy || revision !== null}>
            {easyCopy.continue}
          </button>
        )}
      </div>
    </>
  );
}

function TopBar({ onCancel, onAdvanced }: { onCancel: () => void; onAdvanced?: () => void }) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14 }}>
      <button className="btn btn-ghost btn-sm" onClick={onCancel}>
        {easyCopy.cancel}
      </button>
      {onAdvanced && (
        <button className="btn btn-ghost btn-sm" onClick={onAdvanced}>
          {easyCopy.advancedLink}
        </button>
      )}
    </div>
  );
}
