# PRD: Guard the [[END_CALL]] marker against mid-conversation hangups

## Problem
Callers get hung up on mid-conversation. The pipeline ends the call whenever the model's reply carries `[[END_CALL]]` (`services/conversation/directives.py:133`, latched at `services/conversation/pipeline.py:1350` and `:1378`). Nothing checks that the conversation is over. In a 2026-10-18 local test, `llama3.2` answered an open question with a question of its own plus the marker, and the call was dropped.

Market research skipped: this is codebase-specific behaviour of our own directive protocol, not a solved-elsewhere product problem.

## Scope
- In: a marker in a reply that ends in a question to the caller never ends the call. The text is still spoken and the call stays open.
- In: a marker that follows a caller closure signal (goodbye, "that's all", "no more questions") in the caller's last utterance still ends the call.
- In: a marker on a reply that ends in a farewell (non-question) phrase from the i18n table still ends the call, so legitimate "issue resolved" hangups allowed by `_END_CALL_CONDITION_DEFAULT` (`pipeline.py:105`) keep working.
- In: scripted mode (`_build_end_call_instruction(scripted=True)`) with the token as the only reply still ends the call.
- In: unscripted mode with a reply that is only the marker (no spoken text), when the caller's last utterance is not a closure signal, does not end the call. The caller gave no closure signal and the reply has no farewell, so the marker is rejected and stripped, and the call stays open. No fallback goodbye is synthesized for a rejected marker. A rejected EndCallDirective is removed from the turn's directive list so the never-go-silent branch (`not directives`) can fire. The empty remainder falls through to the existing never-go-silent branch, which speaks the localized `end_call_check` string ("Is there anything else I can help you with?") rather than the error apology, so the caller never hears dead air or a false error.
- In: closure detection checks the detected-language table and the English table, so a caller in a non-English session who says an English goodbye still ends the call. Farewell and closure detection works in every language that has a table in `services/conversation/i18n/`, with no English-only keyword list.
- In: a rejected marker is stripped from the spoken text, speaks the rest of the reply, and logs one `end_call_rejected` line with a reason and session id.
- In: the guard runs only when a marker is present.

## Out of scope
- Mid-call language drift (model replies in a language the session did not switch to). Covered by PR #103's safety net.
- Changing the end-call prompt text (`_END_CALL_CONDITION_DEFAULT`, per-agent `end_call_prompt`) to make the model less likely to emit the marker.
- Workflow End-node hangups and transfers. Only the off-graph `[[END_CALL]]` marker is guarded.
- An LLM or classifier call to judge closure.

## Acceptance criteria
1. Given a reply whose last spoken sentence ends in `?`, `؟` or a full-width `？`, followed by `[[END_CALL]]`, when the turn completes, then the call does not end and the reply text is spoken. The Hindi danda `।` is deliberately not a question terminator: the hi `fallback_goodbye` ends in `।` and must still end the call (conflict with AC5; see 02-design.md).
2. Given the caller's last utterance is a goodbye and the reply is a farewell plus `[[END_CALL]]`, when the turn completes, then the call ends as it does today.
3. Given scripted mode and a reply that is only `[[END_CALL]]`, when the turn completes, then the call ends and the localized goodbye still plays as today.
4. Given an unscripted agent, a non-closing caller utterance, and the reply "Your booking is confirmed. Goodbye. [[END_CALL]]" (ends in a non-question farewell phrase from the i18n table), when the turn completes, then the call ends; and given the same caller utterance with the reply "Your booking is confirmed. [[END_CALL]]" (no farewell phrase, no question), then the call stays open and one `end_call_rejected` line is logged. A trailing question vetoes in all cases (AC1).
5. Given a closure phrase in Hindi (`hi`) as the caller's last utterance, when the marker arrives, then the call ends, with no English text involved.
6. Given a new language module is added under `services/conversation/i18n/` without closure phrases, when the module loads, then it falls back to English with exactly one warning naming the module, and a test asserts both.
7. Given a language with no i18n table, when the marker arrives after a non-closing caller utterance and a reply with no farewell phrase, then the call stays open (the guard fails toward keeping the call).
8. Given a marker is rejected, when the turn completes, then exactly one `end_call_rejected reason=<token> session=<id>` log line is emitted and it contains no caller or agent transcript text.
9. Given a marker is accepted, when the turn completes, then no `end_call_rejected` line is logged.
10. Given a rejected marker, when the reply is synthesized and stored in history, then the literal `[[END_CALL]]` is in neither the audio text nor the history, and no EndCall is sent to the servicer.
11. Given a reply with no marker, when the turn runs, then the guard code is not executed (a test asserts the check is not called) and no extra await or I/O is added to the path.
12. Given the marker arrives mid-stream with `end_call` already yielded as true (`pipeline.py:1355`), when the guard later rejects it, then no consumer has already acted on `end_call=True` for that turn.
13. Given a same-turn pending transfer plus a marker, when the turn completes, then the transfer still wins as today (`test_pipeline.py:2607`).
14. Given the marker is emitted after a tool-token event (`test_pipeline.py:2590`), when the guard evaluates it, then the same rules apply.
15. Given the guard tests, when run, then they use the deployed i18n tables and are in `services/conversation/tests/`, and the existing end-call tests at `test_pipeline.py:669` and `:2208` still pass unmodified. The intentionally changed existing tests are `test_pipeline_marker_only_reply_gets_fallback_audio` (`test_pipeline.py:582-605`), see AC16, and `test_an_end_call_marker_survives_a_transition_in_the_same_turn` (`test_workflow_pipeline.py:167-182`), see AC19.
16. Given an unscripted agent, a caller utterance that is not a closure signal (the existing test's "tear down the call"), and an LLM reply that is only `[[END_CALL]]`, when the turn completes, then no response has `end_call=True`, no EndCall is sent, the localized `end_call_check` string from the never-go-silent branch is passed to `tts.synthesize` and its audio is yielded, the call stays open, `_FALLBACK_GOODBYE` is never passed to `tts.synthesize`, the history contains no `[[END_CALL]]`, and one `end_call_rejected` line is logged. The test at `test_pipeline.py:582-605` is rewritten to assert exactly this, and is renamed to reflect it.
17. Given an unscripted agent, a caller utterance that is a closure signal (e.g. "goodbye"), and an LLM reply that is only `[[END_CALL]]`, when the turn completes, then the call ends and fallback goodbye audio is yielded before `end_call=True`, as today.
18. Given a session in `hi` with supported languages `en,hi`, when the caller's last utterance is the English "bye" or "that's all" and the reply is only `[[END_CALL]]` or a non-farewell, non-question statement such as "All set. [[END_CALL]]", then the call ends (so it can pass only through the English-table closure match, not through a farewell in the reply); and given an English non-closing utterance such as "tell me more" with a reply that has no farewell, then the call stays open.
19. Given the existing test `test_an_end_call_marker_survives_a_transition_in_the_same_turn` (`test_workflow_pipeline.py:167-182`), whose reply "All set. [[END_CALL]]" has no farewell phrase and follows a non-closing caller turn, when the guard ships, then the test is rewritten so the reply is "All set. Goodbye. [[END_CALL]]" (a farewell phrase), keeps its same-turn tool-call transition and its `any(r.end_call ...)` latch assertion, and passes; "All set" is not added to the farewell table.

## Constraints
- Language tables are discovered and validated at import in `services/conversation/i18n/__init__.py`. Missing languages and keys fall back to English, and `t()` raises `KeyError` on an unknown key. Closure and farewell phrases must follow that contract (lesson 46: prove each language's path works, fall back with one loud log).
- The spoken language comes from `_spoken_language(session_id)`. Closure matching checks the detected-language table and the English table (the union), not the agent's configured language alone.
- `end_call` is yielded per token before the full reply is known (`pipeline.py:1355`). The guard must decide before any consumer acts on it (lesson 49: trace the recovering line on every path, including workflow turns, prewarm and the web test).
- A new `def` in `pipeline.py` must not land inside another function's body. Re-read the whole enclosing function (lesson 35).
- Logs must carry no transcript text (lesson 33: enumerate sinks), only reason and session id.
- Tests exercise deployed constants (lesson 25) and must be able to fail: a case where the marker ends the call and one where it does not, each asserting the exact expected outcome (lesson 12).

## Open questions
- Which languages must ship closure and farewell phrases now? Assumption: `en` and `hi`, the only tables present.
