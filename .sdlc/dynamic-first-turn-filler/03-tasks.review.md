# Review: 03-tasks.md
VERDICT: AMBER

1. [minor] T7's malformed-variant fixture never says to put a valid `en.py` in the `tmp_path` package, so every variant can raise on the "en required" check and pass without exercising the rule it targets; the typo-`STRINGS`-key rule also needs `en.STRINGS` to compare against (lesson 12) — T7 — fix: copy the real `en.py` into each malformed package, and assert that the `ValueError` message names `xx` and the specific failed rule, not just that it raised.
2. [minor] Design Test plan 3a (build with `latency_store=None`, spy on `select_tool_filler`, assert a single call with `average_ms=None` plus the session language and a `_DEFAULT_TARGET_S`-tier phrase) has no task; T3 only asserts "exactly one filler" — T3 — fix: add the spy test to T3, and add its assertions to T3's done-when.
