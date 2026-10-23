# Code review (merge of origin/redesign into fix/128-guard-end-call-marker)
VERDICT: GREEN
Coverage: callers 2 checked (main-turn _produce call `pipeline.py:919`, transfer-failed recovery `pipeline.py:1202`) · raises 1 traced (producer exception → queue → `log.error(..., exc_info=item)` `pipeline.py:945-947`) · promises 4 matched (guard after flush `pipeline.py:~1533`, end_call read after stream `pipeline.py:968`, cancelled turns skip guard and hangup `pipeline.py:~1534`/`:1066`, rejected marker → end_call_check fallback `pipeline.py:~1560`) · contracts 1 compared (_llm_to_tts 3-tuple; third item unused at both call sites) · failure-visibility done · env ok (1342 passed)

No findings.

Checks performed:
- (a) Nothing dropped or duplicated. The +/- lines of `git diff origin/redesign -- pipeline.py` match the branch's own change (`git diff origin/redesign...origin/fix/128`) one for one. The only differences are the merged import line (`tool_fillers` kept) and the `_llm_to_tts` call reflowed inside `_produce`. test_pipeline.py is the same apart from the import line. Numstat is identical except +1 line in pipeline.py, which comes from that reflow. No conflict markers.
- (b) The old `except Exception: log.exception` was redesign's removal, not a loss from this merge. The producer catches non-cancel exceptions, puts them on the queue, and the consumer logs them with the full traceback (`exc_info=item`). On an exception, end_call is still `any(EndCallDirective in directives)`, which is the same as the fix/128 parent. Redesign used the last `marker_seen`, which is equivalent or looser. So the exception path is no worse than either parent.
- (c) No race. `_STREAM_END` is put only after the `async for` over `_llm_to_tts` finishes, so the guard and its `directives[:]` rewrite run before the consumer reads `end_call` at `:968`. When the producer runs ahead during a cancel, any extra directives are harmless: the guard is skipped (`not cancel_event.is_set()`), and the hangup at `:1066` also requires `not cancel_event.is_set()`.
- (d) `_marker_seen` is unpacked and never read. end_call comes only from `directives`.
- (e) The transfer-failed recovery call site is unchanged from both parents. It uses the default `caller_text=""` and keeps `silent_turn_text="transfer_failed_fallback"`, so the `end_call_check` override does not apply there.
