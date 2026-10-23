# Security review: .sdlc/guard-end-call-marker/02-design.md
VERDICT: GREEN

## Findings
None. The change is a pure, sync, per-turn decision over session-local inputs (the caller's STT text, the model's directive-free reply, the session's spoken language). It adds no endpoint, no data access, no tenant-scoped read or write, no token handling and no outbound I/O. The only new sink is one log line. No actor gains access, privilege or data they lacked before: a caller who says "goodbye" can end their own call, as they already can today, and the guard only narrows when the model's marker is honoured.

## Verified controls
- Log hygiene (lesson 33): the specified `end_call_rejected` format carries only a fixed reason token and `session_id`, and AC8's test asserts that neither the caller text nor the reply text appears in caplog. Design Interfaces/Risks; existing neighbour logs at `services/conversation/pipeline.py:1351` follow the same pattern.
- No cross-session state: `directives` is a per-turn local list (`pipeline.py:799`) mutated in place, and `_end_call_rejection` is pure, with no module-level cache or shared mutable state, so one session's guard cannot affect another's.
- Language input cannot drive module loading: i18n tables are imported once at import time from the package path (`services/conversation/i18n/__init__.py:52-66`). The lookup is a dict `.get` with an `en` fallback (`:75`, `:82`), and the new accessors mirror `tool_fillers`. The session language (`pipeline.py:591-598`) cannot reach `import_module`.
- Phrase matching is literal token-sequence containment on casefolded text, with no regex built from caller or model input, so there is no ReDoS or injection surface.
- The end-call latch (AC12): the only consumer that acts on the flag is `on_speech_ended`, which today assigns `end_call = marker_seen` per chunk (`pipeline.py:816`) and acts on it only after the loop (`:892-896`). The design moves that read to `directives` after the loop. The other call site (`:1056`) discards the flag. Nothing inside the loop yields `end_call=True` to the servicer.
- Fail-safe direction: an unknown language, a missing table or an empty reply rejects the marker and keeps the call open with localized fallback audio (`pipeline.py:1382-1388`), never silence or an unintended hangup.
