# Security review: dynamic-first-turn-filler (code, `git diff 3ffc891 -- services/ docs/`)
VERDICT: GREEN

## Findings
None.

Considered and dropped, since no attacker gets anything:
- A file placed in `services/conversation/i18n/` gets imported at startup (`i18n/__init__.py:53-57`). Putting one there needs write access to the deployed source tree, which already means code execution. That is not a new trust boundary.
- The first-turn filler length reflects the agent's mean tool latency, and the caller on that agent's line can hear it. That caller is already on this agent's line, the signal is one of a few coarse length tiers, and the existing per-tool filler path (`pipeline.py:1325-1331`) already shows the same thing. Nothing crosses a tenant or agent boundary.
- Hindi-only spoken strings and the `first_turn_filler` key removal are correctness concerns for the critics. They are not security issues.

## Verified controls
- Dynamic import input: `importlib.import_module` (`i18n/__init__.py:55`) only gets `f"{__name__}.{info.name}"`. `info.name` comes from `pkgutil.iter_modules(__path__)` over the package's own directory. No env var, config value, caller language tag or LLM output reaches it. It is the only dynamic import in `services/conversation` and `libs/config_sdk`.
- Discovery runs once at import, not on the call path. `_TABLES` and `_SHORTEST` are frozen module-level dicts built when the module loads (`i18n/__init__.py:67-70`).
- Path traversal through the language code is not possible. At runtime the language reaches `t`, `tool_fillers` and `shortest_tool_filler` only as a dict key after `normalize_language`. That function applies `^([A-Za-z]{2,3})(?:[-_].*)?$` and returns just the lowercased 2-3 letter group (`libs/config_sdk/languages.py:56-64`). An unknown code falls back to `en` and never touches the filesystem or the importer.
- Module-name contract: `_validate` rejects any module whose name is not its own normalized code (`i18n/__init__.py:26-27`), so `en_old`, `x.y` and similar fail startup. It also type-checks `STRINGS` and `TOOL_FILLERS`, and rejects non-finite or non-positive durations (`:28-50`). The ValueError text only contains repo-authored keys and names, produced at import time.
- Tenant and agent isolation of the agent-wide read: `agent_average_ms(self._tenant_id, self._agent_id)` (`pipeline.py:784-787`) uses the same pair as the existing per-tool read. `tenant_id` is `ctx.tenant_id` from the server-side `SessionContext` (`__main__.py:387`), and `agent_id` is `runtime_config.agent.id` (`pipeline.py:377`). The store partitions by tenant first (`tool_latency.py:93`) and filters to `agent == agent_id` inside that tenant's map (`:98`). No other tenant's or agent's samples are read.
- Fail-closed on missing identity: empty or whitespace tenant or agent id returns None and the default target is used (`tool_latency.py:90-91`). `self._agent_id or ""` therefore never matches another agent's keys.
- Bounded work on the call path: `agent_average_ms` scans at most `_MAX_KEYS_PER_TENANT=200` keys, each with a deque of at most `_WINDOW=50` samples, once per call on turn 1. It snapshots the key list before `average_ms` can delete entries (`tool_latency.py:97-98`), so mutating the map while iterating it cannot happen. The outer map stays capped at `_MAX_TENANTS=2000`.
- Spoken text comes only from repo tables: the first-turn filler is `select_tool_filler("first_turn", ...)`, which picks from `tool_fillers(language)`. The error path returns `shortest_tool_filler(language)` from `_SHORTEST`. No caller, LLM or tenant-authored text reaches TTS through this change. Agent overrides are outside this diff.
- Logs: the new path logs only the constant `"first_turn"` (`fillers.py:50`, `%r`) and a repo-authored phrase. The diff adds no logging of transcript, caller or LLM content.
- No new surface: the diff adds no routes, gRPC methods, proto changes or env vars. The changes are internal to the conversation service handler.
