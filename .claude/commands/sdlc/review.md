---
description: Adversarially review the implementation — critics and the security audit run in parallel
allowed-tools: Bash, Read, Agent
---

Slug: `cat .sdlc/current`. Feature dir: `.sdlc/<slug>/`.

**Read `.claude/sdlc/parallelism.md` first.** This stage is the biggest wall-clock win in the pipeline: the code critics and the security audit all read the same diff, none of them writes code, and none depends on another's output. They must be dispatched **in a single message with multiple tool calls**.

1. Size the diff: `git status --short` and `git diff --stat` (some files may be untracked — `git diff` alone misses them).

2. **Dispatch in one message, in parallel:**
   - `sdlc-code-critic` — one agent if the diff is small and single-stack. **Split into two or more when the diff spans stacks** (Python vs `admin-ui/`) or exceeds ~800 lines: give each critic its own file scope, tell it another critic covers the rest, and have each write to its own findings file (`05-review-<scope>.md`). A single critic across two stacks skims one of them.
   - `sdlc-security` in `code` mode — always, concurrently, writing `05-security.md`. Tell it the code critics are running so it does not re-derive their ground; it should go deep on tenant isolation, privilege escalation, credential handling and anything reachable unauthenticated.

3. **Reconcile before acting.** Read the reports together: two agents may name the same defect (strong signal — say so), disagree on severity, or contradict each other. Resolve the contradiction rather than reporting both.

4. **Fix in parallel too, under file ownership.** Group blocking findings by file and dispatch one implementer per disjoint group in a single message. Two agents must never own the same file. Only one of them runs the test suite (blocker 2) — tell the others to report failures in files they do not own rather than fixing them.

5. **Fan in.** Run the full suite yourself once on the combined tree — each agent verified against a tree that lacked the others' changes. Then re-run the critics on the merged state, not on each part. Max 2 rounds; 3 for security.

6. Print only:

```
Verdict: <verdict> after <n> round(s)
Fixed: <one line per fixed finding>
Open: <one line per remaining finding, or None>
Security: <verdict> — <n> critical, <n> high, <n> medium, <n> low
Controls verified: <n>
```

7. If a blocking or critical finding is a class of mistake that will recur, run the `/sdlc:retro` steps and mention which lessons you added.

Minor findings are reported, not fixed, unless I ask.
