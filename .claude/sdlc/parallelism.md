# Running stages in parallel

Wall-clock time in this pipeline is dominated by agents waiting on each other, not by
agents thinking. Most of that waiting is unnecessary: the work is independent and was
only serialised because it was written as a list.

**Default to parallel. Serialise only when one of the four blockers below actually
applies.** Dispatch every independent agent in a single message with multiple tool
calls — separate messages run them one after another and buy you nothing.

## The four blockers — serialise only for these

1. **File overlap.** Two agents editing the same file will clobber each other. Split
   work by file, not by topic, and say which files each agent owns.
2. **Shared test database.** Two agents running the suite against the same Postgres
   interleave and produce flaky failures that look like real regressions and cost more
   time than they saved. One test-running agent at a time, or give each its own database.
3. **Shared ports or a running app.** Only one agent at a time may hold :8000/:3000 or
   drive a browser. QA and `/sdlc:run`-style work never runs concurrently with itself.
4. **A real data dependency.** A critic needs the finished artifact. The design needs the
   approved PRD. Phase 3 needs Phase 2's code to exist. These are genuine.

Anything else — "it feels safer in order", "the second one might need the first one's
context" — is not a blocker. Context is handed off through artifacts, which is the whole
point of the file-per-stage design.

## Where the wins actually are

- **Review and security on the same diff.** Both read-only, different lenses, no overlap
  in practice. Run them together, always.
- **Critics split by dimension or stack.** A backend critic and a UI critic on the same
  PR find different defects and neither skims. Split whenever a diff spans two stacks or
  exceeds roughly 800 lines.
- **Build batches with disjoint file sets.** Two implementers on non-overlapping modules
  is safe *if* only one of them runs the test suite, or they run against separate databases.
- **Research and recon during the PRD.** Competitor research and reading our own code are
  independent inputs to the same document.
- **Independent fixes after a review.** Findings in different files go to different agents
  at once, under the same file-ownership rule.

## Fan-in: the part that protects delivery

Parallelism is only free if the join is real. After any parallel batch:

1. **Run the full verification once, on the combined result.** Each agent verified its own
   slice against a tree that did not contain the others' work. That is not the tree you
   are shipping. This has caught real breakage — two independently-green batches whose
   combination was not.
2. **Re-run the critic on the merged state**, not on each part.
3. **Reconcile the reports.** Two agents may disagree, or both may claim a file. Read for
   contradiction, not just for a union of findings.

If you cannot do the combined verification, you cannot run the batch in parallel. The
speed-up is worthless if it moves the integration failure to production.

## Sizing

Four concurrent agents is a sensible ceiling for one batch. Beyond that the fan-in
reconciliation costs more than the parallelism saves, and the reports stop fitting in a
single reading. Prefer two well-scoped agents over five thin ones.
