# Code review
VERDICT: GREEN
Coverage: callers 7 checked · raises 3 traced · promises 4 matched · contracts 1 compared · failure-visibility done · env ok

Scope: merge fbaac5b (origin/redesign fc6414a into 516801e), conflict resolutions and #99/#103 semantic clashes only.

No findings.

Evidence:
- (a) Directive on every caller-facing LLM request: there are only two, `pipeline.py:1240` (no orchestrator, `with_directive`) and `tools/orchestrator.py:159` (the only `generate` in run_turn, `with_directive(history, trailing_message)`). #99's taint-carry path (`pipeline.py:1053` carry_taint=True -> `_llm_to_tts` :1292 -> `_token_stream` -> run_turn :1248-1253) still passes `trailing_message`. `with_directive` (`providers/interfaces.py:53`) returns a copy, so `history` and `_fold_tool_result_into_history` never see it. #99 adds no other caller-facing generate. Its `OpenAICompatChat.complete` is ingest-time contextualisation in services/knowledge, and sentiment, finalizer and extractor match the branch.
- (b) Taint: `guard.tainted = carry_taint and self._call_taint[...]` (orchestrator.py:108) and the held-write check (:204) read run_turn args and per-call state, never the directive. `answer_in` (pipeline.py:1434) is `language_name()` of a registry code, not tenant text. `sources` stays defanged and truncated (:1432), and `outcome="partial"` is kept (:1437-1439).
- (c) `model_pulled` uses the per-instance `guarded_client` (ollama.py:93-94,101), so the URL guard applies. The warning logs a fixed `failure_code` plus the model name and never base_url or the exception text. `aclose` closes the same client (ollama.py:253), and its callers are `__main__.py:535` and `provider_bundle.py:138`. A guard refusal surfaces as `httpx.ConnectError` (a TransportError, so it's caught and read as pulled).
- (d) Counters: 99+7=106 and 55+7=62. The bases agree on both sides. Both tests assert `==` and pass.
- Targeted tests: 2 counter tests passed, and 363 conversation tests (ollama, pipeline, pipeline_knowledge, orchestrator, language) passed.
