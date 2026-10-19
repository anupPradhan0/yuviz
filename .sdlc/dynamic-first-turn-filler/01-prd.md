# PRD: Dynamic, latency-aware first-turn filler

## Problem
Callers of every agent hear the same fixed "Mm-hmm, one moment." on turn 1 (`first_turn_filler` in `services/conversation/i18n/en.py:11`, spoken at `services/conversation/pipeline.py:784-788`), however fast or slow the agent's tools usually are. The latency-aware `FillerSelector` (`services/conversation/fillers.py`) only runs for tool calls (`pipeline.py:1313-1338`) and never for the first turn, so a tenant whose tools answer in ~1s gets the same phrase as one whose tools take 3s. Market research skipped: this is codebase-specific tuning of an existing in-house selector, not a solved-elsewhere product feature.

## Scope
- In: The turn-1 filler is chosen by the existing latency-aware selection logic instead of the fixed string, using the agent's recorded tool-latency history when it exists.
- In: With no usable history (new agent, process restart, window empty), turn 1 falls back to a sensible default phrase and never to silence or an error.
- In: The chosen phrase is in the call's spoken language, using the same per-language pools as tool fillers.
- In: Turn-1 filler is still spoken at most once per call, and behaves as it does today for barge-in and for calls where it is not spoken.
- In: Turn 1 and the first tool-call filler on the same turn do not repeat the same phrase back to back.
- In: Adding a language is a drop-in module, with automatic discovery and startup validation (see Addendum).
- In: A read-only `agent_average_ms` reader is added to `tool_latency.py` so turn 1 can read an agent-wide average.
- In: The `first_turn_filler` i18n key is removed from `en.py` and `hi.py`.
- Out: Changing tier thresholds, phrase ordering, or the turn-2+ min-gap.
- Out: Changing how latencies are recorded or windowed in `tool_latency.py`, or persisting them across restarts.
- Out: Shipping translations for es/fr/de/pt/it/ja/zh (need native review), and any UI or per-agent configuration for fillers.

## Acceptance criteria
1. Given an agent with recorded tool latencies averaging about 1s, when the caller's first utterance arrives, then the spoken turn-1 filler is drawn from the tier that `select_tool_filler` returns for that agent's measured average.
2. Given two agents of the same tenant whose recorded latencies differ enough to fall in different pool tiers, when each takes turn 1, then each speaks a phrase from its own tier.
3. Given no latency history for the tenant and agent, when turn 1 occurs, then a phrase from the selector's default (uncalibrated) path is spoken and no exception is raised.
4. Given `ToolLatencyStore` is absent (`None`) or `average_ms` returns None, when turn 1 occurs, then the default-path phrase is spoken.
5. Given the selector raises internally, when turn 1 occurs, then a fallback phrase is still spoken and the error is logged once.
6. Given a call whose spoken language is not English, when turn 1 occurs, then the filler comes from that language's pool, and a language with no pool falls back to English rather than failing.
7. Given a call where `first_turn_filler_spoken` is already true, when a later user turn occurs, then no first-turn filler is spoken.
8. Given turn 1 spoke filler X and the LLM then starts a tool call on the same turn, when the tool-call filler is chosen, then it is not X (existing last-phrase avoidance applies).
9. Given the caller barges in during the turn-1 filler, when the cancel event fires, then behaviour matches today (the filler stops and the turn is cancelled).
10. Given concurrent calls share one `FillerSelector`, when they take turn 1 at once, then each reads only its own tenant and agent latency and none leaks another tenant's history.
11. Given the existing tests that pin the hardcoded phrase (`services/conversation/tests/test_i18n.py`), when the change lands, then they are updated to assert the new behaviour, and new tests cover criteria 1-10 (and the addendum criteria 12-20) using the deployed tuning values.

## Addendum (2026-10-18)
Scope addendum from `00-request.md`; design in `02-design.md` "Scope addendum"; contributor steps in `docs/design/multilingual-agents.md` "Adding a language".

12. Given a new file `i18n/<code>.py` is added, when the service imports `i18n`, then that language is available with no edit to any registry or list.
13. Given a module file in `i18n/`, when it is considered for discovery, then a module whose name starts with "_" is skipped as a helper, and any other module whose name is not its own lowercase 2-3 letter base code fails startup (it is not skipped).
14. Given any discovered module, when `i18n` is imported at startup, then its `STRINGS` keys must be a subset of `en`'s (a key not in `en` fails startup; a language may omit keys and falls back to English per key), and `TOOL_FILLERS` must be a non-empty tuple of `(phrase, seconds)` where `seconds` is finite, greater than 0 and not a bool.
15. Given a discovered module that is malformed (a `STRINGS` key not present in `en`, empty or non-tuple `TOOL_FILLERS`, or bad seconds such as 0, negative, NaN, inf or True), when the service starts, then import fails loudly with an error naming the module, and no malformed module is ever first hit mid-call.
16. Given a call in a language that has a module, when the turn-1 or a tool-call filler is selected, then the phrase comes from that language's own `TOOL_FILLERS`.
17. Given the selector raises for a call in a language that has a module, when the filler is chosen, then it is that language's own shortest phrase, and English is used only when that language has no module.
18. Given a language code with no module, when any filler or string is requested, then English is used and no exception is raised.
19. Given a call is in progress, when fillers or strings are looked up, then no directory scan or module import runs (discovery happens only at import).
20. Given the repository after this change, then no es/fr/de/pt/it/ja/zh module is shipped, because those translations need native review.

## Constraints
- Reuse `FillerSelector` and the pools in `services/conversation/i18n/`; do not fork a second phrase list (`fillers.py:10-15`).
- Latency lookup is keyed by tenant, agent and tool (`tool_latency.py:60`), so tenant isolation is preserved. See lessons 2 and 36.
- Turn 1 is spoken before the LLM chooses a tool (`pipeline.py:784-790`), and the selector is shared across concurrent calls (`fillers.py:49`), so no per-call mutable state may live on the selector.
- The filler must not add dead air before first audio. Per lesson 51, any lookup must be in-memory and non-blocking, with no new I/O on the turn-1 path.
- Per lesson 45, every phrase handed to TTS is already normalised by `_synthesize_sentence_stream`; do not bypass it.
- Tests exercise deployed constants (lesson 25), and the change is driven in a real call or the web test (lesson 23).

## Open questions
1. RESOLVED: Turn 1 happens before any tool is named, so which latency drives the target? Answer: the agent-wide average per tool (mean across the agent's tools that have recorded history), read via the read-only `agent_average_ms`.
2. RESOLVED: Should the filler be skipped when tools are very fast? Answer: no; turn 1 always speaks a filler, with a default 1.6s target when there is no history.
