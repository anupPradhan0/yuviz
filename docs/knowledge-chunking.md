# Knowledge chunking: how the default was chosen

`KNOWLEDGE_CHUNKER` = `auto` (default) | `structure` | `recursive` | `fixed`, read on every ingestion (no restart).

| mode | headed documents and plain prose | Q&A pairs, tables/CSV |
|---|---|---|
| `auto` | **fixed windows** (200 words, 40 overlap, token-capped) | shape routes: one pair per chunk; row groups with the header repeated |
| `structure` | heading-aware sections, breadcrumb, parent rows | same shape routes |
| `recursive` | paragraph/line/sentence/word splitting with parent rows | same shape routes |
| `fixed` | fixed windows | fixed windows (also for Q&A and tables) |

`chunker_version` on the document always records the strategy that actually ran (`fixed-v1`, `structure-v1`,
`recursive-v1`, `qa-v1`, `table-v1`); a splitter failure falls back to fixed windows, logs a warning and records `fixed-v1`.
There is no per-knowledge-base override: it needs a new column on `knowledge_bases` (shared schema), so it was skipped.

## Why fixed windows are the default for prose and headed documents
The rule is that a more complex strategy has to beat the simpler one. It did not. The sweep below runs every knob
that could plausibly explain the gap (child size 150/250/400/600 estimated tokens, sibling-packing threshold,
breadcrumb in the embedded text on/off, parent size, fixed-window size/overlap) against fixed windows on the same golden set.
No structure-aware or recursive configuration is ahead of fixed windows on policy documents on both recall@5 and MRR.
The structure default returns less context (about 543 tokens per query against 854) but is less accurate; the
configurations that tie or come closest (recursive child=400/600, structure child=600) return 1255 to 1598 tokens, so a bigger
context is bought, not won. Fixed windows with no overlap (200w/0) scored slightly above the legacy 200w/40 (MRR 0.82, in-context 0.92 at
796 tokens); that is one or two questions on a 24-question set, so the legacy 40-word overlap is kept and the point is left for the real-model run. Q&A documents: same accuracy and about a fifth of the returned context, so the Q&A route stays.
Tables: every strategy scores 1.00 on four questions, so the table route is kept for a design reason (fixed windows collapse
whitespace, cut rows and lose the header), not a measured win.

## Corpus and method (provisional)
39 questions over 48 documents: 9 hand-written (3 policies, a warranty handbook with text before its first heading, a
headings-only outline, an FAQ, a delivery-rate CSV, two prose notes) and 39 seeded template-generated distractors of the same
four shapes. Questions per kind: policy 24, prose 5, FAQ 5, table 4, outline 1. One index per document kind (a strategy is chosen per
document type), chunk-level recall@5 and MRR, and the context production returns: when a child matches, its parent section
(Phase 5 expands via `parent_id`), top 3 contexts, identical ones counted once; `ctx tok` is the average estimated tokens
returned per query. Columns: recall@5 / MRR / answer-in-context / ctx tok.

**Caveat: the embedder is a deterministic hash of words and character trigrams.** It rewards word overlap, has no
semantic similarity, and its scores approximate nothing about the real embedding model. Small chunks rank worse under it, which
favours larger windows. The conclusion is provisional: the default must be re-chosen on the real model and real tenant questions
(issue #86's exit criterion "strategy chosen by a real-model bake-off" is still open). `sweep.py` reproduces the table:
`python -m services.knowledge.tests.eval.sweep`; `test_bakeoff.py` pins the numbers with tolerances and fails if any
structure/recursive configuration starts to beat fixed windows on policy documents.

| strategy | policy R@5 / MRR / ctx / tok | prose R@5 / MRR / ctx / tok | faq R@5 / MRR / ctx / tok | table R@5 / MRR / ctx / tok | mixed KB R@5 / MRR |
|---|---|---|---|---|---|
| fixed 200w/40 | 0.92 / 0.79 / 0.83 / 854 | 1.00 / 0.70 / 1.00 / 778 | 1.00 / 1.00 / 1.00 / 787 | 1.00 / 1.00 / 1.00 / 428 | 0.72 / 0.63 |
| fixed 120w/20 | 0.88 / 0.71 / 0.79 / 489 | 0.80 / 0.65 / 0.60 / 426 | 1.00 / 1.00 / 1.00 / 541 | 1.00 / 1.00 / 1.00 / 428 | 0.74 / 0.56 |
| fixed 300w/60 | 0.92 / 0.83 / 0.92 / 1115 | 1.00 / 0.70 / 1.00 / 739 | 1.00 / 0.87 / 1.00 / 949 | 1.00 / 1.00 / 1.00 / 428 | 0.77 / 0.62 |
| fixed 200w/0 | 0.92 / 0.82 / 0.92 / 796 | 1.00 / 0.70 / 1.00 / 725 | 1.00 / 1.00 / 1.00 / 853 | 1.00 / 1.00 / 1.00 / 428 | 0.79 / 0.63 |
| fixed 400w/80 | 0.96 / 0.84 / 0.92 / 1513 | 1.00 / 0.70 / 1.00 / 1054 | 1.00 / 0.87 / 1.00 / 949 | 1.00 / 1.00 / 1.00 / 428 | 0.77 / 0.62 |
| recursive child=150 | 0.79 / 0.66 / 0.83 / 1209 | 1.00 / 0.77 / 1.00 / 722 | 1.00 / 1.00 / 1.00 / 638 | 1.00 / 1.00 / 1.00 / 532 | 0.67 / 0.54 |
| recursive child=250 | 0.83 / 0.72 / 0.88 / 1298 | 0.80 / 0.64 / 1.00 / 942 | 1.00 / 0.90 / 1.00 / 760 | 1.00 / 1.00 / 1.00 / 532 | 0.64 / 0.56 |
| recursive child=400 | 0.92 / 0.82 / 0.92 / 1380 | 1.00 / 0.70 / 1.00 / 1057 | 1.00 / 0.87 / 1.00 / 949 | 1.00 / 1.00 / 1.00 / 532 | 0.82 / 0.61 |
| recursive child=600 | 0.92 / 0.83 / 0.92 / 1598 | 1.00 / 0.70 / 1.00 / 1054 | 1.00 / 0.87 / 1.00 / 949 | 1.00 / 1.00 / 1.00 / 532 | 0.77 / 0.60 |
| structure child=150 crumb=on | 0.75 / 0.65 / 0.79 / 377 | 1.00 / 0.77 / 1.00 / 722 | 1.00 / 0.90 / 1.00 / 635 | 1.00 / 1.00 / 1.00 / 532 | 0.69 / 0.58 |
| structure child=150 crumb=off | 0.71 / 0.63 / 0.71 / 359 | 1.00 / 0.77 / 1.00 / 722 | 1.00 / 1.00 / 1.00 / 689 | 1.00 / 1.00 / 1.00 / 532 | 0.62 / 0.56 |
| structure child=250 crumb=on | 0.79 / 0.70 / 0.75 / 543 | 0.80 / 0.64 / 1.00 / 942 | 1.00 / 0.70 / 1.00 / 756 | 1.00 / 1.00 / 1.00 / 532 | 0.59 / 0.52 |
| structure child=250 crumb=off | 0.83 / 0.70 / 0.79 / 510 | 0.80 / 0.64 / 1.00 / 942 | 0.80 / 0.70 / 1.00 / 810 | 1.00 / 1.00 / 1.00 / 532 | 0.64 / 0.55 |
| structure child=400 crumb=on | 0.83 / 0.76 / 0.79 / 778 | 1.00 / 0.70 / 1.00 / 1057 | 1.00 / 0.87 / 1.00 / 945 | 1.00 / 1.00 / 1.00 / 532 | 0.72 / 0.55 |
| structure child=400 crumb=off | 0.88 / 0.74 / 0.83 / 794 | 1.00 / 0.70 / 1.00 / 1057 | 1.00 / 0.90 / 1.00 / 934 | 1.00 / 1.00 / 1.00 / 532 | 0.74 / 0.56 |
| structure child=600 crumb=on | 0.88 / 0.77 / 0.83 / 1255 | 1.00 / 0.70 / 1.00 / 1054 | 1.00 / 0.87 / 1.00 / 945 | 1.00 / 1.00 / 1.00 / 532 | 0.77 / 0.55 |
| structure child=600 crumb=off | 0.88 / 0.70 / 0.83 / 1185 | 1.00 / 0.70 / 1.00 / 1054 | 1.00 / 0.90 / 1.00 / 934 | 1.00 / 1.00 / 1.00 / 532 | 0.72 / 0.55 |
| structure pack=125 | 0.75 / 0.67 / 0.75 / 274 | 0.80 / 0.64 / 1.00 / 942 | 1.00 / 0.70 / 1.00 / 756 | 1.00 / 1.00 / 1.00 / 532 | 0.64 / 0.56 |
| structure pack=250 | 0.79 / 0.70 / 0.75 / 543 | 0.80 / 0.64 / 1.00 / 942 | 1.00 / 0.70 / 1.00 / 756 | 1.00 / 1.00 / 1.00 / 532 | 0.59 / 0.52 |
| structure pack=500 | 0.79 / 0.70 / 0.79 / 958 | 0.80 / 0.64 / 1.00 / 942 | 1.00 / 0.70 / 1.00 / 756 | 1.00 / 1.00 / 1.00 / 532 | 0.62 / 0.53 |
| structure parent_max=600 | 0.79 / 0.70 / 0.75 / 543 | 0.80 / 0.64 / 1.00 / 942 | 1.00 / 0.70 / 1.00 / 756 | 1.00 / 1.00 / 1.00 / 532 | 0.59 / 0.52 |
| structure parent_max=900 | 0.79 / 0.70 / 0.75 / 543 | 0.80 / 0.64 / 1.00 / 942 | 1.00 / 0.70 / 1.00 / 756 | 1.00 / 1.00 / 1.00 / 532 | 0.59 / 0.52 |
| structure parent_max=1400 | 0.79 / 0.70 / 0.75 / 543 | 0.80 / 0.64 / 1.00 / 942 | 1.00 / 0.70 / 1.00 / 756 | 1.00 / 1.00 / 1.00 / 532 | 0.59 / 0.52 |
| structure child-only (no parent) | 0.79 / 0.70 / 0.75 / 484 | 0.80 / 0.64 / 0.60 / 449 | 1.00 / 0.70 / 1.00 / 571 | 1.00 / 1.00 / 1.00 / 532 | 0.59 / 0.52 |
| recursive child-only (no parent) | 0.83 / 0.72 / 0.75 / 565 | 0.80 / 0.64 / 0.60 / 449 | 1.00 / 0.90 / 1.00 / 570 | 1.00 / 1.00 / 1.00 / 532 | 0.64 / 0.56 |
| KNOWLEDGE_CHUNKER=auto | 0.92 / 0.79 / 0.83 / 854 | 1.00 / 0.70 / 1.00 / 778 | 1.00 / 1.00 / 1.00 / 147 | 1.00 / 1.00 / 1.00 / 532 | 0.72 / 0.61 |
| KNOWLEDGE_CHUNKER=structure | 0.79 / 0.70 / 0.75 / 543 | 0.80 / 0.64 / 1.00 / 942 | 1.00 / 1.00 / 1.00 / 147 | 1.00 / 1.00 / 1.00 / 532 | 0.62 / 0.55 |
| KNOWLEDGE_CHUNKER=recursive | 0.83 / 0.72 / 0.88 / 1298 | 0.80 / 0.64 / 1.00 / 942 | 1.00 / 1.00 / 1.00 / 147 | 1.00 / 1.00 / 1.00 / 532 | 0.67 / 0.59 |
| KNOWLEDGE_CHUNKER=fixed | 0.92 / 0.79 / 0.83 / 854 | 1.00 / 0.70 / 1.00 / 778 | 1.00 / 1.00 / 1.00 / 787 | 1.00 / 1.00 / 1.00 / 532 | 0.72 / 0.63 |

## Applying a strategy change to existing documents
Documents keep the chunks they were indexed with until re-ingested. `python -m services.knowledge.reingest --tenant <uuid>
[--kb <uuid>] [--except-version fixed-v1] [--dry-run]` bumps each ready or ready_degraded document's version and queues a normal ingestion job
(skipping documents that already have a pending or running job); the worker swaps the chunks in one transaction, so
retrieval keeps the old chunks until the new ones commit. It runs on a tenant-scoped connection (reason `kb-ingestion-job`, no
`platform_conn`), one tenant per call. The console chunk count reads 0 for a document between the version bump and the swap.
Failed documents are not re-queued (use the document retry).

## Limits that protect the worker
Chunking runs off the event loop (`asyncio.to_thread`), so the lease heartbeat keeps running, and carries its own limits:
`KNOWLEDGE_CHUNK_BUDGET_S` (default 60 s wall clock, checked inside every loop; exceeding it fails the document permanently with
"document too complex to index") and `KNOWLEDGE_MAX_CHUNKS_PER_DOC` (default 5,000; "document too large to index (more than N chunks)"),
both enforced before anything is embedded. The shipped ceiling rejects very large prose: 5 MB of ordinary text is about 4,300 fixed windows
(8,300 structure chunks), so a bigger ceiling has to be set deliberately. Vectors are held as float32 arrays while embedding, so memory is
bounded by chunks x 768 x 4 bytes (5,000 chunks = 15 MB). The windowing is linear: a 10 MB single-line document chunks in a few seconds.

## Re-ingest behaviour
A document that is already `ready` or `ready_degraded` keeps its status and keeps serving its old chunks while it re-ingests and through a failed,
retried, dead-lettered or lease-reaped re-ingest; the new chunks replace the old ones only in the single-transaction swap. A failed re-ingest records
`re-index failed: <reason>` in `kb_documents.error` (and in the job and its events); the console shows it on the ready document and offers Retry, and
`python -m services.knowledge.reingest` queues it again. First-time ingests keep pending -> processing -> ready/failed. Re-ingest jobs run behind all normal
jobs, in batches (`--batch`, default 100), one run per tenant at a time (advisory lock).
