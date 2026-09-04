"use client";

// The workflow editor: canvas, inspector, autosave, publish.
//
// No state manager. React Flow's own useNodesState/useEdgesState plus a
// handful of useState is the whole thing (docs/workflow.md Part 6) — this is
// a tab inside an agent page, and a store would be a dependency plus an
// indirection layer for state that never leaves this panel.
//
// The draft/published split is the point of the whole tab: typing here
// autosaves to workflow_draft, which no call ever reads. Publish validates
// server-side and only then writes the graph live calls execute. Because
// autosave means there is no Cancel, this panel owns an undo stack.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import {
  addEdge,
  Background,
  BackgroundVariant,
  ControlButton,
  Controls,
  Panel as FlowPanel,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type Edge,
  type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import { TestAgentPanel } from "@/components/TestAgentPanel";
import { TextChatPanel } from "@/components/TextChatPanel";
import { ApiError, listAgentToolPolicies } from "@/lib/api";
import { listAgentKnowledgeBases } from "@/lib/knowledgeApi";
import {
  getWorkflow,
  publishErrors,
  publishWorkflow,
  saveWorkflowDraft,
  STARTER,
  validateWorkflow,
  type WorkflowEdgeData,
  type WorkflowError,
  type WorkflowGraph,
  type WorkflowNodeData,
  type WorkflowNodeType,
} from "@/lib/workflowApi";
import { autoLayout } from "./autoLayout";
import { EditorContext } from "./editorContext";
import { edgeTypes } from "./edges";
import { Inspector, type Selection } from "./Inspector";
import { nodeTypes } from "./nodes";
import { VersionPanel } from "./VersionPanel";

const AUTOSAVE_MS = 1200;
// Checked against the same validator that gates a publish, so problems show
// up while the operator is still drawing instead of at the end. Shorter than
// autosave: seeing "this connection needs a condition" late is the whole
// complaint this replaces.
const VALIDATE_MS = 500;
// One drag = one undo step, not sixty. Anything inside this window collapses
// into the previous entry.
const UNDO_COALESCE_MS = 600;
const UNDO_LIMIT = 60;


// Always supplied per call by WorkflowRunner (libs/config_sdk/workflow.py's
// CALL_CONTEXT_VARIABLES) — kept in the same order the operator would think
// of them, not the Python one.
const CALL_CONTEXT_VARIABLES = [
  "caller_number", "called_number", "agent_name", "business_name",
  "current_date", "current_time", "direction",
];

const NEW_NODE_DEFAULTS: Record<Exclude<WorkflowNodeType, "start">, WorkflowNodeData> = {
  agent: { name: "new stage", prompt: "", tools: [], knowledge_base_ids: [] },
  transfer: { name: "to a human", prompt: "Tell the caller you're connecting them now.", transfer_destination: null },
  end: { name: "ended", prompt: "Close the call warmly.", disposition: "completed" },
  global: { name: "always applies", prompt: "" },
};

/** Key-order-independent serialization, for comparing a graph the editor
 *  built against the same graph after a JSONB round-trip. */
function canonicalize(value: unknown): string {
  return JSON.stringify(value, (_key, v) =>
    v && typeof v === "object" && !Array.isArray(v)
      ? Object.fromEntries(Object.entries(v as object).sort(([a], [b]) => a.localeCompare(b)))
      : v,
  );
}

// __invalid / __active are canvas-only markers stripped before persisting.
type RFNode = Node<WorkflowNodeData & { __invalid?: boolean; __active?: boolean }>;
type RFEdge = Edge<WorkflowEdgeData>;

function toReactFlow(graph: WorkflowGraph): { nodes: RFNode[]; edges: RFEdge[] } {
  return {
    nodes: graph.nodes.map((n) => ({
      id: n.id, type: n.type, position: n.position, data: { ...n.data },
      // The entry point is not something you can delete your way out of.
      deletable: n.type !== "start",
    })) as RFNode[],
    edges: graph.edges.map((e) => ({
      id: e.id, source: e.source, target: e.target,
      // React Flow renders `label` itself; the persisted copy stays in
      // data.label, which is what the backend reads.
      label: e.data.label, data: { ...e.data },
    })) as RFEdge[],
  };
}

function toGraph(nodes: RFNode[], edges: RFEdge[]): WorkflowGraph {
  return {
    version: 1,
    nodes: nodes.map((n) => {
      const { __invalid, __active, ...data } = n.data;
      void __invalid; void __active;   // canvas-only markers, never persisted
      return {
        id: n.id, type: (n.type || "agent") as WorkflowNodeType,
        position: n.position, data: data as WorkflowNodeData,
      };
    }),
    edges: edges.map((e) => {
      // Same strip as the nodes above. It matters more here than it looks:
      // onEdgeClick reads its selection off the *painted* edge, so editing
      // any connection copies the canvas-only __invalid marker into the
      // real edge — and from there into workflow_draft, the published
      // graph, and every version row. The backend ignores unknown keys, so
      // nothing breaks; it just quietly accumulates junk forever.
      const { __invalid, ...data } = (e.data || { label: "", condition: "" }) as WorkflowEdgeData;
      void __invalid;
      return {
        id: e.id, source: e.source, target: e.target,
        data: data as WorkflowEdgeData,
      };
    }),
  };
}

/** Set when the editor owns the whole page (app/workflows/[t]/[a]). The
 *  back link, the agent name and "Settings" then live in the editor's
 *  own toolbar instead of a second header row above it — one row, the way
 *  the reference editors do it. Omitted when the panel is embedded. */
export type WorkflowHeader = { title: string; backHref: string; settingsHref: string };

function Panel({
  tenantSlug, agentId, agentSlug, header,
}: { tenantSlug: string; agentId: string; agentSlug: string; header?: WorkflowHeader }) {
  const [nodes, setNodes, onNodesChange] = useNodesState<RFNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<RFEdge>([]);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [errors, setErrors] = useState<WorkflowError[]>([]);
  const [warnings, setWarnings] = useState<WorkflowError[]>([]);
  const [publishing, setPublishing] = useState(false);
  const [published, setPublished] = useState<string | null>(null);   // canonical live graph
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved">("idle");
  const [justPublished, setJustPublished] = useState(false);
  const [versionKey, setVersionKey] = useState(0);
  const [showHistory, setShowHistory] = useState(false);
  const [agentTools, setAgentTools] = useState<string[]>([]);
  const [knowledgeBases, setKnowledgeBases] = useState<{ id: string; name: string }[]>([]);
  const [addOpen, setAddOpen] = useState(false);
  // At most one always-applies node per flow — a second would concatenate
  // with the first in whatever order the nodes happen to be stored, which is
  // a prompt nobody wrote. The server rejects it too; this just stops the
  // operator drawing something that can't publish.
  const hasGlobal = nodes.some((n) => n.type === "global");
  const [moreOpen, setMoreOpen] = useState(false);
  const [showHelp, setShowHelp] = useState(false);
  // null = not testing. "call" opens the browser voice test, "chat" the
  // text one; both run the same session, and both light up the active
  // stage on the canvas as the conversation moves.
  const [testing, setTesting] = useState<"call" | "chat" | null>(null);
  // The node the live test call is in right now. The single
  // highest-value thing this editor shows: "the transition didn't fire"
  // stops being a guess.
  const [activeNodeId, setActiveNodeId] = useState<string | null>(null);
  // Suppresses the autosave that would otherwise fire from the very first
  // render's setNodes — an empty draft overwriting a real one.
  const loaded = useRef(false);
  const reactFlow = useReactFlow();

  useEffect(() => {
    loaded.current = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    Promise.all([
      getWorkflow(tenantSlug, agentId),
      listAgentToolPolicies(agentId).catch(() => []),
      listAgentKnowledgeBases(agentId).catch(() => []),
    ])
      .then(([state, policies, kbs]) => {
        const graph = state.workflow_draft ?? state.workflow ?? STARTER;
        const rf = toReactFlow(graph);
        setNodes(rf.nodes);
        setEdges(rf.edges);
        setPublished(state.workflow ? canonicalize(state.workflow) : null);
        setAgentTools(policies.filter((p) => p.enabled).map((p) => p.tool_name));
        setKnowledgeBases(kbs.map((kb) => ({ id: kb.kb_id, name: kb.kb_name })));
        // Someone who has never seen this tab gets told what it is once.
        setShowHelp(!state.workflow && !state.workflow_draft);
        loaded.current = true;
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantSlug, agentId]);

  // Everything the operator can legitimately drop into a prompt: the
  // per-call context plus whatever any stage captures. Offering exactly this
  // set is what stops {{ custmer_name }} reaching a call recording.
  const availableVariables = useMemo(() => {
    const declared = nodes.flatMap((n) =>
      n.data.extraction?.enabled ? n.data.extraction.variables.map((v) => v.name) : [],
    );
    return [...new Set([...CALL_CONTEXT_VARIABLES, ...declared.filter(Boolean)])];
  }, [nodes]);

  const graph = useMemo(() => toGraph(nodes, edges), [nodes, edges]);
  const serialized = JSON.stringify(graph);
  // Compared against the live graph with keys sorted, because that copy has
  // round-tripped through a Postgres JSONB column, which reorders object
  // keys — a raw string compare never matches and the badge would read
  // "draft" forever, including on a freshly published graph nobody touched.
  const canonical = canonicalize(graph);

  // ── Undo/redo ─────────────────────────────────────────────────────────
  // Autosave means there is no Cancel, so there has to be a way back.
  const past = useRef<string[]>([]);
  const future = useRef<string[]>([]);
  const lastGraph = useRef<string>("");
  const lastPushAt = useRef(0);
  const timeTravelling = useRef(false);
  // Mirrored into state, not read off the refs at render time: a ref
  // changing doesn't re-render, so the buttons would sit at their initial
  // enabled/disabled state forever.
  const [depth, setDepth] = useState({ undo: 0, redo: 0 });
  const syncDepth = useCallback(
    () => setDepth({ undo: past.current.length, redo: future.current.length }),
    [],
  );

  useEffect(() => {
    if (!loaded.current) return;
    if (timeTravelling.current) {
      timeTravelling.current = false;
      lastGraph.current = serialized;
      return;
    }
    if (lastGraph.current && lastGraph.current !== serialized) {
      const now = Date.now();
      // Collapse a drag (dozens of intermediate positions) into one step.
      if (now - lastPushAt.current > UNDO_COALESCE_MS) {
        past.current.push(lastGraph.current);
        if (past.current.length > UNDO_LIMIT) past.current.shift();
        lastPushAt.current = now;
      }
      future.current = [];
      syncDepth();
    }
    lastGraph.current = serialized;
  }, [serialized, syncDepth]);

  const restore = useCallback((snapshot: string) => {
    const rf = toReactFlow(JSON.parse(snapshot) as WorkflowGraph);
    timeTravelling.current = true;
    setNodes(rf.nodes);
    setEdges(rf.edges);
    setSelection(null);
    syncDepth();
  }, [setNodes, setEdges, syncDepth]);

  const undo = useCallback(() => {
    const previous = past.current.pop();
    if (previous === undefined) return;
    future.current.push(lastGraph.current);
    restore(previous);
  }, [restore]);

  const redo = useCallback(() => {
    const next = future.current.pop();
    if (next === undefined) return;
    past.current.push(lastGraph.current);
    restore(next);
  }, [restore]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Never steal the browser's own undo while someone is typing a prompt.
      const el = e.target as HTMLElement | null;
      if (el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "z") {
        e.preventDefault();
        if (e.shiftKey) redo(); else undo();
      }
      if (e.key === "Escape") setAddOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [undo, redo]);

  // ── Autosave ──────────────────────────────────────────────────────────
  // No save button, no lost work. Drafts are never validated and never read
  // by a call, so this can fire on a half-drawn graph without consequence.
  useEffect(() => {
    if (!loaded.current) return;
    setSaveState("saving");
    const timer = setTimeout(() => {
      saveWorkflowDraft(tenantSlug, agentId, JSON.parse(serialized))
        .then(() => setSaveState("saved"))
        .catch((e) => {
          setSaveState("idle");
          setError(e instanceof ApiError ? e.detail : String(e));
        });
    }, AUTOSAVE_MS);
    return () => clearTimeout(timer);
  }, [serialized, tenantSlug, agentId]);

  // ── Live validation ───────────────────────────────────────────────────
  // The same check that gates a publish, run as you draw — so "this
  // connection has no condition" surfaces while you're looking at it,
  // instead of as a wall of red after you press Publish.
  useEffect(() => {
    if (!loaded.current) return;
    let current = true;
    const timer = setTimeout(() => {
      validateWorkflow(tenantSlug, agentId, JSON.parse(serialized))
        .then((result) => {
          if (!current) return;   // a newer edit already superseded this
          setErrors(result.errors ?? []);
          setWarnings(result.warnings ?? []);
        })
        .catch(() => {/* the publish attempt reports for real; don't nag */});
    }, VALIDATE_MS);
    return () => { current = false; clearTimeout(timer); };
  }, [serialized, tenantSlug, agentId]);

  // ── Canvas painting ───────────────────────────────────────────────────
  const badNodeIds = useMemo(
    () => new Set(errors.filter((e) => e.kind === "node").map((e) => e.id)),
    [errors],
  );
  const badEdgeIds = useMemo(
    () => new Set(errors.filter((e) => e.kind === "edge").map((e) => e.id)),
    [errors],
  );

  const paintedNodes = useMemo(() => nodes.map((n) => {
    const invalid = badNodeIds.has(n.id);
    const active = n.id === activeNodeId;
    if (Boolean(n.data.__invalid) === invalid && Boolean(n.data.__active) === active) return n;
    return { ...n, data: { ...n.data, __invalid: invalid, __active: active } };
  }), [nodes, badNodeIds, activeNodeId]);

  // ConditionEdge draws the label itself, and reads __invalid off data.
  // toGraph strips the marker on the way out (see there) — it can't just be
  // assumed not to reach it, because onEdgeClick selects off this derived
  // copy and editing writes the selection back into the real edge.
  const paintedEdges = useMemo(() => edges.map((e) => {
    const invalid = badEdgeIds.has(e.id);
    return {
      ...e,
      type: "condition",
      label: undefined,
      data: { ...(e.data as WorkflowEdgeData), __invalid: invalid },
      className: invalid ? "wf-edge-invalid" : !e.data?.condition?.trim() ? "wf-edge-unfinished" : undefined,
      animated: invalid,
    };
  }), [edges, badEdgeIds]);

  // ── Editing ───────────────────────────────────────────────────────────
  const onConnect = useCallback((connection: Connection) => {
    setEdges((eds) =>
      addEdge(
        {
          ...connection,
          id: `e-${connection.source}-${connection.target}-${Date.now()}`,
          label: "",
          data: { label: "", condition: "" },
        } as RFEdge,
        eds,
      ),
    );
  }, [setEdges]);

  const selectNode = useCallback((node: RFNode) => {
    setSelection({
      kind: "node", id: node.id,
      nodeType: node.type as WorkflowNodeType,
      data: node.data as WorkflowNodeData,
    });
  }, []);

  const updateNode = (id: string, data: WorkflowNodeData) => {
    setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...data } } : n)));
    setSelection((s) => (s && s.id === id ? { ...s, data } : s));
  };

  const changeNodeType = (id: string, type: WorkflowNodeType) => {
    setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, type } : n)));
    setSelection((s) => (s && s.id === id ? { ...s, nodeType: type } : s));
  };

  const updateEdge = (id: string, data: WorkflowEdgeData) => {
    setEdges((es) => es.map((e) => (e.id === id ? { ...e, label: data.label, data } : e)));
    setSelection((s) => (s && s.id === id ? { ...s, data } : s));
  };

  /** Re-layout top-to-bottom and re-centre. Autosave picks the new
   *  positions up like any other edit, so there is nothing to save here. */
  const tidyUp = () => {
    setNodes((ns) => autoLayout(ns, edges));
    window.setTimeout(() => reactFlow.fitView({ duration: 400, padding: 0.2 }), 0);
  };

  const remove = (sel: Selection) => {
    if (sel.kind === "node") {
      setNodes((ns) => ns.filter((n) => n.id !== sel.id));
      setEdges((es) => es.filter((e) => e.source !== sel.id && e.target !== sel.id));
    } else {
      setEdges((es) => es.filter((e) => e.id !== sel.id));
    }
    setSelection(null);
  };

  const addNode = (type: Exclude<WorkflowNodeType, "start">, from?: string) => {
    const id = `${type}-${Date.now()}`;
    const origin = from ? nodes.find((n) => n.id === from) : undefined;
    // Branching is the normal case — a step routes to booking OR to Q&A —
    // so the second child of a step has to land beside the first, not on
    // top of it. Offset by however many branches already leave `from`.
    const siblings = from ? edges.filter((e) => e.source === from).length : 0;
    const position = origin
      ? { x: origin.position.x + siblings * 360, y: origin.position.y + 230 }
      : { x: 320, y: 60 + nodes.length * 40 };
    const node = { id, type, position, data: { ...NEW_NODE_DEFAULTS[type] }, deletable: true } as RFNode;

    setNodes((ns) => [...ns, node]);
    if (from) {
      setEdges((es) => [...es, {
        id: `e-${from}-${id}`, source: from, target: id,
        label: "", data: { label: "", condition: "" },
      } as RFEdge]);
    }
    setAddOpen(false);
    // Land the operator straight in the form for what they just made,
    // rather than making them find and click it.
    selectNode(node);
  };

  const addConnectedStage = useCallback((fromNodeId: string) => {
    addNode("agent", fromNodeId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes]);

  /** Jump to whatever a problem is talking about. A message naming a node
   *  you then have to go hunting for is barely better than no message. */
  const revealProblem = (problem: WorkflowError) => {
    if (!problem.id) return;
    const node = nodes.find((n) => n.id === problem.id);
    if (node) {
      selectNode(node);
      reactFlow.fitView({ nodes: [{ id: node.id }], duration: 400, maxZoom: 1.3 });
      return;
    }
    const edge = edges.find((e) => e.id === problem.id);
    if (edge) {
      setSelection({
        kind: "edge", id: edge.id,
        data: (edge.data || { label: "", condition: "" }) as WorkflowEdgeData,
      });
      reactFlow.fitView({ nodes: [{ id: edge.source }, { id: edge.target }], duration: 400, maxZoom: 1.3 });
    }
  };

  // ── Publish ───────────────────────────────────────────────────────────
  const publish = async () => {
    setPublishing(true);
    setError(null);
    try {
      const result = await publishWorkflow(tenantSlug, agentId, JSON.parse(serialized));
      setWarnings(result.warnings);
      setErrors([]);
      setPublished(canonical);
      setVersionKey((k) => k + 1);
      setJustPublished(true);
      setTimeout(() => setJustPublished(false), 4000);
    } catch (e) {
      const { message, errors: found } = publishErrors(e);
      setError(message);
      setErrors(found);
    } finally {
      setPublishing(false);
    }
  };


  if (loading) return <div className="empty-state">Loading workflow…</div>;

  const diverged = published !== canonical;
  const blocking = errors.length > 0;
  const status = published === null ? "not live" : diverged ? "unpublished changes" : "live";

  return (
    <EditorContext.Provider value={{ addConnectedStage }}>
      <div className="wf-root">
        {showHelp && (
          <div className="wf-help">
            <div>
              <strong>A workflow splits the call into stages.</strong> Each stage has its own
              instructions and its own tools, and the agent moves between them when a
              connection&apos;s condition is met. Callers only reach a stage once the agent has
              earned its way there — so it can&apos;t book before it has verified.
            </div>
            <button className="btn btn-ghost btn-sm" onClick={() => setShowHelp(false)}>Got it</button>
          </div>
        )}

        <div className="wf-toolbar">
          {header && (
            <>
              <Link href={header.backHref} className="wf-back-btn" title="Back to Workflows">←</Link>
              <span className="wf-page-title">{header.title}</span>
              <span className="wf-toolbar-sep" />
            </>
          )}
          <button
            className="btn btn-ghost btn-sm"
            title="Undo (Ctrl+Z)"
            disabled={depth.undo === 0}
            onClick={undo}
          >
            ↶
          </button>
          <button
            className="btn btn-ghost btn-sm"
            title="Redo (Ctrl+Shift+Z)"
            disabled={depth.redo === 0}
            onClick={redo}
          >
            ↷
          </button>
          <button
            className={`btn btn-sm ${testing ? "btn-primary" : "btn-ghost"}`}
            title="Try this flow before publishing it — talk to it or type at it. The active stage lights up on the canvas as the conversation moves."
            onClick={() => { setTesting(testing ? null : "call"); setActiveNodeId(null); }}
          >
            Test Agent
          </button>

          <span className={`badge ${published === null ? "gray" : diverged ? "amber" : "green"}`}>
            {status}
          </span>
          <span className="wf-save-state">
            {saveState === "saving" ? "Saving…" : saveState === "saved" ? "Draft saved" : ""}
          </span>

          <div className="wf-toolbar-right">
            <button
              className="btn btn-primary btn-sm"
              disabled={publishing || blocking || (!diverged && !justPublished)}
              title={
                blocking ? "Fix the problems listed below first"
                  : !diverged ? "Nothing has changed since the last publish"
                  : "Make this the flow live calls run"
              }
              onClick={publish}
            >
              {publishing ? "Publishing…"
                : justPublished ? "Published ✓"
                : blocking ? `${errors.length} to fix`
                : "Publish"}
            </button>

            {/* Everything you reach for once a session, not once a minute. */}
            <div className="wf-menu-wrap">
              <button
                className="btn btn-ghost btn-sm"
                aria-expanded={moreOpen}
                aria-haspopup="menu"
                title="More"
                onClick={() => setMoreOpen((o) => !o)}
              >
                ⋮
              </button>
              {moreOpen && (
                <>
                  <div className="wf-menu-scrim" onClick={() => setMoreOpen(false)} />
                  <div className="wf-menu wf-menu-right" role="menu">
                    {header && (
                      <Link role="menuitem" href={header.settingsHref} onClick={() => setMoreOpen(false)}>
                        <span>Settings</span>
                        <small>Voice, model, tools and number</small>
                      </Link>
                    )}
                    <button
                      role="menuitem"
                      onClick={() => { setMoreOpen(false); setShowHistory((v) => !v); }}
                    >
                      <span>{showHistory ? "Hide" : "Show"} publish history</span>
                      <small>Earlier versions, and roll back</small>
                    </button>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>

        {error && <div className="error-banner">{error}</div>}

        <div className="wf-layout">
          <div className="wf-canvas">
            <ReactFlow
              nodes={paintedNodes}
              edges={paintedEdges}
              nodeTypes={nodeTypes}
              edgeTypes={edgeTypes}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onConnect={onConnect}
              onNodeClick={(_, node) => selectNode(node as RFNode)}
              onEdgeClick={(_, edge) =>
                setSelection({
                  kind: "edge", id: edge.id,
                  data: (edge.data || { label: "", condition: "" }) as WorkflowEdgeData,
                })
              }
              onPaneClick={() => setSelection(null)}
              onNodesDelete={() => setSelection(null)}
              onEdgesDelete={() => setSelection(null)}
              deleteKeyCode={["Delete", "Backspace"]}
              fitView
              minZoom={0.2}
              proOptions={{ hideAttribution: false }}
            >
              <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="#64748b" />
              <Controls showInteractive={false}>
                {/* Drag a few nodes around and the graph stops reading as a
                    flow. This re-lays it out top-to-bottom and re-centres —
                    the one canvas action that is pure clean-up, so it lives
                    with the other view controls rather than in a menu. */}
                <ControlButton onClick={tidyUp} title="Tidy up — arrange the stages and centre them">
                  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6">
                    <rect x="5.5" y="1.5" width="5" height="3.5" rx="1" />
                    <rect x="1" y="11" width="5" height="3.5" rx="1" />
                    <rect x="10" y="11" width="5" height="3.5" rx="1" />
                    <path d="M8 5v2.5M3.5 11V7.5h9V11" />
                  </svg>
                </ControlButton>
              </Controls>

              {/* Adding a node belongs on the canvas you're adding it to,
                  not in a page toolbar — same placement as Dograh's. */}
              <FlowPanel position="top-right" className="wf-canvas-panel">
                <div className="wf-menu-wrap">
                  <button
                    className="wf-canvas-btn"
                    aria-expanded={addOpen}
                    aria-haspopup="menu"
                    title="Add a node"
                    onClick={() => setAddOpen((o) => !o)}
                  >
                    +
                  </button>
                  {addOpen && (
                    <>
                      {/* Click-anywhere-else closes it, the way every other menu does. */}
                      <div className="wf-menu-scrim" onClick={() => setAddOpen(false)} />
                      <div className="wf-menu wf-menu-right" role="menu">
                        <button role="menuitem" onClick={() => addNode("agent")}>
                          <span>Stage</span><small>Another step in the conversation</small>
                        </button>
                        <button role="menuitem" onClick={() => addNode("transfer")}>
                          <span>Transfer to a human</span><small>Hands the call over</small>
                        </button>
                        <button role="menuitem" onClick={() => addNode("end")}>
                          <span>End the call</span><small>Hangs up, with an outcome</small>
                        </button>
                        <button
                          role="menuitem"
                          disabled={hasGlobal}
                          title={hasGlobal ? "This flow already has one" : undefined}
                          onClick={() => addNode("global")}
                        >
                          <span>Always applies</span>
                          <small>{hasGlobal ? "Already added" : "Instructions for every step"}</small>
                        </button>
                      </div>
                    </>
                  )}
                </div>
              </FlowPanel>
            </ReactFlow>
          </div>
          {/* The column only exists when it has something to say: a
              selected node to edit, or a test session. With neither, the
              canvas gets the whole page — which is what you want open in
              front of you while you are drawing a flow. */}
          {(testing || selection) && (
          <div className="wf-side">
            {testing && (
              <div className="wf-test-tabs" role="tablist">
                <button
                  role="tab"
                  aria-selected={testing === "call"}
                  className={testing === "call" ? "active" : ""}
                  onClick={() => { setTesting("call"); setActiveNodeId(null); }}
                >
                  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                    <path d="M8 1.5a2.2 2.2 0 012.2 2.2v4a2.2 2.2 0 01-4.4 0v-4A2.2 2.2 0 018 1.5z" />
                    <path d="M3.8 7.5V8a4.2 4.2 0 008.4 0v-.5M8 12.2v2.3" />
                  </svg>
                  Test Audio
                </button>
                <button
                  role="tab"
                  aria-selected={testing === "chat"}
                  className={testing === "chat" ? "active" : ""}
                  onClick={() => { setTesting("chat"); setActiveNodeId(null); }}
                >
                  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                    <path d="M2 3.5h12v7H6.5L3.5 13v-2.5H2z" />
                  </svg>
                  Test Chat
                </button>
                <button
                  className="wf-test-close"
                  title="Close the test panel"
                  aria-label="Close the test panel"
                  onClick={() => { setTesting(null); setActiveNodeId(null); }}
                >
                  ✕
                </button>
              </div>
            )}
            {testing === "call" ? (
              <TestAgentPanel
                open
                onClose={() => { setTesting(null); setActiveNodeId(null); }}
                tenantSlug={tenantSlug}
                agentSlug={agentSlug}
                useDraft
                inline
                onNodeChanged={(node) => setActiveNodeId(node.id)}
              />
            ) : testing === "chat" ? (
              <TextChatPanel
                open
                onClose={() => { setTesting(null); setActiveNodeId(null); }}
                tenantSlug={tenantSlug}
                agentSlug={agentSlug}
                useDraft
                onNodeChanged={(node) => setActiveNodeId(node.id)}
              />
            ) : (
              <Inspector
                selection={selection}
                agentTools={agentTools}
                knowledgeBases={knowledgeBases}
                errors={errors}
                warnings={warnings}
                availableVariables={availableVariables}
                onChangeNode={updateNode}
                onChangeNodeType={changeNodeType}
                onChangeEdge={updateEdge}
                onDelete={remove}
              />
            )}
          </div>
          )}
        </div>

        <ProblemList errors={errors} warnings={warnings} onReveal={revealProblem} />

        <div className="wf-history">
          {showHistory && (
            <VersionPanel
              tenantSlug={tenantSlug}
              agentId={agentId}
              refreshKey={versionKey}
              onRolledBack={() => {
                setVersionKey((k) => k + 1);
                getWorkflow(tenantSlug, agentId).then((state) => {
                  if (!state.workflow) return;
                  const rf = toReactFlow(state.workflow);
                  setNodes(rf.nodes);
                  setEdges(rf.edges);
                  setPublished(canonicalize(state.workflow));
                });
              }}
            />
          )}
        </div>

      </div>
    </EditorContext.Provider>
  );
}

function ProblemList({
  errors, warnings, onReveal,
}: { errors: WorkflowError[]; warnings: WorkflowError[]; onReveal: (p: WorkflowError) => void }) {
  if (!errors.length && !warnings.length) {
    return <div className="wf-problems wf-problems-ok">No problems — this flow is ready to publish.</div>;
  }
  const row = (p: WorkflowError, kind: "error" | "warning", i: number) => (
    <button
      key={`${kind}-${i}`}
      className={`wf-problem wf-problem-${kind}`}
      onClick={() => onReveal(p)}
      disabled={!p.id}
      title={p.id ? "Show me" : undefined}
    >
      <span className="wf-problem-dot" aria-hidden />
      <span>{p.message}</span>
    </button>
  );
  return (
    <div className="wf-problems">
      {errors.length > 0 && (
        <div className="wf-problems-hdr">
          {errors.length} thing{errors.length === 1 ? "" : "s"} to fix before this can go live
        </div>
      )}
      {errors.map((e, i) => row(e, "error", i))}
      {warnings.length > 0 && (
        <div className="wf-problems-hdr wf-problems-hdr-warn">
          {warnings.length} thing{warnings.length === 1 ? "" : "s"} worth a look — these won&apos;t block publishing
        </div>
      )}
      {warnings.map((w, i) => row(w, "warning", i))}
    </div>
  );
}

export function WorkflowPanel(props: {
  tenantSlug: string; agentId: string; agentSlug: string; header?: WorkflowHeader;
}) {
  // ReactFlowProvider is required for the hooks the canvas uses internally.
  return (
    <ReactFlowProvider>
      <Panel {...props} />
    </ReactFlowProvider>
  );
}
