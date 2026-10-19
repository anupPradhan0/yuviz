# Security review (r2, fresh re-audit): working-tree diff vs 3ffc891 (services/), feature dynamic-first-turn-filler (#98)
VERDICT: GREEN

## Findings
None. Counts: critical 0, high 0, medium 0, low 0.

What changed: the diff adds a synchronous, in-memory read, `ToolLatencyStore.agent_average_ms`. It also moves the turn-1 filler from a fixed i18n string to the existing `FillerSelector`, and removes the `first_turn_filler` i18n keys. It adds no endpoint, no I/O and no new input. I re-derived every control below from the code rather than from r1, and I could not build an attack path across a tenant or agent boundary.

Leads I examined and dropped, because no boundary is crossed:
- Latency side channel. The filler length tier depends only on the same agent's own tool latency over the last 15 minutes. A caller can learn, coarsely, that this agent's tools were slow recently. That stays inside one tenant and one agent, and it carries no PII or third-party data.
- Read-path side effects. `average_ms` calls `move_to_end` and deletes aged-out keys, and the new read inherits that. The read never inserts a tenant, so it cannot push another tenant out through the `_MAX_TENANTS` / `_MAX_KEYS_PER_TENANT` LRU limits. All it can do is refresh or reclaim the caller's own tenant and agent entries.
- Concurrent mutation during the snapshot. The server is `grpc.aio` (`__main__.py:470`, `servicer.py:23`). No executor or `to_thread` touches the store, and `agent_average_ms` never awaits. So the key snapshot at `tool_latency.py:98` cannot raise "mutated during iteration", and calls cannot crash each other's turn 1.

## Verified controls
- Tenant partition on read: `agent_average_ms` reads only `self._tenants.get(tenant_id)`, the outer per-tenant map, and never iterates other tenants (`services/conversation/tool_latency.py:93`).
- Agent scoping on read: inner keys are filtered by `agent == agent_id` before averaging, so sibling agents in the same tenant are excluded (`tool_latency.py:98`). The new test `test_agent_average_does_not_see_other_tenant` covers the case of the same agent id under another tenant.
- Read and write use the same keys: the write path (`tools/middleware.py:82-84`) keys on `ToolExecutionContext.tenant_id/agent_id`. The orchestrator builds that context (`tools/orchestrator.py:389`) from the pipeline's `self._agent_id`/`self._tenant_id` (`pipeline.py:1257`). These are the same two fields the new read uses (`pipeline.py:785`).
- Where the ids come from: `self._tenant_id` is `ctx.tenant_id` from the session context (`__main__.py:387`). `self._agent_id` is the server-resolved `runtime_config.agent.id` (`pipeline.py:376`). Neither comes from caller speech, the LLM or a request body, and the diff adds no new trust in client input.
- Blank-id guard: the read returns None for an empty or whitespace tenant or agent (`tool_latency.py:90`), matching `record` (`tool_latency.py:32`). A legacy session (`agent.id` "" mapped to None, then "") therefore never reads a shared bucket.
- Bounded work on the call path: the read runs at most `_MAX_KEYS_PER_TENANT` (200) x `_WINDOW` (50) steps (`tool_latency.py:13,16`). It runs once per session, guarded by `first_turn_filler_spoken`, which is set before the read (`pipeline.py:781-783`), and it never awaits.
- No LLM or caller text reaches spoken output: the phrase is always taken from the fixed i18n `TOOL_FILLERS` pool (`fillers.py:36`). The language is normalized with an English fallback (`i18n/__init__.py:28-30`). The average only picks a length tier and is never spoken.
- No sensitive data in logs: the selector's only log line logs `tool_name`, which here is the literal "first_turn", and then returns a constant (`fillers.py:51-53`). No tenant, agent or latency value is logged.
- No cross-call state in the shared selector: `FillerSelector` holds no instance state. The last phrase is stored per session in `SessionState.tool_call_filler_last_phrase` (`pipeline.py:792`).
- Removed i18n key is fully unreferenced: grep over `services/` finds no remaining `t("first_turn_filler", ...)` caller, so no path speaks a raw key or fails mid-call.
- No new unauthenticated surface: the change is internal to the per-stream handler and adds no HTTP or gRPC route.

## Comparison with 05-security.md (r1)
Both reviews are GREEN with zero findings. This audit independently re-confirmed all 11 of r1's controls against the current code; line references are unchanged. It also adds two checks r1 did not make explicit: that the read path cannot evict or disturb another tenant's LRU entries, and that the `grpc.aio` single-loop model rules out concurrent-mutation crashes during the key snapshot. Nothing in r1 is contradicted.
