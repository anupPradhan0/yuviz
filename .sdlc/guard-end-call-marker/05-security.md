# Security review: guard-end-call-marker (code, base 9b90f7d)
VERDICT: GREEN

## Findings
None. The change is local to the conversation service's in-process turn handling. It adds no endpoint, query, credential, cache key or role path, and it does not cross a tenant boundary.

## Verified controls
- No new reachable surface: the diff touches only `services/conversation/pipeline.py`, `services/conversation/i18n/*` and tests. It adds no routes, RPCs, DB access or auth paths, so there is nothing new for tenant isolation, privilege or IDOR checks to cover.
- The guard only tightens: `_end_call_rejection` and the scripted branch (`pipeline.py` around lines 1419-1431) can only remove an `EndCallDirective`, never add one. A caller who says "bye" to the LLM gets no new way to hang up the call beyond what the unconditional marker already allowed.
- Log hygiene: the rejection log carries only `reason` and `session_id`, with no caller utterance or reply text. The i18n warning logs only the module code and attribute names.
- Untrusted language input: `has_phrase_tables`, `closure_phrases` and `farewell_phrases` handle an unknown or None language without raising. An unknown table means the marker is trusted (the old behaviour) and the English phrases are used as the fallback, so there is no crash on attacker-influenced session language.
- Input processing cost: `_PHRASE_SPLIT_RE` is a single character class with `+`, so it runs in linear time. Phrase matching is bounded substring search over fixed small tables, so there is no ReDoS on caller transcripts.
- Phrase tables are validated at import (`i18n/__init__.py` `_validate`), so a malformed module fails startup and is not silently ignored.
- Both `_llm_to_tts` call sites (`pipeline.py:841`, `pipeline.py:1090`) were checked. The primary path now reads `end_call` from the guarded `directives` list after the stream. The secondary path does not use the yielded flag (`_end_call`).
