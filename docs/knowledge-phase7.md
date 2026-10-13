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

Every `KNOWLEDGE_RERANK_*` and `KNOWLEDGE_ENRICH_*` number is parsed defensively: empty, text, `inf`, `nan`, negative or
out-of-range values fall back to the default below and log one warning (never the value). A malformed value cannot
take retrieval or ingestion down.

| Variable | Default | Meaning |
|---|---|---|
| `KNOWLEDGE_RERANKER` | `none` | `none` \| `cohere` \| `voyage`. Unknown value = none (logged). Read once per process. |
| `KNOWLEDGE_RERANK_API_KEY_REF` | `env:COHERE_API_KEY` / `env:VOYAGE_API_KEY` | Secret ref (`env:`, `k8s:`, `enc:`), resolved once like an embedding `api_key_ref`. Platform key, not a tenant's. |
| `KNOWLEDGE_RERANK_MODEL` | `rerank-v3.5` / `rerank-2` | Provider model name. |
| `KNOWLEDGE_RERANK_URL` | provider default | Override the endpoint. Must be `https` and its host must be the vendor's or listed in `KNOWLEDGE_RERANK_ALLOWED_HOSTS`; otherwise the service REFUSES TO START (the key goes in a header). |
| `KNOWLEDGE_RERANK_ALLOWED_HOSTS` | none | Comma-separated extra hosts allowed for the URL. |
| `KNOWLEDGE_RERANK_PLATFORM_RATE` | `10` | Calls per second the platform rerank key may make in total (your vendor quota, below its real limit). |
| `KNOWLEDGE_RERANK_PROCESSES` | `4` | Knowledge-service processes sharing that quota. **Set this to the real count.** |
| `KNOWLEDGE_RERANK_TENANT_SHARE` | `0.25` | The share of the quota one tenant may use. |
| `KNOWLEDGE_RERANK_TENANT_RATE` / `_BURST` | derived / `5` | Per-tenant calls per second per process. Default = platform rate x tenant share / processes (0.625 with the defaults); an explicit value overrides. Over budget = skipped with `rerank_budget`. |
| `KNOWLEDGE_RERANK_VALVE_TENANTS` / `_WINDOW_S` / `_COOLDOWN_S` | `3` / `30` / `20` | The global safety valve: opens only when this many DISTINCT tenants fail within the window. |
| `KNOWLEDGE_RERANK_TOP_N` | `20` | Candidates sent. The rest keep fused order behind them. |
| `KNOWLEDGE_RERANK_DOC_CHARS` | `1200` | Per-candidate text cap. |
| `KNOWLEDGE_RERANK_MAX_PAYLOAD_CHARS` | `20000` | Total candidate text per call. |
| `KNOWLEDGE_RERANK_HTTP_TIMEOUT_MS` | `95` | Adapter timeout, under the stage budget. |
| `KNOWLEDGE_RERANK_TIMEOUT_MS` | `120` | Existing stage budget in `retrieve()`; unchanged. |
| `KNOWLEDGE_RERANK_DECISIVE_SCORE` | `0.85` | See "Decisive skip". |
| `KNOWLEDGE_RERANK_DECISIVE_GAP` | `0.10` | See "Decisive skip". |
| `KNOWLEDGE_ENRICH_CHUNKS` | `0` | `1` is the platform switch. It enables NO tenant on its own. |
| `KNOWLEDGE_ENRICH_TENANTS` | empty | Tenant ids or slugs (comma separated) that may use it. Empty = nobody; there is no wildcard. |
| `KNOWLEDGE_ENRICH_MAX_CALLS_PER_TENANT_DAY` | `2000` | LLM calls per tenant per UTC day, in-process (each worker process counts for itself and a restart resets it, so the ceiling is the cap times the processes). |
| `KNOWLEDGE_REEMBED_MAX_SWAP_CHUNKS` | `20000` | A swap over this many chunks needs `--allow-long-swap`. |
| `KNOWLEDGE_ENRICH_TIMEOUT_S` | `8` | One LLM call. |
| `KNOWLEDGE_ENRICH_CONCURRENCY` | `4` | Parallel LLM calls per document. |
| `KNOWLEDGE_ENRICH_MAX_CHUNKS_PER_DOC` | `100` | Chunks past this are embedded plain. |
| `KNOWLEDGE_ENRICH_MAX_TOKENS_PER_DOC` | `60000` | Estimated prompt + output tokens per document. |
| `KNOWLEDGE_ENRICH_DOC_DEADLINE_S` | `90` | Wall-clock for one document's enrichment. |
| `KNOWLEDGE_PROVIDER_ALLOWED_HOSTS` | the operator's Ollama only | Comma-separated `host` or `host:port` entries a tenant's embedding `extra.base_url` may point at even though it is private. See "Provider URLs". Set on the Config service AND the Knowledge service. |

## Provider URLs (tenant-supplied embedding endpoints)

A tenant admin can set `extra.base_url` on an `embedding` provider config (engine `ollama`). It is treated as an
origin, never as a URL to fetch (issue #97):

- Only scheme, host and port are used. The API path (`/api/embeddings`) is fixed in code. A `base_url` with a path,
  query, fragment, credentials, a non-http(s) scheme, an empty host or an odd IP spelling (`2130706433`, `0x7f.0.0.1`,
  `127.1`, `0177.0.0.1`, IPv4-mapped IPv6) is refused with a 400 when the config is created or edited, and again when
  the Knowledge service builds the client (a row written before this check still cannot be used).
- Every address the host resolves to must be public. Loopback, link-local (169.254.0.0/16 including cloud metadata,
  fe80::/10), private (10/8, 172.16/12, 192.168/16, fc00::/7), CGNAT 100.64/10, multicast, reserved, unspecified,
  IPv4-mapped IPv6 and the 6to4/Teredo/NAT64 forms are refused. The resolution happens once, inside the connect, and the
  connection goes to the validated address (Host header and TLS SNI still use the hostname), so DNS rebinding cannot
  swap the address. Redirects are not followed and proxy environment variables are ignored.
- The allow-list `KNOWLEDGE_PROVIDER_ALLOWED_HOSTS` (comma-separated `host` or `host:port`) names endpoints that are
  allowed although private. **Unset**, it holds only the operator's own Ollama: the host:port of `OLLAMA_BASE_URL`, or
  `localhost:11434` (the code's default), plus `127.0.0.1` on the same port when the host is `localhost`; so local
  development needs no extra configuration for exactly that endpoint. **Set**, it replaces that default (an empty value
  allows nothing). An entry without a port allows every port on that host; prefer `host:port`.
  The Config service reads the same variable at write time, so set it on both services.
- Failures are stored and returned as fixed codes, never exception text: `provider_unreachable` (refused, timeout,
  DNS, TLS and the guard's own refusal are all this one code, so a port's state cannot be read back), `provider_auth_failed`
  (401/403), `provider_rate_limited` (429), `provider_error` (any other non-2xx, including a redirect),
  `provider_bad_response` (invalid JSON or shape), `provider_url_invalid` (the URL fails the syntax rules above). They
  appear in `documents.error`, the job and its events, the health endpoint and document events. Retry behaviour is
  unchanged (4xx other than 408/425/429 are permanent) but is decided from the structured exception.
- Rows written before this change may still hold old error text; it is re-scrubbed on read, not rewritten.
- Not covered: STT, LLM and TTS adapters in the Conversation service and custom tool URLs still take tenant URLs (tracked
  in issue #97). Services also still connect to Postgres with a superuser DSN.

## Data each feature sends, and where

| Feature | Sent | To | When |
|---|---|---|---|
| Reranker | The caller's query (max 1000 chars) and the text of up to `TOP_N` candidate chunks (each cut to `DOC_CHARS`, all to `MAX_PAYLOAD_CHARS`). No ids, titles, tenant or agent names. | Cohere or Voyage, with ONE PLATFORM-WIDE KEY shared by every tenant (not a tenant's own key) | Only if `KNOWLEDGE_RERANKER` is set **and** the agent's retrieval policy has `rerank` true **and** the call does not turn it off **and** the top hit is not decisive **and** the tenant is within its call budget. A request can never switch rerank on |
| Enrichment | Per enriched chunk: document title, section heading, chunk text, up to 300 chars of the previous and next chunk. | The tenant's own default LLM provider (`tenants.default_llm_config_id`, which must be owned by that tenant; engine openai/groq/nvidia/cohere), with the tenant's own key | Only if `KNOWLEDGE_ENRICH_CHUNKS=1` **and** the tenant is in `KNOWLEDGE_ENRICH_TENANTS`, at ingestion, on the job's FIRST attempt only (a retry embeds plain), within the per-tenant daily call cap, and the tenant has such a config. Anthropic, Gemini and Ollama engines are skipped (a local 3B model fabricates; the other two are not OpenAI-compatible) |
| Re-embed | Stored chunk text | The target embedding config's own provider (same as ingestion) | Only when an operator runs the CLI |

Nothing logs query or document text. Logs carry fixed reason codes, counts and exception class names only. Provider
error bodies are never stored or logged. A key ref that fails to resolve is never echoed.

## Reranker

`retrieve()` already had the hook, a 120 ms stage budget, a breaker and a 400 ms deadline. Phase 7 adds the adapters
(`services/knowledge/reranker.py`) and wires them into the router (`runtime.get_reranker()`, built once per process;
a construction failure never fails a retrieval, it reports `rerank_not_configured`).

- **The provider key is platform-wide.** One key, one vendor quota, shared by every tenant whose agent has the flag.
  Three layers protect it. (1) Each tenant has its own call budget (token bucket). (2) The whole process is capped at
  its share of the quota (platform rate / processes), so no mix of tenants can push the shared key into 429 for
  everyone; both over-budget cases skip with `rerank_skipped` + `rerank_budget`. (3) Breakers: each tenant has its OWN
  breaker (`rerank:<tenant id>`, in a bounded table that evicts closed breakers first), so a tenant whose own calls time
  out (a pathological agent, long documents) opens only its own. A separate global valve opens only when 3 distinct
  tenants fail within 30 s (a platform-wide vendor outage), lets one probe through after 20 s, and closes on a success.
  Quota 429s and budget refusals count toward no breaker; timeouts, 5xx and transport errors do.
  **Sizing:** set `KNOWLEDGE_RERANK_PLATFORM_RATE` to your vendor quota in calls/s (leave headroom),
  `KNOWLEDGE_RERANK_PROCESSES` to the number of knowledge-service processes, and `KNOWLEDGE_RERANK_TENANT_SHARE` to the
  largest share one tenant may take. The budgets are per process, so they are an upper bound only as good as the
  process count you declare.
- **Who can switch it on.** Only the agent's retrieval policy (`rerank: true`). A request's `rerank` value can turn it
  OFF for that call, never ON: a viewer sending `rerank: true` to an agent whose flag is off reaches no provider.
  With `KNOWLEDGE_RERANKER=none` (default) a flagged agent gets `degraded: ['rerank_skipped']` as before.
- **Decisive skip.** The provider is not called when the top fused hit has dense support, a dense cosine >=
  `DECISIVE_SCORE` (0.85), and leads the best OTHER dense candidate anywhere in the list by >= `DECISIVE_GAP` (0.10).
  Lexical-only candidates are never compared on their score (another scale) and a result with no dense support is never
  decisive. Skips do not spend the tenant's budget. Counted in `rerank_decisive_skip` (`/internal/ops`). There is no
  evidence for these numbers yet; tune from measured data.
- **Failure.** Timeout, 429, 5xx, a bad key, a malformed body, a wrong result count, a duplicate or out-of-range
  index: the fused order is kept and `degraded` gets `rerank_skipped` plus one of `rerank_timeout`,
  `rerank_rate_limited`, `rerank_budget`, `rerank_provider_error`, `rerank_bad_response`, `rerank_not_configured` (or
  `rerank_error`). No retries. Five consecutive health failures open the shared `rerank` breaker.
- **Warm connection.** The HTTP client is created once with keep-alive (120 s), and the service opens the connection at
  startup with a body-less HEAD to the vendor origin (no key, no text). Measured against a local TLS fake with no
  network round trip: handshake about 6 ms, a cold first call about 3 ms, warm calls about 1 ms, and three calls reused
  one connection. A real network adds round trips that are NOT measured here; a connection the vendor closed while idle
  makes the next call pay the handshake, and if that exceeds the 95 ms budget the call is dropped (fused order kept).
- Returned scores are unchanged (still dense cosine / lexical), so `low_confidence` and `minimum_score` keep their
  meaning; only the order changes. There is no rerank cache.

## Contextual enrichment

`services/knowledge/enrichment.py`, called from the ingestion worker's embed step. The sentence is put in front of the
text that is **embedded**; stored `content`, the `tsv` column, citations and parent sections are the original chunk.
Opt-in is per tenant (`KNOWLEDGE_ENRICH_TENANTS`), the LLM config is looked up with an explicit
`provider_configs.tenant_id = <the document's tenant>` predicate, and only the first attempt of a job enriches.
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
python -m services.knowledge.reembed --tenant <uuid> --kb <uuid> --to-config <uuid> [--batch 64] [--sleep-ms 200] [--max-batches-per-minute 30] [--allow-long-swap]
python -m services.knowledge.reembed --tenant <uuid> --kb <uuid> --abandon                      # drop a half-done migration
```

1. Create the new `provider_configs` row (role `embedding`, **768 dimensions**) for the tenant first.
2. **Ownership is checked by the tool.** The CLI usually runs on the service DSN where RLS does not apply, so the module
   requires the KB AND the target config to belong to `--tenant` before any provider call or write (dry run and
   `--abandon` included), and every statement carries its own `tenant_id` predicate. A wrong tenant, KB or config is
   refused with one message and nothing is sent or changed.
3. Dry run, then the real run. It embeds every non-parent chunk of live documents in bounded batches into
   `embedding_next` with the new config (retrying a failed batch like the worker, validating count, 768 dims, finite,
   non-zero). `--sleep-ms` / `--max-batches-per-minute` pace the batches.
4. Queries keep using the old `embedding` and the old config the whole time.
5. When every chunk has a shadow vector made by the target model, one transaction copies `embedding_next` into
   `embedding`, sets `embedding_model`, points the KB at the new config, clears the shadow and writes an audit row.
   The transaction takes `FOR NO KEY UPDATE` on the KB row, so uploads keep working during it; the ingestion worker's
   `FOR SHARE` still conflicts, which is the intended wait. A swap over `KNOWLEDGE_REEMBED_MAX_SWAP_CHUNKS` (20000)
   chunks needs `--allow-long-swap`: the reviewer measured about 70 s per 20k chunks with an HNSW index, so run it
   when quiet.
6. Resumable: re-run the same command after any interruption. Switching `--to-config` re-embeds what the earlier
   target had done. Exit code 0 = swapped (or dry run / already on target), 1 = stopped or incomplete, 2 = refused.
7. After a large swap run `REINDEX INDEX CONCURRENTLY idx_kb_chunks_embedding_hnsw` in a quiet period: the HNSW index
   stays correct through the update but accumulates dead entries until vacuum/reindex, and until then approximate
   searches can return fewer rows than exist.

**One migration per KB.** A transaction-scoped advisory lock on a connection held for the whole fill and swap makes a
second run for the same KB, and `--abandon` during a run, refuse with "a migration of knowledge base ... is already
running" before any provider call (a crash releases it). Other KBs are unaffected. The long-swap size check is repeated
under the KB lock right before the swap, so uploads during a long fill cannot push it past the limit unnoticed (the
shadow vectors are kept; re-run with `--allow-long-swap`).

**Soft-deleted documents.** Their chunks are not re-embedded (their text is not sent out again). The swap clears their
vectors rather than leave old-space ones in a KB on the new model. There is no restore path; re-ingest if needed.

**Chunks added or changed while it runs.** An upload or re-ingest during the migration is embedded by the worker with
the KB's *old* config and so has no shadow vector. The swap re-checks under the KB row lock and refuses if any chunk
is left (the CLI embeds the stragglers and retries, up to 5 rounds). The worker's index step takes `FOR SHARE` on the
KB row (tenant-scoped) and refuses to commit vectors made with a config the KB no longer has (the job is retried with
the new config). A worker blocked on the swap does not get reaped or run twice: it holds its own job row locked, the
reaper skips locked rows (`SKIP LOCKED`), and the test `test_a_worker_waiting_on_the_swap_...` shows one attempt and one
success with a 1 s lease held for 2.5 s. (The heartbeat does not extend the lease during that wait, for the same reason:
its UPDATE waits on the worker's lock. That is harmless only because the reaper also skips the row.)

**Stale plans after the swap (the residual window).** The retrieval plan cache is per process, and the CLI can only
invalidate its own. Other processes keep the old plan for up to `KNOWLEDGE_PLAN_TTL_S` (30 s; a refresh starts after
half of that). Dense search now only compares vectors of the model the plan's embedding config uses (chunks with no
recorded model still count), so a stale plan never meets new-space vectors with an old-model query: dense returns
nothing, lexical search still answers, and the result carries `degraded: ['embedding_model_mismatch']` instead of a
plain ok. The KB's `config_version` is bumped by the existing trigger, but nothing polls it, so the window is the plan
TTL; restart the knowledge service after the swap to remove it entirely.

**Other limits.** (a) Run one migration per KB at a time. Two concurrent runs with different targets cannot produce a
mixed swap (the swap checks its own target) but redo each other's work. (b) Same-named models on different engines are
treated as the same vector space. (c) Enrichment is not re-applied (re-ingest for that).

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
