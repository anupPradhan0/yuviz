"use client";

// Agent Studio. Opening an agent goes to its config tabs; the call-flow canvas lives under /workflows.
// Cards show only stored values — no invented metrics like containment rate.

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { BookOpen, Bot, Globe, Mic, Pencil, PhoneForwarded, Plug, Plus, Volume2 } from "lucide-react";
import { Agent, ApiError, ProviderConfig, listAgents, listProviders } from "@/lib/api";
import { useActiveTenant } from "@/lib/useActiveTenant";
import { listAgentKnowledgeBases } from "@/lib/knowledgeApi";
import { listAgentCustomApis } from "@/lib/toolexecApi";
import { AGENT_TEMPLATES } from "@/lib/agentTemplates";
import { BUILTIN_TTS_ENGINE, LANGUAGES } from "@/lib/engineCatalog";
import { AgentDraft, clearAgentDraft, draftSavedLabel, loadAgentDraft } from "@/lib/agentDraft";

interface AgentRow extends Agent {
  tenantName: string;
  tenantSlug: string;
}

interface Attachments {
  sources: number | null; // null = the lookup failed; render "—", never 0
  tools: number | null;
}

export default function AgentsPage() {
  const router = useRouter();
  const { tenant, allTenants, isPlatformScoped, isAllTenants, loading: tenantLoading } = useActiveTenant();
  const [agents, setAgents] = useState<AgentRow[]>([]);
  const [providersById, setProvidersById] = useState<Record<string, ProviderConfig>>({});
  const [attachments, setAttachments] = useState<Record<string, Attachments>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [draft, setDraft] = useState<AgentDraft | null>(null);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setDraft(loadAgentDraft());
  }, []);

  // allSettled: one failing tenant must not blank the others.
  useEffect(() => {
    if (tenantLoading) return;
    const targets = isAllTenants ? allTenants : tenant ? [tenant] : [];
    if (targets.length === 0) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setAgents([]);
      setLoading(false);
      return;
    }
     
    setLoading(true);
    (async () => {
      const results = await Promise.allSettled(
        targets.map((t) =>
          listAgents(t.slug).then((found) =>
            found.map((a): AgentRow => ({ ...a, tenantName: t.name, tenantSlug: t.slug })),
          ),
        ),
      );
      const list: AgentRow[] = [];
      const errs: string[] = [];
      results.forEach((r, i) => {
        if (r.status === "fulfilled") list.push(...r.value);
        else errs.push(`${targets[i].name}: ${r.reason instanceof ApiError ? r.reason.detail : String(r.reason)}`);
      });
      setAgents(list);
      setError(errs.length > 0 ? errs.join("; ") : null);
      setLoading(false);

      const provResults = await Promise.allSettled(targets.map((t) => listProviders(t.id)));
      const provs = provResults.flatMap((r) => (r.status === "fulfilled" ? r.value : []));
      setProvidersById(Object.fromEntries(provs.map((p) => [p.id, p])));

      // No bulk endpoint for attachment counts; failures degrade to "—" per agent.
      const entries = await Promise.all(
        list.map(async (a) => {
          const [kbs, apis] = await Promise.all([
            listAgentKnowledgeBases(a.id).then((r) => r.length).catch(() => null),
            listAgentCustomApis(a.id).then((r) => r.length).catch(() => null),
          ]);
          return [a.id, { sources: kbs, tools: apis }] as const;
        }),
      );
      setAttachments(Object.fromEntries(entries));
    })();
  }, [tenant, allTenants, isAllTenants, tenantLoading]);

  const rows = useMemo(() => {
    const q = search.trim().toLowerCase();
    const matched = q
      ? agents.filter((a) => `${a.name} ${a.tenantName}`.toLowerCase().includes(q))
      : agents;
    return [...matched].sort(
      (a, b) =>
        Number(b.status === "active") - Number(a.status === "active") || a.name.localeCompare(b.name),
    );
  }, [agents, search]);

  // No voice on the agent means the account's default voice is used.
  const voiceLabel = (id: string | null) => {
    const p = id ? providersById[id] : undefined;
    if (!p) return "Default voice";
    return p.engine === BUILTIN_TTS_ENGINE ? "Built-in voice" : p.name;
  };

  const accountLine = isAllTenants
    ? `${agents.length} agent${agents.length === 1 ? "" : "s"} across ${allTenants.length} account${allTenants.length === 1 ? "" : "s"}.` +
      " Each has its own voice, knowledge and rules."
    : tenant
      ? `${agents.length} agent${agents.length === 1 ? "" : "s"} in ${tenant.name}.` +
        (isPlatformScoped ? " Switch accounts from the header." : "") +
        " Each has its own voice, knowledge and rules."
      : "Each agent has its own voice, knowledge and rules.";

  return (
    <>
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "flex-start", gap: 12, marginBottom: 18 }}>
        <div>
          <h1 style={{ fontSize: "1.5rem", fontWeight: 600, margin: 0 }}>AI agents</h1>
          <div className="form-hint" style={{ marginTop: 4 }}>{accountLine}</div>
        </div>
        <input
          className="form-input"
          style={{ width: 200, marginLeft: "auto" }}
          placeholder="Search agents…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {draft && (
        <div className="draft-banner">
          <span>
            You have an unfinished agent{draft.name.trim() ? <> called <b>{draft.name.trim()}</b></> : ""}, saved{" "}
            {draftSavedLabel(draft.savedAt)}.
          </span>
          <button className="btn btn-primary btn-sm" onClick={() => router.push("/agents/new?mode=advanced")}>Continue</button>
          <button
            className="btn btn-ghost btn-sm"
            onClick={() => {
              clearAgentDraft();
              setDraft(null);
            }}
          >
            Discard
          </button>
        </div>
      )}

      {error && <div className="error-banner">{error}</div>}

      {loading ? (
        <div className="empty-state">Loading…</div>
      ) : rows.length === 0 ? (
        <div className="agent-empty">
          <div className="agent-empty-ico"><Bot size={22} /></div>
          <h2>{search.trim() ? "No agents match your search" : "Create your first agent"}</h2>
          <p>
            {search.trim()
              ? "Try a different name, or clear the search."
              : "An agent answers or places phone calls for you. Pick a template below to get going in a minute."}
          </p>
        </div>
      ) : (
        <div className="agent-card-grid">
          {rows.map((a) => {
            const att = attachments[a.id];
            const language = LANGUAGES.find((l) => l.value === a.language)?.label ?? a.language;
            return (
              <div key={a.id} className={`agent-card${a.status === "active" ? "" : " paused"}`}>
                <div className="agent-card-top">
                  <div className="agent-card-avatar">{a.name.trim()[0]?.toUpperCase() ?? "A"}</div>
                  <div className="agent-card-id">
                    <div className="agent-card-name">{a.name}</div>
                    {isAllTenants && <div className="agent-card-acct">{a.tenantName}</div>}
                  </div>
                  <span className={`agent-card-status${a.status === "active" ? " on" : ""}`}>
                    <i />{a.status === "active" ? "Live" : "Paused"}
                  </span>
                </div>

                <div className="agent-card-lbl">Opens with</div>
                <div className="agent-card-quote">
                  {a.greeting?.trim() ? `“${a.greeting.trim()}”` : "No opening line yet."}
                </div>

                <div className="agent-card-facts">
                  <span><Volume2 size={13} />{voiceLabel(a.tts_config_id)}</span>
                  {language && <span><Globe size={13} />{language}</span>}
                  {att?.sources != null && (
                    <span className={att.sources === 0 ? "muted" : ""}>
                      <BookOpen size={13} />
                      {att.sources === 0 ? "No knowledge yet" : `${att.sources} knowledge source${att.sources === 1 ? "" : "s"}`}
                    </span>
                  )}
                  {!!att?.tools && (
                    <span><Plug size={13} />{att.tools} connection{att.tools === 1 ? "" : "s"}</span>
                  )}
                  {a.transfer_type !== "none" && <span><PhoneForwarded size={13} />Can transfer to a person</span>}
                </div>

                <div className="agent-card-actions">
                  <button
                    className="btn btn-ghost btn-sm"
                    onClick={() => router.push(`/agents/${a.tenantSlug}/${a.slug}`)}
                  >
                    <Pencil size={13} /> Edit
                  </button>
                  <button
                    className="btn btn-primary btn-sm"
                    onClick={() => router.push(`/agents/${a.tenantSlug}/${a.slug}/test`)}
                  >
                    <Mic size={13} /> Test it
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {!loading && (
        <section className="agent-templates">
          <div className="agent-templates-hdr">
            <h2>{rows.length === 0 && !search.trim() ? "Start from a template" : "Create another agent"}</h2>
            <span>Pre-filled for you. Change anything later.</span>
          </div>
          <div className="agent-template-row">
            {AGENT_TEMPLATES.map((t) => (
              <button
                key={t.key}
                type="button"
                className="agent-template-card"
                onClick={() => router.push(`/agents/new?template=${t.key}`)}
              >
                <i className="tpl-ico"><t.icon size={15} /></i>
                <div className="agent-template-title">{t.label}</div>
                <div className="agent-template-blurb">{t.blurb}</div>
              </button>
            ))}
            <button type="button" className="agent-template-card blank" onClick={() => router.push("/agents/new")}>
              <i className="tpl-ico"><Plus size={15} /></i>
              <div className="agent-template-title">Start blank</div>
              <div className="agent-template-blurb">Describe the job in your own words.</div>
            </button>
          </div>
        </section>
      )}
    </>
  );
}
