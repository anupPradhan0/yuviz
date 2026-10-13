# Knowledge phase 7: precision and scale

Three optional features. **Every one is off by default and every one that calls an external service needs an explicit
operator or tenant setting.** None logs a caller query or document text. Connectors are deferred
(`knowledge-connectors-deferred.md`).

## What is unproven

- **Reranking improves retrieval: not established.** The golden-set tests use a deterministic fake cross-encoder and a
  fake embedder. They prove the plumbing (flag, hook, reorder, latency accounting, fail-open), not quality. Measure
  with a real provider and real tenant questions before turning it on.
- **Contextual enrichment improves retrieval: not established, and the "keep it only if the golden set shows a win"
  rule is not met.** It needs a real embedder and a real LLM. It ships off.
- The p95-with-rerank-inside-400-ms criterion was not measured against a real provider. The deadline logic is tested
  with injected slow/failing providers; the real network round trip is unmeasured.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `KNOWLEDGE_RERANKER` | `none` | `none` \| `cohere` \| `voyage`. Unknown value = none (logged). Read once per process. |
| `KNOWLEDGE_RERANK_API_KEY_REF` | `env:COHERE_API_KEY` / `env:VOYAGE_API_KEY` | Secret ref (`env:`, `k8s:`, `enc:`), resolved once like an embedding `api_key_ref`. Platform key, not a tenant's. |
| `KNOWLEDGE_RERANK_MODEL` | `rerank-v3.5` / `rerank-2` | Provider model name. |
| `KNOWLEDGE_RERANK_URL` | provider default | Override the endpoint (regional/proxy). |
| `KNOWLEDGE_RERANK_TOP_N` | `20` | Candidates sent. The rest keep fused order behind them. |
| `KNOWLEDGE_RERANK_DOC_CHARS` | `1200` | Per-candidate text cap. |
| `KNOWLEDGE_RERANK_MAX_PAYLOAD_CHARS` | `20000` | Total candidate text per call. |
| `KNOWLEDGE_RERANK_HTTP_TIMEOUT_MS` | `95` | Adapter timeout, under the stage budget. |
| `KNOWLEDGE_RERANK_TIMEOUT_MS` | `120` | Existing stage budget in `retrieve()`; unchanged. |
| `KNOWLEDGE_RERANK_DECISIVE_SCORE` | `0.85` | See "Decisive skip". |
| `KNOWLEDGE_RERANK_DECISIVE_GAP` | `0.10` | See "Decisive skip". |
| `KNOWLEDGE_ENRICH_CHUNKS` | `0` | `1` turns contextual enrichment on. |
| `KNOWLEDGE_ENRICH_TIMEOUT_S` | `8` | One LLM call. |
| `KNOWLEDGE_ENRICH_CONCURRENCY` | `4` | Parallel LLM calls per document. |
| `KNOWLEDGE_ENRICH_MAX_CHUNKS_PER_DOC` | `100` | Chunks past this are embedded plain. |
| `KNOWLEDGE_ENRICH_MAX_TOKENS_PER_DOC` | `60000` | Estimated prompt + output tokens per document. |
| `KNOWLEDGE_ENRICH_DOC_DEADLINE_S` | `90` | Wall-clock for one document's enrichment. |

## Data each feature sends, and where

| Feature | Sent | To | When |
|---|---|---|---|
| Reranker | The caller's query (max 1000 chars) and the text of up to `TOP_N` candidate chunks (each cut to `DOC_CHARS`, all to `MAX_PAYLOAD_CHARS`). No ids, titles, tenant or agent names. | Cohere or Voyage, with the platform key | Only if `KNOWLEDGE_RERANKER` is set **and** the agent's policy `rerank` is true (or the call asks for it) **and** the top hit is not decisive |
| Enrichment | Per enriched chunk: document title, section heading, chunk text, up to 300 chars of the previous and next chunk. | The tenant's own default LLM provider (`tenants.default_llm_config_id`, engine openai/groq/nvidia/cohere), with the tenant's own key | Only if `KNOWLEDGE_ENRICH_CHUNKS=1`, at ingestion, and the tenant has such a config. Anthropic, Gemini and Ollama engines are skipped (a local 3B model fabricates; the other two are not OpenAI-compatible) |
| Re-embed | Stored chunk text | The target embedding config's own provider (same as ingestion) | Only when an operator runs the CLI |

Nothing logs query or document text. Logs carry fixed reason codes, counts and exception class names only. Provider
error bodies are never stored or logged. A key ref that fails to resolve is never echoed.

## Reranker

`retrieve()` already had the hook, a 120 ms stage budget, a breaker and a 400 ms deadline. Phase 7 adds the adapters
(`services/knowledge/reranker.py`) and wires them into the router (`runtime.get_reranker()`).

- Select with `KNOWLEDGE_RERANKER`. Per agent, the existing policy flag `rerank` decides whether a call reranks. Flag
  true with no reranker configured is skipped with `degraded: ['rerank_skipped']` (unchanged).
- **Decisive skip.** The provider is not called when the top fused hit was found by both dense and lexical search,
  has dense cosine >= `DECISIVE_SCORE` (0.85) and leads the runner-up by >= `DECISIVE_GAP` (0.10). Reordering a clear
  winner is unlikely to help and costs a network call. Counted in `rerank_decisive_skip` (`/internal/ops`). Tune from
  measured data; there is no evidence yet for these numbers.
- **Failure.** Timeout, 429, 5xx, a bad key, a malformed body, a wrong result count, a duplicate or out-of-range index:
  the fused order is kept and `degraded` gets `rerank_skipped` plus one of `rerank_timeout`, `rerank_rate_limited`,
  `rerank_provider_error`, `rerank_bad_response`, `rerank_not_configured` (or `rerank_error`). There are no retries.
  Five consecutive failures open the shared `rerank` breaker (existing settings); while open the provider is not called.
- The breaker key `rerank` carries no tenant data and there is no rerank cache (so no tenant-keyed cache to leak).
- Returned scores are unchanged (still dense cosine / lexical), so `low_confidence` and `minimum_score` keep their
  meaning; only the order changes.

## Contextual enrichment

`services/knowledge/enrichment.py`, called from the ingestion worker's embed step. The sentence is put in front of the
text that is **embedded**; stored `content`, the `tsv` column, citations and parent sections are the original chunk.
Fail-open: no config, unsupported engine, timeout, HTTP error, garbage output, a cap hit or any exception embeds that
chunk plain and is counted in the embed event's detail (`enrichment`, `enriched`, `failed`, `skipped_cap`,
`skipped_too_long`, `enrich_reason`). Caps: chunks per document, estimated tokens per document (reserved before the
call), wall-clock per document, consecutive failures (5 stops the calls for that document), per-call timeout,
bounded concurrency, and a chunk whose token count could push it past the embedder's `max_tokens` is left plain.

The document is untrusted input to the LLM: fenced in `<doc>` with angle brackets removed from the data, the model
gets no tools, and its output is cut to one plain line (first line only, tags, URLs, markup and control characters
removed, 240 chars max) before it can touch an embedding. The worst a hostile document can do is skew its own chunk's
vector. Re-embedding (below) embeds stored text and does **not** re-apply enrichment; re-ingest the documents for that.

## Re-embed a knowledge base (runbook)

Migrates a KB to another embedding config without downtime. Schema: `kb_chunks.embedding_next VECTOR(768)` and
`embedding_next_model TEXT` (additive, idempotent, no index).

```
python -m services.knowledge.reembed --tenant <uuid> --kb <uuid> --to-config <uuid> --dry-run   # counts only, no provider call
python -m services.knowledge.reembed --tenant <uuid> --kb <uuid> --to-config <uuid> [--batch 64]
python -m services.knowledge.reembed --tenant <uuid> --kb <uuid> --abandon                      # drop a half-done migration
```

1. Create the new `provider_configs` row (role `embedding`, **768 dimensions**) for the tenant first.
2. Dry run. Then run for real. It embeds every non-parent chunk in bounded batches into `embedding_next` with the new
   config, retrying a failed batch like the worker does, validating count, 768 dims, finite and non-zero.
3. Queries keep using the old `embedding` and the old config the whole time.
4. When every chunk has a shadow vector made by the target model, one transaction (holding the KB row lock) copies
   `embedding_next` into `embedding`, sets `embedding_model`, points the KB at the new config, clears the shadow and
   writes an audit row. Readers see all-old or all-new.
5. Resumable: re-run the same command after any interruption. Switching `--to-config` re-embeds what the earlier
   target had done. Exit code 0 = swapped (or dry run / already on target), 1 = stopped or incomplete.
6. After a large KB swap, run `REINDEX INDEX CONCURRENTLY idx_kb_chunks_embedding_hnsw` in a quiet period: the HNSW
   index stays correct through the update but accumulates dead entries until vacuum/reindex.

**Chunks added or changed while it runs.** An upload or re-ingest during the migration is embedded by the worker with
the KB's *old* config (the KB row still points at it) and so has no shadow vector. The swap re-checks under the KB row
lock and refuses if any chunk is left (the CLI embeds the stragglers and retries, up to 5 rounds). The worker's index
step takes `FOR SHARE` on the KB row and refuses to commit vectors made with a config the KB no longer has (the job
is retried with the new config), so a worker that was mid-embed when the swap committed cannot land old-space vectors.

**Known limits.** (a) The retrieval plan cache is per process: the CLI invalidates its own process only, so the
service learns of the swap within `KNOWLEDGE_PLAN_TTL_S` (30 s default). In that window a stale plan embeds queries
with the old model against new-space vectors: dense hits mostly fall under `minimum_score`, lexical search still
answers. Run migrations off-peak, or restart the knowledge service after the swap to remove the window. (b) The swap
is one transaction over every chunk of the KB: on a very large KB it holds row locks for its duration, and an
ingest of that KB waits for it. (c) Run one migration per KB at a time. Two concurrent runs with different targets
cannot produce a mixed swap (the swap checks its own target) but will redo each other's work. (d) Same-named models on
different engines are treated as the same vector space.

## Evaluating rerank and enrichment properly

Do not enable either on the strength of the fake-based tests. With real credentials, against a scratch database:

```
POSTGRES_DSN=postgresql://.../scratchdb OPENAI_API_KEY=... KNOWLEDGE_RERANKER=cohere COHERE_API_KEY=... \
  python -m services.knowledge.tests.eval.real_eval --allow-external-calls --rerank --enrich
```

It prints recall@5, MRR and answer-in-context for baseline, +rerank (with the rerank stage's p50/p95) and +enrichment.
It refuses without `--allow-external-calls`. The golden set is 39 synthetic questions: differences of a few points are
noise. Then repeat with a sample of the tenant's real questions and documents (what the tenant approves sending out),
and check p95 with rerank on stays inside the 400 ms deadline from `retrieval_logs.timings_ms`.
Plumbing tests (no network): `pytest services/knowledge/tests/eval/test_golden_gates.py -k plumbing`.
