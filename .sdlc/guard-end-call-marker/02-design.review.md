# Review: 02-design.md
VERDICT: GREEN

1. [minor] The design drops `।` as a question terminator, which contradicts PRD AC1 as written, so a tester or approver checking AC1 literally will fail a correct build — 02-design.md "Approach" deviation paragraph vs 01-prd.md AC1 — fix: amend 01-prd.md AC1 to list only `?`, `？` and `؟`, and cite the `hi` fallback_goodbye conflict with AC5.
