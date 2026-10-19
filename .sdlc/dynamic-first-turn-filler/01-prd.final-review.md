# Review: 01-prd.md (criteria 1-20) against built code
VERDICT: AMBER

Coverage map (code satisfies / proving test):
- 1 yes / test_pipeline.py:309 (fast 1000ms -> 1.0 tier, slow 2400ms -> 2.4 tier; pipeline.py:781-794)
- 2 yes / test_pipeline.py:309
- 3 yes / test_pipeline.py:329 (empty_store), :343 (hi default tier)
- 4 yes / test_pipeline.py:329 (no_store), test_fillers.py:29 (invalid averages)
- 5 yes / test_pipeline.py:369 (one filler, one log record), test_fillers.py:35
- 6 yes / test_multilingual.py:422, :433; test_i18n.py:53, :157
- 7 partly / test_pipeline.py:277 (cannot tell the flag from the `not history` check; the flag-only path is not driven)
- 8 yes / test_pipeline.py:2308
- 9 NO TEST (no cancel-during-first-turn-filler test exists)
- 10 partly / selector is stateless (fillers.py:47) and the store is tenant-keyed (test_tool_latency.py:255); no test runs two calls at once
- 11 yes for tests that pin the old phrase (test_i18n.py:41); the "criteria 1-8" wording is stale
- 12 yes / test_i18n.py:111, :116
- 13 partly / `_` prefix filter in i18n/__init__.py:57 and name check at :25 work; test_i18n.py:90 covers a bad name only; no test drops a `_helper.py` into a package
- 14 yes / test_i18n.py:79-96 (bool, 0, NaN, wrong type, empty); i18n/__init__.py:23-49
- 15 MISMATCH on "missing STRINGS key" (see finding 1); the other cases are covered at test_i18n.py:79-96
- 16 yes / test_i18n.py:130, test_pipeline.py:343
- 17 yes / test_i18n.py:145 (parametrized over every real module), :157
- 18 yes / test_i18n.py:25, :157
- 19 NO TEST (see finding 3)
- 20 NO TEST (see finding 4)

1. [blocking] Criterion 15 says a module with a missing STRINGS key must fail startup, but `_validate` only rejects extra keys (i18n/__init__.py:32-34). Partial languages are deliberate: hi.py is partial, and test_i18n.py:31 plus `t()` rely on the English fallback for a missing key. As written, the criterion would break shipping hi — PRD criteria 14 and 15 contradict criterion 18 and the fallback design. Fix: drop "missing" from criterion 15 and from criterion 14's wording, saying keys must be a subset of en, with missing keys falling back to English.
2. [blocking] No test proves criterion 9 (barge-in during the turn-1 filler), and criterion 10 has no concurrent-calls test. Criterion 11 only demands tests for 1-8, so both gaps pass unnoticed. test_pipeline.py:409-447 covers on_cancel in general, not the filler path. Fix: add a test that cancels after the first filler chunk and asserts no LLM call. Add an `asyncio.gather` test of two handlers with different agents sharing one `FillerSelector` and one store. Change criterion 11 to "criteria 1-10".
3. [minor] Criterion 19 (no scan or import during a call) is untestable as written, and nothing guards it. The code does satisfy it, since `_TABLES` and `_SHORTEST` are built once at import. Fix: add a test that monkeypatches `pkgutil.iter_modules` and `importlib.import_module` to raise, then calls `t`, `tool_fillers` and `shortest_tool_filler`.
4. [minor] Criterion 20 (no es/fr/de/pt/it/ja/zh module) has no test. test_i18n.py:116 only asserts `len(files) >= 2`, and the "fr" case in :25 and :157 is an accidental tripwire that only fires for fr. Criterion 13's `_` helper rule is also undriven. Fix: assert `set(i18n._TABLES).isdisjoint({"es","fr","de","pt","it","ja","zh"})`. Add a `_helper.py` case to `test_valid_language_module_is_discovered` that proves it is skipped.
5. [minor] Scope contradiction: Out says "Changing how tool-call fillers on turns 2+ are chosen" and "Changing ... tool_latency.py". The addendum (criteria 16 and 17) changes tool-call filler language and the error-path phrase, which are turn 2+ behaviour. Open Question 1 also adds `agent_average_ms` to tool_latency.py (a new reader, not a recording change). Open Question 2 is still listed as open although it was resolved ("always speak"). Fix: reword Out to "pool ordering, tiering and min-gap for turns 2+". State that adding a read-only `agent_average_ms` is In scope. Close the open questions.
