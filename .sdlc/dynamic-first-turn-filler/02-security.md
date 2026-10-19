# Security review: .sdlc/dynamic-first-turn-filler/02-design.md
VERDICT: GREEN

## Findings
None.

## Verified controls
- Latency store is partitioned by tenant at the outer map (`tool_latency.py:22-28`, `TenantKey` -> `OrderedDict[(agent_id, tool_name)]`). The proposed `agent_average_ms` indexes only `self._tenants.get(tenant_id)` and filters on `agent_id`, so no tenant can read another tenant's history (AC 10).
- `agent_average_ms` reuses the `average_ms` path (`tool_latency.py:60-81`), including the empty or whitespace tenant/agent guard. An empty `ctx.tenant_id` therefore returns None (default tier) and never falls into a shared bucket. Note that the `"default"` substitution at `__main__.py:95` applies to config loading only, not to `tenant_id=ctx.tenant_id` at `__main__.py:387`.
- The tenant id comes from the server-side session context, which `__main__.py:84` checks against the dispatch grant. It reaches the handler through `pipeline.py:374`, the same source the existing tool-filler lookup at `pipeline.py:1319-1321` uses. No tenant id comes from caller input.
- Agent id comes from the tenant-scoped runtime config (`pipeline.py:380`), not from caller input.
- The shared `FillerSelector` (`__main__.py:319`) holds no per-call state under the design. Last-phrase state stays on the per-session `SessionState`, so concurrent calls cannot see each other's phrase choice.
- Snapshotting the inner-map keys before the per-tool `average_ms` calls (which may `del` keys) avoids mutating the dict while iterating it. The lookup is bounded by `_MAX_KEYS_PER_TENANT = 200` (`tool_latency.py:16`) and is synchronous with no I/O, so it adds no new DoS surface on the turn-1 path.
- Timing side channel: the turn-1 phrase and its length depend only on the caller's own tenant and agent history. Per lesson 2, this is per-caller invariant with respect to other tenants.
- Import-time i18n discovery runs only over package source files, which deployers control. No request input reaches `importlib`, so no attacker can steer which module loads. A malformed module fails closed at startup.
- The exception log in the selector carries only `tool_name="first_turn"`. No tenant data, transcript or PII is added to logs.
