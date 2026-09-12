# Workflow — full design

Status: built. Everything below is implemented except the two items
called out in "Not built" at the end of Part 8.
Reference studied: `/home/mors/Code/dograh` — `api/services/workflow/` (7,644 lines)
and `ui/src/components/flow` + `ui/src/app/workflow` (18,191 lines).

This document describes the **whole feature**, not a first cut. Where we
diverge from Dograh it says why — the goal is a workflow engine that fits
yuviz's architecture, not a port of theirs into a codebase that works
differently.

---

## Part 1 — What a workflow is

### 1.1 The problem with one prompt

Today a yuviz agent is a single string. `agents.system_prompt` lands in
`history[0]` at `services/conversation/pipeline.py:639` and never changes for
the life of the call. Every tool the agent can reach is resolved once, per
agent, by `ToolPolicyResolver.enabled_tools(agent_id)`.

For "answer questions about the clinic" that's correct and a graph would be
pure overhead. It breaks down the moment a call has *stages*:

> Verify the caller is the account holder → understand what they want →
> check eligibility → book or transfer → confirm and close.

Cram that into one prompt and you get the three failure modes everyone who
has shipped a voice agent has seen:

- **Stage skipping.** The model books before verifying, because nothing
  structurally prevents it — the booking tool was available on turn one.
- **Stage forgetting.** Twelve turns in, the eligibility instruction is 4,000
  tokens back and competing with the transcript for attention.
- **Unfalsifiable failure.** The call went badly. Which instruction lost?
  There is one prompt and one transcript; nothing to bisect.

Prompt engineering does not fix these. They are consequences of putting a
state machine's worth of behavior into a stateless string.

### 1.2 The model

A workflow makes the states explicit:

- A **node** is one stage. It owns a prompt, and it owns which tools are
  reachable while it is active.
- An **edge** is a permitted move, carrying a label ("caller verified") and a
  condition in plain English ("The caller has confirmed both their date of
  birth and postcode").

Exactly one node is active at a time. Its prompt *is* the system prompt. Its
outgoing edges are the only moves available. The booking tool doesn't exist
until the booking node is active — not "the model is told not to use it", it
is genuinely not in the schema list sent to the provider.

```
                     ┌───────────┐
                     │   start   │  greeting + who are you
                     └─────┬─────┘
                           │ caller identified
                           ▼
      ┌──────────────┬───────────┬──────────────┐
      │              │  triage   │              │
      │              └─────┬─────┘              │
      │ wants to book      │ just a question    │ wants a human
      ▼                    ▼                    ▼
┌───────────┐        ┌───────────┐        ┌───────────┐
│  booking  │        │    q&a    │        │ transfer  │
│ +calendar │        │  +kb      │        │           │
└─────┬─────┘        └─────┬─────┘        └───────────┘
      │ booked             │ answered
      │                    │ anything else? ──┐
      ▼                    ▼                  │
┌───────────────────────────────┐             │
│             end               │◄────────────┘
└───────────────────────────────┘
```

### 1.3 The one mechanism

**Each outgoing edge is registered with the LLM as a callable function.**

That is the entire trick, and it is what Dograh does at
`pipecat_engine.py:571` (`_setup_llm_context`) — on entering a node it sets
the system prompt to that node's prompt and builds one function schema per
outgoing edge, taking the **name from the edge label** and the **description
from the edge condition**.

So the model sees:

```
system: You are booking an appointment. Get a date and time the caller wants,
        then call book_appointment.

tools:  book_appointment(requested_datetime, ...)
        booking_confirmed()   — "The appointment has been successfully booked."
        caller_changed_mind() — "The caller no longer wants to book."
```

and moves the conversation forward by calling `booking_confirmed()`.

Why this beats every alternative:

- **No second model.** A separate "should we transition?" classifier doubles
  latency on every turn and sees less context than the model already holds.
- **No transcript regex.** Brittle, and unfixable when the model paraphrases.
- **Decision and action are one event.** The model decides to move *and* the
  move happens, atomically, in the same turn. There is no window where the
  model believes it has advanced but the engine hasn't.
- **Observability is free.** Every transition is a named function call in the
  logs. "Where do calls die?" becomes a `GROUP BY node_id`.

The cost is one extra generation round-trip per transition — the model calls
the function, gets `{"status":"done"}`, then generates its actual reply under
the new node's prompt. Dograh accepts this and so should we; it is the same
round-trip the tool loop already pays for `book_appointment`.

---

## Part 2 — What Dograh built, and what we take from it

| Concern | Dograh file | Lines | Our decision |
|---|---|---|---|
| Graph parse + validate | `workflow_graph.py` | 444 | **Take**, adapted to dataclasses in the SDK |
| Runtime node walk | `pipecat_engine.py` | 1,159 | **Take the ~250 lines that matter**, rest is Pipecat plumbing we don't have |
| Prompt/tool composition | `pipecat_engine_context_composer.py` | 132 | **Take**, simplified |
| Variable extraction | `pipecat_engine_variable_extractor.py` | 251 | **Take**, on our own LLM interface |
| Context summarization | `pipecat_engine_context_summarizer.py` | 173 | **Take the idea**, own implementation (~60 lines) |
| Node property spec registry | `dto.py` + `node_specs/` | ~1,500 | **Skip** — see §2.2 |
| Per-node custom/MCP tools | `pipecat_engine_custom_tools.py` | 1,009 | **Adapt** — we have `ToolPolicyResolver`, we filter it per node (~40 lines) |
| Text-chat runner | `text_chat_runner.py` | 781 | **Skip** — no text product; our runner is voice-agnostic if one appears |
| Embed chat, QA scoring | several | ~1,200 | **Skip** |
| Trigger / webhook nodes | `trigger_paths.py` + DTOs | ~400 | **Skip** — no mid-call HTTP triggers |
| Pre-call HTTP fetch | in start node | ~150 | **Skip** — campaign contact data covers it |
| Disposition catalog | `disposition_codes.py` | 44 | **Take** — see §5.6 |
| Frontend editor | `ui/src/components/flow` etc. | 18,191 | **Take the shape, ~1/6 the size** — see Part 6 |

### 2.1 The architectural gap that changes everything

Dograh's engine is built **on Pipecat**. That is not a detail — it dictates
their whole design, and it is the reason a copy-paste port would be wrong.

| | Dograh (Pipecat) | yuviz |
|---|---|---|
| Where tools live | `llm.register_function(name, fn)` mutates the **LLM object** | schemas passed **per call** into `LLMAdapter.generate(messages, schemas)` |
| Where the prompt lives | `llm._update_settings(system_instruction=...)` — provider settings | `history[0]`, a plain message |
| Continuing after a tool call | `FunctionCallResultProperties(on_context_updated=...)` callback fired by the framework | `ToolCallOrchestrator.run_turn()` just loops — `orchestrator.py` |
| Interruption | Pipecat interruption frames | `cancel_event` already threaded everywhere |

The consequence, and it is in our favour: **Dograh's transitions are stateful
mutations of a long-lived LLM object; ours are pure arguments to one function
call.**

Their `register_function` accumulates. Registering `booking_confirmed` on the
booking node leaves it registered when you move to the Q&A node unless
something clears it. Their `compose_functions_for_node` rebuilds the *schema*
list per node, so the model isn't *offered* the stale function — but the
handler is still installed, and a model that hallucinates the name still hits
it. That's a whole class of bug we structurally cannot have, because
`run_turn` receives the current node's tool list as an argument on every
single turn. Nothing persists between turns except the node pointer.

This is why the port is an adaptation, not a translation. Roughly 700 of
`pipecat_engine.py`'s 1,159 lines exist to manage Pipecat state — frame
queueing, playback arming, mute filters, context aggregator races. We need
none of it. Our equivalent is smaller *because our runtime is simpler*, not
because we're cutting features.

### 2.2 Why we skip the node-spec registry

Dograh's `dto.py` is 43 KB, and most of that is `spec_field(...)` metadata:
`ui_type`, `display_name`, `description`, `display_options`, `llm_hint`. It
feeds `node_specs/` which serializes a **NodeSpec contract** to the frontend,
which renders property forms generically via `renderer/PropertyInput.tsx` (490
lines) and a `displayOptions.ts` visibility evaluator mirrored 1:1 in Python
with golden fixtures locking the two implementations together.

That machinery buys one thing: **third parties can register node types without
touching the editor.** Dograh has an integrations directory and an SDK; for
them it's correct.

We have four node types and no third-party integrations. Building a
spec-driven renderer to avoid writing four forms is 1,500 lines of framework
to save 300 lines of form. It also creates the exact synchronization problem
their code comments warn about (`evaluate_display_options` is "Mirrored 1:1 in
the TypeScript renderer... update both whenever the semantics change").

We hardcode four node types in both languages. When a fifth arrives, we write
a fifth form. If a third party ever needs to register one, we build the
registry then, with a real requirement to shape it.

### 2.3 Where we go further than Dograh

Three places where yuviz's existing architecture lets us do better:

1. **Transfer is a node type, not a tool.** Dograh's human handoff is a
   regular custom tool. yuviz has a first-class transfer subsystem —
   `transfer_engine.py`, `agents.transfer_type`/`caller_id_policy`/
   `transfer_waiting_experience`, and a `Transferring` state in the C++
   `CallFSM`. Modelling handoff as a tool would bypass all of it. A
   **transfer node** is the honest representation: reaching it hands the call
   to the transfer engine with that node's configured destination.

2. **Draft/publish, not save-and-pray.** Dograh saves the graph and validates
   it. Since a broken graph means broken *live calls*, we split
   `workflow_draft` (what the editor writes) from `workflow` (what calls
   read). See §4.2.

3. **Dry-run the graph without audio.** Because our runner has no voice
   dependency at all (it returns a prompt and a tool list — that's its entire
   surface), a scripted list of caller turns can be walked through the graph
   in a test in milliseconds. Dograh cannot do this cheaply; their engine is
   welded to a Pipecat pipeline. See §7.2.

---

## Part 3 — Where it fits in yuviz

Four facts from the current code that the design leans on:

1. **The pipeline is already per-call.** `handler_factory`
   (`services/conversation/__main__.py:233`) builds a fresh
   `PipelineConversationHandler` per gRPC stream (`servicer.py:137`). The
   current-node pointer is therefore just an instance attribute — no session
   map, no cross-call leakage, no cleanup path.

2. **`history[0]` is already the system message and trimming protects it.**
   `_trim_history` (`pipeline.py:1164`) explicitly preserves `history[0]`.
   Swapping the node prompt is a one-line mutation, not a context rework.

3. **The tool loop already does what a transition needs.** `run_turn`
   (`orchestrator.py`) loops: generate → detect tool call → execute → fold
   result into history → generate again. A transition is that loop with a
   different executor. We are not writing an engine; we are adding a kind of
   tool.

4. **Config already flows agent → Redis → conversation.** `RuntimeConfig` is
   resolved once per call by `agent_resolver.py` and is immutable for the
   session. The graph rides that same path and gets caching and invalidation
   for free.

### 3.1 Seam-by-seam change list

| Seam | Today | Change | Size |
|---|---|---|---|
| `database/schema.sql:70` | `agents.system_prompt TEXT` | + `workflow`, `workflow_draft` JSONB; + `agent_workflow_versions` table | ~20 lines SQL |
| `libs/config_sdk/models.py` | `Agent` dataclass | + `workflow: dict \| None` | 2 lines |
| `libs/config_sdk/workflow.py` | — | **new**: graph model + validation | ~220 |
| `services/config/routers/agents.py:56` | PATCH agent | + draft save, publish, versions, rollback | ~120 |
| `services/conversation/workflow/runner.py` | — | **new**: the runtime | ~300 |
| `services/conversation/workflow/extractor.py` | — | **new**: variable extraction | ~120 |
| `tools/orchestrator.py` | policy tools only | + `local_tools` param | ~20 |
| `tools/policy_resolver.py` | agent-wide tool set | + optional per-node filter | ~15 |
| `pipeline.py` | fixed prompt | node-aware prompt, tools, speech, end, transfer | ~90 |
| `transcript_builder.py` | turn rows | + `node_id`, `node_name` | ~15 |
| `admin-ui` | — | canvas + inspector + versions | ~2,800 TS |

Backend total: roughly **900 new lines and ~250 changed**. Against Dograh's
7,644 — and the gap is Pipecat plumbing, the spec registry, and the text-chat
duplicate, not capability.

`workflow IS NULL` means today's exact behavior. Single-prompt agents are not
deprecated and not degraded; the entire workflow path is skipped.

---

## Part 4 — Data model

### 4.1 Graph JSON

React Flow's own save format, stored verbatim so canvas positions round-trip.

```json
{
  "version": 1,
  "nodes": [
    { "id": "n1", "type": "start", "position": {"x": 0, "y": 0},
      "data": {
        "name": "greeting",
        "prompt": "Greet the caller warmly and ask how you can help.",
        "greeting": "Hi, thanks for calling {{ business_name }}.",
        "delayed_start_ms": 0
      } },

    { "id": "n2", "type": "agent", "position": {"x": 320, "y": 0},
      "data": {
        "name": "booking",
        "prompt": "Book an appointment. Get a date and time, then book it.",
        "tools": ["book_appointment"],
        "knowledge_base_ids": [],
        "extraction": {
          "enabled": true,
          "prompt": "Capture what the caller agreed to.",
          "variables": [
            {"name": "appointment_reason", "type": "string",
             "prompt": "Why the caller wants the appointment."}
          ]
        }
      } },

    { "id": "n3", "type": "transfer", "position": {"x": 320, "y": 200},
      "data": { "name": "to_human", "prompt": "Tell the caller you're connecting them.",
                "transfer_destination": null } },

    { "id": "n4", "type": "end", "position": {"x": 640, "y": 0},
      "data": { "name": "goodbye", "prompt": "Confirm the details and close warmly.",
                "disposition": "qualified" } }
  ],
  "edges": [
    { "id": "e1", "source": "n1", "target": "n2",
      "data": { "label": "caller wants to book",
                "condition": "The caller has asked to make an appointment.",
                "transition_speech": "Of course, let me pull up the calendar." } }
  ]
}
```

**Node types — the complete set.**

| Type | Cardinality | Owns |
|---|---|---|
| `start` | exactly 1 | greeting, prompt, optional delayed start |
| `agent` | 0..n | prompt, tools, knowledge bases, extraction config |
| `transfer` | 0..n | prompt (what to say before handing off), destination override |
| `end` | 1..n | closing prompt, disposition code |

No `global` node. Dograh has one because they have nowhere else to put an
always-on instruction. We already have `agents.system_prompt` — in workflow
mode it becomes the global prompt, prepended to every node's prompt. That is a
field the UI already exposes on the Behaviour tab and an operator already
understands. A floating node on the canvas that isn't part of the graph is
strictly worse UX for the same thing.

**Edge data** is Dograh's `EdgeDataDTO` (`dto.py:1069`) minus the audio
recording fields:

```
label              — becomes the tool name: lower(), non-alphanumerics → "_"
condition          — becomes the tool description. This is the prompt that
                     decides the transition. It matters more than the node
                     prompt and the UI should say so.
transition_speech  — optional. Spoken immediately on transition, before the
                     new node generates. Covers the round-trip with something
                     natural instead of dead air.
```

### 4.2 Storage — draft, published, versioned

```sql
ALTER TABLE agents ADD COLUMN IF NOT EXISTS workflow       JSONB;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS workflow_draft JSONB;

CREATE TABLE IF NOT EXISTS agent_workflow_versions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id     UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    version      INT  NOT NULL,
    graph        JSONB NOT NULL,
    published_by UUID REFERENCES users(id),
    published_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    note         TEXT,
    UNIQUE (agent_id, version)
);
CREATE INDEX IF NOT EXISTS idx_awv_agent ON agent_workflow_versions(agent_id, version DESC);
```

Three states, and the split is the point:

- **`workflow_draft`** — the editor autosaves here. May be invalid, may be
  half-drawn. Never read by a call.
- **`workflow`** — what live calls execute. Only ever written by publish, and
  publish validates first. **A broken graph cannot reach a phone call.**
- **`agent_workflow_versions`** — every publish appends. Rollback is
  `UPDATE agents SET workflow = (SELECT graph FROM ... WHERE version = $2)`.

Why a column for the live graph rather than always joining the versions table:
`agents.config_version` already bumps on agent writes and already drives Redis
invalidation for `RuntimeConfig` (`services/config/cache.py`). Publishing
bumps it and the graph propagates through machinery that already exists and is
already tested. A join would need its own invalidation path.

Why a versions table at all, when §2 argues against speculative structure:
this isn't speculative. An operator editing a live agent's conversation flow
with no way back is a support incident, not a hypothetical, and the table is
nine lines.

---

## Part 5 — Backend

### 5.1 `libs/config_sdk/workflow.py` — the shared graph model (~220 lines)

Both planes need the identical definition: the config service validates on
publish, the conversation service walks at runtime. `libs/config_sdk` is
already exactly that boundary. It is dataclass-only today and stays that way —
no pydantic dependency added to the SDK.

```python
@dataclass(frozen=True)
class Edge:
    id: str
    source: str
    target: str
    label: str
    condition: str
    transition_speech: str | None = None

    @property
    def tool_name(self) -> str:
        return re.sub(r"[^a-z0-9]+", "_", self.label.lower()).strip("_")

@dataclass
class Node:
    id: str
    type: str                      # start | agent | end | transfer
    name: str
    prompt: str
    greeting: str | None = None
    delayed_start_ms: int = 0
    tools: list[str] = field(default_factory=list)
    knowledge_base_ids: list[str] = field(default_factory=list)
    extraction: Extraction | None = None
    transfer_destination: str | None = None
    disposition: str | None = None
    out_edges: list[Edge] = field(default_factory=list)

@dataclass
class WorkflowGraph:
    nodes: dict[str, Node]
    start_node_id: str

    def reachable(self) -> set[str]: ...
    def template_variables(self) -> set[str]: ...   # every {{ var }} in the graph

def parse_graph(raw: dict) -> WorkflowGraph:
    """Raises WorkflowInvalid(errors) — never returns a partial graph."""
```

**Validation rules.** Every one of these is a runtime break, not a style
preference. Ported from `workflow_graph.py`'s `_assert_connection_counts` /
`validate_unique_transition_tool_names` / `validate_node_instance_constraints`:

| Rule | Why it exists |
|---|---|
| every edge endpoint resolves to a real node | dangling reference → `KeyError` mid-call |
| exactly one `start` node | ambiguous entry point |
| at least one `end` node | no way to hang up cleanly |
| `start` has no incoming edges | it is the entry, not a destination |
| `end` and `transfer` have no outgoing edges | terminal by definition |
| every non-terminal node has ≥1 outgoing edge | **dead end — the model is trapped with no way to advance and will loop until max call duration** |
| **no two outgoing edges of one node share a `tool_name`** | "yes" and "Yes!" both become `yes`; the second silently shadows the first. Dograh has a dedicated validator for this — it is the rule that actually fires in practice |
| every node reachable from `start` | unreachable node = editor mistake, warn not error |
| every `{{ var }}` is resolvable | see §5.7 |
| node names unique | they appear in logs and transcripts; duplicates make analytics lie |

**Cycles are allowed.** "Caller has another question" looping back to Q&A is
correct behavior, not a bug. Dograh commented out their own cycle check for
the same reason (`workflow_graph.py`, `_assert_acyclic`). Runaway loops are
bounded by `agents.max_call_duration_s`, which already exists.

Errors return as a list, never a single string:

```python
@dataclass(frozen=True)
class WorkflowError:
    kind: str          # "node" | "edge" | "workflow"
    id: str | None
    field: str | None
    message: str
```

so the editor can paint the specific node or edge red with the message
attached, instead of showing one toast the operator has to go hunting from.

### 5.2 `services/conversation/workflow/runner.py` — the runtime (~300 lines)

Owns the current node. Its entire public surface is "what prompt, what tools,
what happened" — no audio, no frames, no provider objects. That constraint is
what makes §7.2's dry-run testing possible, so it is worth defending.

```python
class WorkflowRunner:
    def __init__(self, graph, *, global_prompt, base_suffix, variables, extractor=None):
        self._graph = graph
        self._node = graph.nodes[graph.start_node_id]
        self._global = global_prompt          # agents.system_prompt
        self._suffix = base_suffix            # date context + end-call marker
        self._vars = dict(variables)          # caller_number, called_number, ...
        self._extractor = extractor
        self.visited: list[str] = [self._node.name]
        self.pending_speech: str | None = None
        self.pending_end: bool = False
        self.pending_transfer: Node | None = None

    # ── what the pipeline asks for each turn ────────────────────────────
    @property
    def node(self) -> Node: ...
    def system_prompt(self) -> str: ...
    def allowed_tool_names(self) -> list[str] | None: ...
    def local_tools(self) -> dict[str, tuple[ToolDefinition, Callable]]: ...
    def greeting(self) -> str | None: ...
    def render(self, text: str) -> str: ...
```

**`system_prompt()`** composes, mirroring Dograh's
`compose_system_prompt_for_node`:

```
render(global_prompt)      # agents.system_prompt — the always-on instruction
render(node.prompt)        # this stage
base_suffix                # current date + [[END_CALL]] marker instruction,
                           # which pipeline.py already builds today
```

joined by blank lines. Rendering happens at compose time, not at save time, so
variables extracted earlier in the call appear in later nodes' prompts.

**`local_tools()`** builds one `ToolDefinition` per outgoing edge —
`name=edge.tool_name`, `description=edge.condition`, empty parameter schema —
paired with a handler. The handler is where the state machine lives, and it is
short because the orchestrator does the rest:

```python
async def _transition(self, edge: Edge) -> ToolResult:
    # 1. Extract before leaving — this node's slice of the conversation is
    #    the extraction window, and after the swap it is context the next
    #    node's extraction would have to re-derive.
    if self._node.extraction and self._extractor:
        await self._extractor.extract(self._node, background=True)

    # 2. Queue the bridging line. Spoken by the pipeline before the next
    #    generation, so the model's round-trip isn't dead air.
    self.pending_speech = self.render(edge.transition_speech or "") or None

    # 3. Move.
    self._node = self._graph.nodes[edge.target]
    self.visited.append(self._node.name)

    # 4. Flag terminals for the pipeline to act on after the turn.
    if self._node.type == "end":
        self.pending_end = True
    elif self._node.type == "transfer":
        self.pending_transfer = self._node

    return ToolResult(status=ToolStatus.SUCCESS, payload={"status": "done"})
```

Compare Dograh's `_create_transition_func` (`pipecat_engine.py:248`, ~100
lines). Same four steps. The difference is entirely the ~60 lines they spend
on `FunctionCallResultProperties(on_context_updated=...)`, audio recording
fetch, and the `queue_frame` dance — Pipecat's callback protocol for
"regenerate now that the context changed". Our orchestrator loops on its own,
so step 4 just sets flags and returns.

Note the ordering: extraction fires **before** the node changes. Dograh does
the same and their comment explains why — after the swap, the segment you
wanted to extract from is already historical context.

### 5.3 `tools/orchestrator.py` — one new parameter (~20 lines)

```python
async def run_turn(self, ..., local_tools=None):
```

where `local_tools: dict[str, tuple[ToolDefinition, Callable[[dict], Awaitable[ToolResult]]]]`.

Two changes inside:

```python
# schemas: DB-backed policy tools + locally-executed tools
schemas = [p.definition.to_generic_schema() for p in policies]
schemas += [d.to_generic_schema() for d, _ in (local_tools or {}).values()]
```

```python
# _execute_tool_call: short-circuit before policy/provider resolution
entry = (local_tools or {}).get(event.tool_name)
if entry is not None:
    return await entry[1](event.arguments)
```

A local tool never touches `ToolPolicyResolver`, `ToolProviderManager`, or
`ExecutorRegistry` — it has no DB row, no credentials, no circuit breaker,
because it executes in-process and cannot fail in the ways those exist to
handle.

This stays honest to the module's stated contract ("nothing here mentions
calendar, Cal.com, or any specific tool"). `local_tools` is a generic
in-process tool; workflow transitions are its first user.

**Two traps, both real:**

1. **`DEFAULT_MAX_TOOL_ITERATIONS = 2`.** A turn that transitions *and* books
   burns both iterations, and the third generation runs with `schemas = None`
   — the model gets its tools yanked mid-flow and improvises. Local tools must
   not increment `iteration`. The cap exists to stop runaway *external* calls;
   an in-process pointer move is not what it's guarding.

2. **`run_turn` mutates `history` in place.** After a transition the system
   prompt is stale for the remainder of the same turn. The swap must happen
   inside the loop, not between turns — the pipeline passes a callback the
   orchestrator invokes after folding a local tool result, or (simpler) the
   transition handler mutates `history[0]` directly since it already has the
   composed prompt. Get this wrong and the second generation of a transitioning
   turn runs under the *previous* node's prompt with the *new* node's tools.
   It will mostly work, which is what makes it a nasty bug.

### 5.4 `pipeline.py` — node-aware turns (~90 lines changed)

- **Construct**: if `runtime_config.conversation.workflow` is present, build
  `WorkflowRunner`; else `self._workflow = None` and every path below is dead.
- **Greeting**: start node's `greeting` wins over
  `runtime_config.conversation.greeting`.
- **Delayed start**: `delayed_start_ms` before the greeting — for outbound,
  where speaking the instant the line opens gets the first second clipped.
- **Per turn**: refresh `history[0]` from `runner.system_prompt()`.
- **Tools**: pass `runner.local_tools()` into `run_turn` (`pipeline.py:1021`),
  plus `runner.allowed_tool_names()` to scope the policy set (§5.5).
- **After the turn**: if `pending_speech`, synthesize it *before* the next
  generation's audio, through the existing `_synthesize_sentence_stream` — and
  it is barge-in-able, same `cancel_event` as any other speech. A caller who
  interrupts "let me pull up the calendar" should be heard.
- **Terminals**: `pending_end` → existing
  `HandlerResponse(end_call=True, end_call_grace_period_ms=...)` path.
  `pending_transfer` → existing `transfer_engine`, with the node's
  `transfer_destination` override if set. Both reuse what's there; neither
  invents a new teardown path.

### 5.5 Per-node tool scoping (~15 lines in `policy_resolver.py`)

`enabled_tools(agent_id)` gains an optional `only: list[str] | None`. The DB
stays the source of truth for *what the agent may use*; the node narrows that
to *what this stage may use*. A node cannot grant a tool the agent doesn't
have — that would be a privilege escalation through the graph editor.

An empty `tools` list on a node means "no tools this stage", not "all tools" —
the restrictive reading, because the whole point of stages is withholding
capability until it's earned.

### 5.6 Disposition and call outcome

`end` nodes carry a `disposition` string written to `calls.disposition` when
the call ends there. Dograh derives theirs from an `EndTaskReason` enum plus
telephony statuses (`disposition_codes.py`) and lets organizations mint custom
codes per workflow.

We do the same, smaller: system dispositions are a fixed tuple in
`libs/config_sdk/workflow.py` (`completed`, `qualified`, `not_qualified`,
`transferred`, `abandoned`, `failed`), and an `end` node may set any string.
The calls list filter reads distinct values from the column rather than a
hardcoded frontend list — Dograh's comment on exactly this ("Keeping the list
here — rather than duplicated in the frontend — is what stops the filter
dropdown from drifting behind the code") is worth heeding.

### 5.7 `{{ variable }}` rendering

Variables come from three places, resolved in this order:

1. **Call context**, always present: `caller_number`, `called_number`,
   `direction`, `agent_name`, `current_date`, `current_time`.
2. **Campaign contact fields**, for outbound — `services/campaigns` already
   carries per-contact data.
3. **Extracted variables** (§5.8), which appear as the call progresses.

`WorkflowGraph.template_variables()` walks every prompt, greeting, and
transition speech and returns the set referenced. Publish-time validation
warns on any variable that is neither a call-context key nor declared by some
node's extraction config — catching `{{ custmer_name }}` in the editor instead
of hearing it in production.

**Not Jinja2**, though it's already in `requirements.txt`. Rendering happens
on strings that will contain caller-influenced extracted values; full Jinja on
that is a sandbox-escape surface we have no use for. Substitution is a regex
over `{{ name }}` and `{{ name | default }}`, ~25 lines. Unknown variables
render as the empty string, never as a literal `{{ x }}` reaching TTS — that
is the failure mode that ends up in a call recording.

### 5.8 `workflow/extractor.py` — variable extraction (~120 lines)

The feature that turns a workflow from a router into something that produces
data. Modelled on Dograh's `VariableExtractionManager` but on our own `ILLM`.

A node declares what to capture:

```json
"extraction": {
  "enabled": true,
  "prompt": "Only capture what the caller explicitly said.",
  "variables": [
    {"name": "policy_number", "type": "string", "prompt": "Their policy number."},
    {"name": "wants_callback", "type": "boolean", "prompt": "Did they ask to be called back?"}
  ]
}
```

On leaving that node, an **out-of-band LLM call** (not in the conversation
context — a separate request on the same provider) gets the transcript so far
plus the variable descriptions, and returns JSON. Results merge into the
runner's variable dict, so later nodes' prompts can say
`{{ policy_number }}`, and they land in `calls.extracted_variables`.

Details worth copying from Dograh, each earned the hard way:

- **Background by default.** `asyncio.create_task`, tracked in a pending set.
  Blocking the transition on an extraction round-trip adds a full LLM latency
  to a moment the caller is already waiting through.
- **Flush before anything that reads the values.** Dograh's
  `flush_variable_extraction()` awaits pending tasks before transfer routing
  and before final disposal, because a transfer that routes on
  `{{ wants_callback }}` cannot read a value still in flight. Same for call
  end.
- **Idempotent final extraction.** Multiple teardown paths converge (caller
  hangs up, agent ends, max duration, transfer completes). Dograh guards with
  `_final_extraction_done`. Without it you get duplicate extractions racing to
  write the same row.
- **Never let it fail the call.** Extraction is analytics. A timeout logs and
  moves on.
- **Strip transition-tool responses from the extraction transcript.** Dograh
  explicitly filters `{"status": "done"}` — dozens of them accumulate and they
  are pure noise to an extraction model.

### 5.9 Context management on transition (~60 lines)

Long calls accumulate context, and after several transitions much of it is
tool calls belonging to nodes the conversation has left. Dograh summarizes in
the background on every transition
(`pipecat_engine_context_summarizer.py`), replacing old messages with a
summary while preserving the system message and recent turns.

We already have `_trim_history` doing a cruder version — keep the last N
pairs, preserve `history[0]`. For workflows that is not enough: dropping the
turns where the caller gave their date of birth loses information the booking
node needs.

Our version, simpler than Dograh's because our history is a plain list rather
than a Pipecat `LLMContext`:

- On transition, if history exceeds a threshold, fire a background
  summarization on the same LLM.
- Replace everything except `history[0]` and the last two turns with one
  `role: "user"` summary message.
- **Apply-time snapshot, not request-time.** Dograh's comment flags this:
  messages added while the summary was generating must survive. Splice by
  index captured at apply time.
- **Cancel the in-flight summarization if another transition happens first.**
- On timeout or failure, keep the full context. Degrading to "more tokens" is
  always better than degrading to "lost the caller's name".

### 5.10 Graph parsing at prewarm

`_prewarm_agents` (`__main__.py:305`) already warms agent config at startup.
Parse graphs there too and cache the `WorkflowGraph` by
`(agent_id, config_version)`. Parsing per call is wasted work on the latency
path, and a parse failure discovered at call time is a dropped call — at
prewarm it is a log line and a fallback to the built-in starter graph.

---

## Part 6 — Frontend

`admin-ui` today has **three dependencies**: `next`, `react`, `react-dom`. No
UI library, no Tailwind, no state manager. This adds exactly one.

```
npm i @xyflow/react
```

Unavoidable and worth it. A canvas with drag, pan, zoom, connect, multi-select,
and edge routing is thousands of lines, and it is the most-touched surface in
the product. React Flow v12 is what Dograh uses and what every comparable tool
uses.

**No Zustand.** Dograh has a 459-line `workflowStore.ts` plus a 684-line
`useWorkflowState.ts`. That is scaled for their editor: version diffing,
embed dialogs, recordings, phone-call testing, MCP refresh. Ours is a tab
inside an agent page. React Flow's own `useNodesState`/`useEdgesState` plus
one `useState` for the selected element is sufficient, and a store would be a
dependency plus an indirection layer for state that never leaves the panel.

### 6.1 Files

| File | Lines | Purpose |
|---|---|---|
| `app/workflows/page.tsx` | ~230 | the agents list, and the New agent dialog that seeds `STARTER` |
| `app/workflows/[tenantSlug]/[agentSlug]/page.tsx` | ~45 | full-page editor shell |
| `app/workflows/[tenantSlug]/[agentSlug]/settings/page.tsx` | ~850 | the agent's own settings, one level under its flow |
| `components/workflow/WorkflowPanel.tsx` | ~400 | canvas, autosave, publish, error surfacing |
| `components/workflow/nodes.tsx` | ~300 | four node renderers + handles |
| `components/workflow/Inspector.tsx` | ~500 | node/edge property forms |
| `components/workflow/ExtractionEditor.tsx` | ~200 | the variable list editor |
| `components/workflow/VersionPanel.tsx` | ~200 | history, diff summary, rollback |
| `components/workflow/autoLayout.ts` | ~120 | tidy-up button (port of Dograh's `layoutNodes.ts`) |
| `lib/workflowApi.ts` | ~150 | draft/publish/versions calls |

~1,900 lines against Dograh's 18,191. The gap is version-diff UI (694 lines),
the embed dialog (1,101), a settings page (1,874), recordings (652), the
in-browser phone tester (603 + 925 + 871 for WebRTC) — and the 1,100-line
generic property renderer we're skipping per §2.2. We already have
`TestAgentPanel.tsx` for browser-mic testing.

### 6.2 Layout

```
┌───────────────────────────────────────────────┬────────────────────────┐
│  ⊕ Node ▾   ⟲ Tidy      ● Draft   [ Publish ] │  BOOKING        agent  │
├───────────────────────────────────────────────┤                        │
│                                               │  Name    [booking____] │
│    ┌─────────┐  caller wants to book          │                        │
│    │ ▶ start │──────────────┐                 │  Prompt                │
│    │ greeting│              │                 │  ┌──────────────────┐  │
│    └─────────┘              ▼                 │  │ Book an appoint- │  │
│         │            ┌───────────┐            │  │ ment. Get a date │  │
│         │            │  booking  │  ⚠         │  │ and time...      │  │
│         │ just a Q   │  🔧 1 tool│            │  └──────────────────┘  │
│         ▼            └─────┬─────┘            │  {{ }} inserts a var   │
│    ┌─────────┐             │ booked           │                        │
│    │   q&a   │             ▼                  │  Tools                 │
│    │  📚 kb  │       ┌───────────┐            │  [x] book_appointment  │
│    └────┬────┘       │  ■ end    │            │  [ ] cancel_appt       │
│         └───────────►│  goodbye  │            │                        │
│                      └───────────┘            │  Knowledge base        │
│                                               │  [ ] Clinic FAQ        │
│  ⚠ booking: two edges both named "yes"        │                        │
│                                               │  ▸ Extract variables   │
└───────────────────────────────────────────────┴────────────────────────┘
```

- Click a node → name, prompt, and type-specific fields. Click an edge →
  label, condition, transition speech.
- **The condition field is the important one.** It becomes the tool
  description and it is what actually decides the transition. The UI should
  give it more room than the label and say what it does — operators
  consistently under-write it, then wonder why transitions misfire.
- Node badges show tool count, KB attachment, extraction-on — so the canvas
  answers "what does this stage do" without clicking through.
- Validation errors from publish paint the offending node/edge and list at the
  bottom. Errors carry `{kind, id, field}` precisely so this works.

### 6.3 Draft, publish, versions

- Autosave the draft on a debounce. No save button, no lost work.
- A **Draft / Published** pill shows divergence.
- **Publish** validates server-side; on failure nothing is written and errors
  paint the canvas. On success the graph goes live, `config_version` bumps,
  Redis invalidates, and the next call picks it up.
- **Version panel** lists publishes with who and when. Rollback republishes an
  old version as a new one (append-only — never rewrite history, so "what was
  live at 3pm yesterday" stays answerable).

Skipping Dograh's structural version *diff* (694 lines across
`workflowVersionDiff.ts` and its dialog). "Published by X, 4 nodes, 6 edges,
2 hours ago" plus rollback covers the actual need — which is undoing a bad
publish, not auditing a graph line by line.

### 6.4 Testing from the editor

`TestAgentPanel.tsx` already runs a browser-mic call. Two additions make it a
workflow tool:

- **Test the draft.** A query param on the webcall session so an operator can
  hear a change before publishing to live traffic.
- **Highlight the active node.** Transitions are already events; the canvas
  lights up the current node as the call moves. This is the single
  highest-value observability feature in the whole editor — it turns "the
  transition didn't fire" from a guess into something you watch happen.

---

## Part 7 — Observability and testing

### 7.1 What gets recorded

| Where | Field | Answers |
|---|---|---|
| `transcript_entries` | `node_id`, `node_name` | which stage said this |
| `calls` | `nodes_visited JSONB` | the path this call took |
| `calls` | `disposition` | how it ended |
| `calls` | `extracted_variables JSONB` | what we learned |
| logs | one line per transition, with the tool name | why it moved |

That combination answers the questions that matter and that a single-prompt
agent could not answer at all: where do calls die, which edge never fires,
which stage runs long, which node correlates with a bad outcome.

The calls detail page shows the path as a strip —
`start → triage → booking → end` — with the transcript segmented by node.

### 7.2 Dry-run testing

Because `WorkflowRunner` has no voice dependency, a fake LLM that calls
transitions on cue walks the graph in milliseconds:

```python
def test_qualified_path():
    runner = WorkflowRunner(parse_graph(GRAPH), global_prompt="", base_suffix="",
                            variables={"caller_number": "+15551234"})
    assert runner.node.name == "greeting"

    tools = runner.local_tools()
    assert "caller_wants_to_book" in tools
    asyncio.run(tools["caller_wants_to_book"][1]({}))

    assert runner.node.name == "booking"
    assert "book_appointment" in runner.allowed_tool_names()
    assert runner.pending_speech == "Of course, let me pull up the calendar."
```

This is the check that every published graph should have: assert the happy
path reaches an `end` node, and assert no node is a dead end. Cheap enough to
run on every publish server-side, which is what §5.1's validation is.

Dograh cannot do this cheaply — their engine is welded to a Pipecat pipeline
and an `LLMContext`. Keeping `WorkflowRunner` free of voice concerns is what
buys it, which is why that constraint is worth defending in review.

### 7.3 Integration test

One end-to-end through the real pipeline with a scripted LLM that emits
transition calls, asserting the graph walks, the prompt swaps, tools scope
correctly, and the call ends on the `end` node. That plus the dry-runs covers
the logic; the browser test panel covers the feel.

---

## Part 8 — Build order

Not phases with features held back — this is dependency order for shipping the
whole thing. Each step is testable on its own, and nothing is user-visible
until the last two.

1. **`libs/config_sdk/workflow.py`** — model, parse, validate. Pure functions,
   fully unit-testable, zero integration. Everything else depends on it.
2. **Schema + config service** — columns, versions table, draft/publish/
   rollback endpoints, validation wired to publish. Testable with curl.
3. **`local_tools` in the orchestrator** — the 20 lines, with a test that a
   local tool executes without touching policy or provider, and doesn't
   consume a tool iteration.
4. **`WorkflowRunner`** — node walk, prompt composition, transition handlers,
   rendering. Dry-run tests from §7.2. Still no pipeline involvement.
5. **Pipeline wiring** — greeting, prompt refresh, tool passing, transition
   speech, end and transfer terminals, prewarm caching. First point a real
   call can run a graph.
6. **Extraction and summarization** — the two background LLM passes, with the
   flush and idempotency rules from §5.8.
7. **Observability** — node ids on transcript entries, path and disposition on
   calls, the path strip on the calls page.
8. **Editor** — canvas, inspector, autosave, publish, error painting.
9. **Versions and live test** — version panel, rollback, draft testing, active
   node highlight.

Steps 1–4 are ~650 lines with no integration risk. Step 5 is where it becomes
real and where the two traps in §5.3 will bite. Steps 8–9 are the bulk of the
calendar time and none of the risk.

### Not built

Two things this document describes that the change list above doesn't
actually pay for, left out rather than half-wired:

- **Per-knowledge-base scoping on a node.** A node's `knowledge_base_ids`
  is honored as on/off — a stage with nothing attached does no retrieval —
  but not as a filter, because `knowledge_sdk`'s `RetrievalPolicy` has no
  knowledge-base field and `services/knowledge/retrieval.py` has nothing to
  filter on. Both need the field before this can mean more than a toggle.
- **Campaign contact fields as `{{ variables }}`** (§5.7's second source).
  There is no data to carry: `campaign_contacts` stores only
  `phone_number`/`name`, and nothing plumbs either from an originated call
  through to Conversation Service. `WorkflowRunner`'s `variables` dict is
  the only seam it needs once contacts grow custom fields.

---

## Part 9 — Decisions to argue about now

1. **`agents.workflow` column vs a first-class `workflows` table.** Column,
   because `config_version` already drives Redis invalidation (§4.2). The
   counter-argument is real: Dograh treats a workflow as the top-level object
   with agents attached, which is right if one graph should serve many agents.
   Today it shouldn't. If that changes, the runtime doesn't care — it reads a
   dict either way.

   **Settled 2026-08-30:** the *UI* merged the two — one list, one object, the
   canvas as the way in, settings at `/workflows/{tenant}/{agent}/settings` —
   while the schema deliberately did not. Dograh has no `agents` table
   (`api/db/models.py:510`: `workflows` IS the agent), but their cardinality is
   1:1, same as ours, so the column already expresses it. Renaming the table
   would touch every FK into `agents`, the gateway's `agent_slug` routing and
   the config SDK's cache contract, and change nothing a user can see.

2. **Four hardcoded node types, no spec registry.** §2.2. Revisit only when
   something outside this repo needs to register a node type.

3. **`agents.system_prompt` becomes the global prompt.** No `global` node.
   Means an agent's Behaviour tab and its workflow are not independent — the
   prompt there applies to every node. That is the intent, but it should be
   said out loud in the UI or it will surprise someone.

4. **Cycles allowed, bounded by `max_call_duration_s`.** Same call as Dograh.
   The alternative — a per-node visit cap — is a knob nobody will tune.

5. **Transfer as a node type, not a tool.** §2.3. Costs a node type; buys
   correct integration with `transfer_engine` and the C++ `CallFSM`
   `Transferring` state instead of routing around both.

6. **An agent is single-prompt or workflow, never both.** **Settled
   2026-08-30: there is no single-prompt mode at all.** Every agent is created
   with a graph (`agents.create_agent`), `graph_for()` never returns `None`,
   and the six columns that held conversation text —
   `greeting`, `system_prompt`, `end_call_prompt`, `farewell_message`,
   `transfer_prompt`, `transfer_announcement` — are dropped. The always-on
   instruction is a `global` node on the canvas, the greeting is the start
   node's, the closing words are each end node's. Matches Dograh, which has
   no agent-level conversation text either (`NodeType.globalNode`).

   The `[[END_CALL]]`/`[[TRANSFER]]` tokens survive with **fixed** wording:
   they are the safety net for a caller who finishes where no edge covers it,
   not a second way to configure what the agent says.

7. **No text-chat runner.** Dograh maintains an 781-line parallel
   implementation of the graph walk for text chat. If we ever need one,
   `WorkflowRunner` already has no voice dependency — that is the whole reason
   for the constraint in §5.2.
