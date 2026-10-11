"use client";

// Agent Studio. Opening an agent goes to its config tabs; its conversation-steps canvas lives under /workflows.
// Cards show only stored values — no invented metrics like containment rate.

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { Bot, MoreVertical, Pencil, Play, Plus, Workflow } from "lucide-react";
import { Agent, ApiError, CallListResult, listAgents, listCalls, listPhoneNumbers } from "@/lib/api";
import { outcomeOf } from "@/lib/callOutcome";
import { AgentTestPopup } from "@/components/AgentTestPopup";
import { useActiveTenant } from "@/lib/useActiveTenant";
import { listAgentKnowledgeBases } from "@/lib/knowledgeApi";
import { listAgentCustomApis } from "@/lib/toolexecApi";
import { AGENT_TEMPLATES, agentDirection, agentIcon, agentTemplate } from "@/lib/agentTemplates";
import { LANGUAGES } from "@/lib/engineCatalog";
import { AgentDraft, clearAgentDraft, draftSavedLabel, loadAgentDraft } from "@/lib/agentDraft";

interface AgentRow extends Agent {
  tenantName: string;
  tenantSlug: string;
}

interface Attachments {
  sources: number | null; // null = the lookup failed; render "—", never 0
  tools: number | null;
  calls: CallStats | null;
  numbers: string[] | null;
}

interface CallStats {
  real: number;
  tests: number;
  avgMs: number | null;
  okPct: number | null; // share of finished real calls that ended normally
  partial: boolean; // more calls exist than the sampled page
}

const STATS_SAMPLE = 200;

function callStats(r: CallListResult): CallStats {
  const real = r.items.filter((c) => (c.direction as string) !== "test");
  const finished = real.filter((c) => c.status === "completed" && outcomeOf(c.close_reason).key !== "unrecorded");
  const durations = real.map((c) => c.duration_ms).filter((d): d is number => d != null);
  return {
    real: real.length,
    tests: r.items.length - real.length,
    avgMs: durations.length ? durations.reduce((s, d) => s + d, 0) / durations.length : null,
    okPct: finished.length
      ? Math.round((100 * finished.filter((c) => outcomeOf(c.close_reason).key === "done").length) / finished.length)
      : null,
    partial: r.total > r.items.length,
  };
}

const fmtDuration = (ms: number) => {
  const s = Math.round(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};

export default function AgentsPage() {
  const router = useRouter();
  const { tenant, allTenants, isPlatformScoped, isAllTenants, loading: tenantLoading } = useActiveTenant();
  const [agents, setAgents] = useState<AgentRow[]>([]);
  const [attachments, setAttachments] = useState<Record<string, Attachments>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [draft, setDraft] = useState<AgentDraft | null>(null);
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [testing, setTesting] = useState<AgentRow | null>(null);

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

      // No bulk endpoint for attachment counts; failures degrade to "—" per agent.
      const tenantNumbers = new Map(
        await Promise.all(
          targets.map(async (t) => [t.id, await listPhoneNumbers(t.id).catch(() => null)] as const),
        ),
      );
      const entries = await Promise.all(
        list.map(async (a) => {
          const [kbs, apis, calls] = await Promise.all([
            listAgentKnowledgeBases(a.id).then((r) => r.length).catch(() => null),
            listAgentCustomApis(a.id).then((r) => r.length).catch(() => null),
            listCalls(a.tenantSlug, { agentId: a.id, limit: STATS_SAMPLE }).then(callStats).catch(() => null),
          ]);
          const nums = tenantNumbers.get(a.tenant_id);
          const numbers = nums ? nums.filter((n) => n.agent_id === a.id).map((n) => n.did) : null;
          return [a.id, { sources: kbs, tools: apis, calls, numbers }] as const;
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
            const Icon = agentIcon(a);
            const tpl = agentTemplate(a);
            const direction = agentDirection(a);
            const editUrl = `/agents/${a.tenantSlug}/${a.slug}`;
            return (
              <div
                key={a.id}
                className={`agent-card${a.status === "active" ? "" : " paused"}`}
                role="link"
                tabIndex={0}
                onClick={() => router.push(editUrl)}
                onKeyDown={(e) => e.key === "Enter" && e.target === e.currentTarget && router.push(editUrl)}
              >
                <div className="agent-card-top" onClick={(e) => e.stopPropagation()}>
                  <div className="agent-card-avatar"><Icon size={22} /></div>
                  <button className="agent-card-icon-btn" aria-label={`Test ${a.name}`} title="Test it" onClick={() => setTesting(a)}>
                    <Play size={15} />
                  </button>
                  <div className="ed2-menu">
                    <button
                      className="agent-card-icon-btn"
                      aria-label="More actions"
                      aria-expanded={menuFor === a.id}
                      onClick={() => setMenuFor(menuFor === a.id ? null : a.id)}
                    >
                      <MoreVertical size={15} />
                    </button>
                    {menuFor === a.id && (
                      <>
                        <div className="ed2-menu-backdrop" onClick={() => setMenuFor(null)} />
                        <div className="ed2-menu-pop" role="menu">
                          <button role="menuitem" onClick={() => router.push(editUrl)}>
                            <Pencil size={13} /> Edit agent
                          </button>
                          <button role="menuitem" onClick={() => router.push(`/workflows/${a.tenantSlug}/${a.slug}`)}>
                            <Workflow size={13} /> Conversation steps
                          </button>
                        </div>
                      </>
                    )}
                  </div>
                </div>

                <div className="agent-card-name">{a.name}</div>
                {isAllTenants && <div className="agent-card-acct">{a.tenantName}</div>}

                <div className="agent-card-meta">
                  {direction && (
                    <span className={`badge ${direction === "inbound" ? "cyan" : "indigo"}`}>
                      {direction === "inbound" ? "Inbound" : "Outbound"}
                    </span>
                  )}
                  <span className="badge gray">{tpl?.label ?? "Custom"}</span>
                  {att?.numbers != null && (att.numbers.length === 0
                    ? <span className="badge gray">No number</span>
                    : att.numbers.map((n) => <span key={n} className="badge green">{n}</span>))}
                </div>

                <div className="agent-card-facts">
                  {language && <span>{language}</span>}
                  {att?.sources != null && (
                    <span className={att.sources === 0 ? "muted" : ""}>
                      {att.sources === 0 ? "No knowledge added" : `${att.sources} knowledge source${att.sources === 1 ? "" : "s"}`}
                    </span>
                  )}
                  {!!att?.tools && <span>{att.tools} connection{att.tools === 1 ? "" : "s"}</span>}
                  {a.transfer_type !== "none" && <span>Can transfer to a person</span>}
                </div>

                {att?.calls && (
                  <div className="agent-card-stats" title={att.calls.partial ? `Based on the latest ${STATS_SAMPLE} calls` : undefined}>
                    <div><b>{att.calls.real}{att.calls.partial ? "+" : ""}</b><span>Calls</span></div>
                    <div><b>{att.calls.tests}</b><span>Tests</span></div>
                    <div><b>{att.calls.avgMs == null ? "—" : fmtDuration(att.calls.avgMs)}</b><span>Avg length</span></div>
                    <div>
                      <b className={att.calls.okPct == null ? "" : att.calls.okPct >= 80 ? "good" : att.calls.okPct >= 50 ? "warn" : "bad"}>
                        {att.calls.okPct == null ? "—" : `${att.calls.okPct}%`}
                      </b>
                      <span>Ended well</span>
                    </div>
                  </div>
                )}

                <div className="agent-card-foot">
                  <span className={`agent-card-status${a.status === "active" ? " on" : ""}`}>
                    <i />{a.status === "active" ? "Live" : "Paused"}
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {testing && (
        <AgentTestPopup
          key={testing.id}
          tenantSlug={testing.tenantSlug}
          agentSlug={testing.slug}
          name={testing.name}
          icon={agentIcon(testing)}
          onClose={() => setTesting(null)}
        />
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
