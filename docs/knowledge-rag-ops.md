# Knowledge RAG: what to watch

Operating notes for the knowledge service's ingestion queue and retrieval hot path (epic #82, phase #88). Design
targets are in the redesign doc; this page is what to look at, why, and how each target is checked.

Every tunable named here (deadlines, limiter, pool, caches, retention, parser limits, opt-ins) is listed with its default and meaning in `docs/knowledge-env-vars.md`.

## Where the signals are

| Signal | Where | Scope |
|---|---|---|
| Queue depth, oldest pending age, running, stuck, dead-letter, failed documents with reasons | `GET /tenants/{tenant}/knowledge/health` | one tenant (RLS, plus an explicit `app.tenant_id` predicate) |
| Last-24h retrieval counts per status, no-match rate, degraded counts and reasons, p50/p95/p99 | same endpoint, from `retrieval_logs` | one tenant |
| Ingestion trail for one document (stage, outcome, retry/dead-letter markers) | `GET /documents/{id}/events` | one tenant |
| Limiter overload count, in-flight retrievals, breaker states, plan-cache hit/miss, `retrieval_logs` drops | `GET /internal/ops` (JSON) and `GET /internal/ops?format=prometheus` | process; platform-scoped callers only (service account or superadmin); no tenant labels, ids or query text |
| One row per retrieval (trace id, status, degraded reasons, per-stage timings; a cold plan is `retrieval_metadata.plan_cache = miss`, not a degraded reason) | `retrieval_logs`, 30-day retention | tenant, RLS |

The repo has no `/metrics` convention (the conversation service and the gateway take an injected `IMetrics` sink),
so the process counters are a JSON endpoint behind the existing auth, with a Prometheus text rendering for a
scraper. Counters reset on restart; use `rate()`/`increase()`. They are per process: sum across replicas.

The admin UI screens for these endpoints are not built yet; the endpoints are the deliverable.

## What to watch, and why

1. **Oldest pending job age.** A queue that is long but moving is fine; a queue whose oldest job keeps ageing means no
   worker is claiming (worker down, per-tenant cap held by stuck jobs, or every job failing transiently and backing
   off). This is the earliest signal of the "document reaches a terminal state within 10 minutes" SLO failing.
2. **Stuck running jobs** (`status = 'running'` with an expired lease). The reaper requeues them every 15 s; any that
   persist mean the reaper itself is not running, or a worker keeps dying mid-job.
3. **Dead-letter growth.** Dead-letter is a document that exhausted its retries on transient errors. One is a bad day
   at a provider; a rising rate is an embedding provider outage or a misconfigured provider config. Each carries its
   redacted reason (`/health` `failed_documents`) and can be retried from the existing retry route.
4. **Degraded / fallback rate.** `partial` answers mean a branch was lost (embedder, vector search or lexical search).
   Callers still got an answer, so nothing pages on a single one, but a sustained rate is an unhealthy dependency:
   `degraded_by_reason` says which (`embedding_unavailable`, `lexical_only`, `dense_failed`, `rerank_skipped`,
   `deadline`, `overloaded`, `prompt_docs_trimmed`). Prompt-mode documents are always returned but capped at half of `max_tokens`; `prompt_docs_trimmed` means some did not fit, so a growing prompt-mode set is eating the budget for ranked answers.
5. **`unavailable` rate.** The caller-visible failure: the tool envelope tells the agent to apologise or transfer.
   This is the number behind "retrieval error surfaced to the caller < 0.1%".
6. **No-match rate.** Jumps mean content is missing (a document failed ingestion, a KB was detached) or queries have
   drifted away from the content. A slow climb with a stable ingest means a content gap, which is a product signal,
   not an incident.
7. **Breaker state and limiter overload.** `knowledge_breakers{state="open"}` means a dependency is being skipped
   (the hot path pays nothing for it, but answers are degraded). `knowledge_retrieval_overloaded_total` rising means
   the process is shedding load: the admission limiter or the DB pool is saturated.
8. **Plan-cache hit ratio.** Should be near 1 once agents are warm (call-start prewarm). A falling ratio means plans
   are being evicted or invalidated more than expected, and the cold-plan cost (a few queries) is landing inside calls.
9. **`retrieval_logs` growth and drops.** The table is purged hourly past retention. `retrieval_logs_dropped_total`
   rising means the writer cannot keep up (rows are dropped, never block retrieval); a table that grows past what
   30 days of traffic explains means the purge loop is failing.

## Alert rule examples

The first group uses the counters exposed by `/internal/ops?format=prometheus`. The second group needs queue and log
numbers that live in Postgres; export them with a `postgres_exporter` custom query (below) or any SQL-to-metrics
job, then the same rules apply. Thresholds are starting points, not tuned values.

```yaml
groups:
  - name: knowledge-rag-process
    rules:
      - alert: KnowledgeRetrievalShedding
        expr: sum(rate(knowledge_retrieval_overloaded_total[5m])) > 0.2
        for: 10m
        annotations: {summary: "Knowledge service is refusing retrievals (limiter or DB pool saturated)"}
      - alert: KnowledgeBreakerOpen
        expr: sum by (dependency) (knowledge_breakers{state="open"}) > 0
        for: 5m
        annotations: {summary: "A knowledge dependency breaker has been open for 5 minutes (embed or rerank)"}
      - alert: KnowledgePlanCacheCold
        expr: |
          sum(rate(knowledge_plan_cache_total{result="hit"}[15m]))
            / sum(rate(knowledge_plan_cache_total[15m])) < 0.8
        for: 15m
        annotations: {summary: "Retrieval plan cache hit ratio below 80%: cold plans are landing inside calls"}
      - alert: KnowledgeRetrievalLogsDropping
        expr: sum(rate(knowledge_retrieval_logs_dropped_total{reason="queue_full"}[10m])) > 0
        for: 10m
        annotations: {summary: "retrieval_logs writer is dropping rows (database slow)"}

  - name: knowledge-rag-postgres   # metric names come from the exporter queries below
    rules:
      - alert: KnowledgeIngestionStalled
        expr: knowledge_ingestion_oldest_pending_seconds > 600
        for: 5m
        annotations: {summary: "Oldest pending ingestion job is older than 10 minutes (the terminal-state SLO)"}
      - alert: KnowledgeStuckJobs
        expr: knowledge_ingestion_stuck_running > 0
        for: 10m
        annotations: {summary: "Running jobs with an expired lease persist: is the lease reaper running?"}
      - alert: KnowledgeDeadLetterGrowing
        expr: increase(knowledge_ingestion_dead_letter_total[1h]) > 3
        annotations: {summary: "Documents are exhausting their ingestion retries"}
      - alert: KnowledgeRetrievalDegraded
        expr: knowledge_retrieval_degraded_ratio_15m > 0.1
        for: 15m
        annotations: {summary: "More than 10% of retrievals took a fallback"}
      - alert: KnowledgeRetrievalUnavailable
        expr: knowledge_retrieval_unavailable_ratio_15m > 0.001
        for: 10m
        annotations: {summary: "Retrieval errors surfaced to callers above the 0.1% target"}
      - alert: KnowledgeNoMatchSpike
        expr: knowledge_retrieval_no_match_ratio_1h > 0.5
        for: 1h
        annotations: {summary: "Over half of retrievals find nothing: missing content or a detached KB"}
```

Exporter queries (a platform database role reads across tenants; this is an operator job, not an app path):

```yaml
knowledge_ingestion:
  query: |
    SELECT coalesce(extract(epoch FROM now() - min(created_at) FILTER (WHERE status = 'pending')), 0) AS oldest_pending_seconds,
           count(*) FILTER (WHERE status = 'running' AND lease_expires_at < now())                     AS stuck_running,
           count(*) FILTER (WHERE status = 'dead_letter')                                              AS dead_letter_total
      FROM kb_ingestion_jobs
  metrics:
    - oldest_pending_seconds: {usage: GAUGE, description: "Age of the oldest pending job"}
    - stuck_running:          {usage: GAUGE, description: "Running jobs past their lease"}
    - dead_letter_total:      {usage: COUNTER, description: "Jobs in dead_letter"}
knowledge_retrieval:
  query: |
    SELECT count(*) FILTER (WHERE cardinality(degraded) > 0)::float / greatest(count(*), 1)    AS degraded_ratio_15m,
           count(*) FILTER (WHERE status IN ('unavailable', 'error'))::float / greatest(count(*), 1) AS unavailable_ratio_15m
      FROM retrieval_logs WHERE created_at >= now() - interval '15 minutes'
  metrics:
    - degraded_ratio_15m:    {usage: GAUGE, description: "Share of retrievals that took a fallback"}
    - unavailable_ratio_15m: {usage: GAUGE, description: "Share of retrievals the caller saw fail"}
```

## SQL

Run as a database operator (a role that bypasses RLS) for the whole fleet, or under a tenant's scope for one tenant.

```sql
-- Queue depth by state
SELECT status, count(*) FROM kb_ingestion_jobs GROUP BY status ORDER BY status;

-- Oldest pending age (seconds), and how many pending jobs are past their next_attempt_at (due now)
SELECT extract(epoch FROM now() - min(created_at)) AS oldest_pending_s,
       count(*) FILTER (WHERE next_attempt_at <= now()) AS due_now
  FROM kb_ingestion_jobs WHERE status = 'pending';

-- Dead-letter growth: jobs that dead-lettered per hour, last 24 h
SELECT date_trunc('hour', finished_at) AS hour, count(*)
  FROM kb_ingestion_jobs WHERE status = 'dead_letter' AND finished_at > now() - interval '24 hours'
 GROUP BY 1 ORDER BY 1;

-- Stuck running jobs (lease expired; the reaper should have requeued them within ~15 s)
SELECT id, document_id, attempts, stage, lease_expires_at, now() - lease_expires_at AS overdue
  FROM kb_ingestion_jobs WHERE status = 'running' AND lease_expires_at < now() ORDER BY lease_expires_at;

-- Terminal-state SLO: documents not terminal 10 minutes after upload (should be zero)
SELECT d.id, d.status, now() - d.created_at AS age
  FROM kb_documents d
 WHERE d.deleted_at IS NULL AND d.status IN ('pending', 'processing') AND d.created_at < now() - interval '10 minutes'
   AND NOT EXISTS (SELECT 1 FROM kb_ingestion_jobs j WHERE j.document_id = d.id AND j.status = 'pending'
                   AND j.attempts > 0 AND j.next_attempt_at > now());   -- a job backing off a transient error is waiting, not lost

-- Time to terminal state, last 24 h (p50 / p95 / p99, seconds)
SELECT percentile_cont(ARRAY[0.5, 0.95, 0.99]) WITHIN GROUP (ORDER BY extract(epoch FROM j.finished_at - d.created_at))
  FROM kb_ingestion_jobs j JOIN kb_documents d ON d.id = j.document_id
 WHERE j.finished_at > now() - interval '24 hours' AND j.status IN ('succeeded', 'failed', 'dead_letter');

-- Fallback / degraded rate and the reasons, last hour
SELECT count(*) FILTER (WHERE cardinality(degraded) > 0)::float / greatest(count(*), 1) AS degraded_rate FROM retrieval_logs
 WHERE created_at > now() - interval '1 hour';
SELECT d AS reason, count(*) FROM retrieval_logs, unnest(degraded) d
 WHERE created_at > now() - interval '1 hour' GROUP BY d ORDER BY 2 DESC;

-- No-match and unavailable rate, last hour
SELECT count(*) FILTER (WHERE status = 'no_match')::float / greatest(count(*), 1) AS no_match_rate,
       count(*) FILTER (WHERE status IN ('unavailable', 'error'))::float / greatest(count(*), 1) AS unavailable_rate
  FROM retrieval_logs WHERE created_at > now() - interval '1 hour';

-- retrieval_logs growth: rows per hour and table size (expect a plateau at about 30 days of traffic)
SELECT date_trunc('hour', created_at) AS hour, count(*) FROM retrieval_logs
 WHERE created_at > now() - interval '24 hours' GROUP BY 1 ORDER BY 1;
SELECT pg_size_pretty(pg_total_relation_size('retrieval_logs')), (SELECT min(created_at) FROM retrieval_logs) AS oldest_row;
```

## SLOs and how each is checked

| Target | How it is checked | Where it is enforced |
|---|---|---|
| Retrieval p50 < 100 ms | Measured by `bench_retrieval.py`; in production from `retrieval_logs` `timings_ms.total` (the `/health` endpoint shows p50/p95/p99 per tenant) | Measured, not gated: latency depends on the machine |
| Retrieval p95 < 250 ms, rerank off | Same | Same |
| Retrieval p99 <= 400 ms | The 400 ms deadline is a hard cap in `retrieve()`; the gate asserts that every call under every injected fault returns within it (plus a CI-jitter margin) | `test_retrieval_gates.py` fails the build if a stage ignores the deadline |
| Retrieval error surfaced to caller < 0.1% | `unavailable` + `error` share of `retrieval_logs`; every answer is typed, never a bare error | Typed-status gate in CI; the `KnowledgeRetrievalUnavailable` alert in production |
| Document reaches a terminal state within 10 min (99.9%) | The "not terminal after 10 minutes" and time-to-terminal queries above; the chaos corpus asserts no file stays `processing` | `test_corpus_invariant.py` and `test_e2e_pipeline.py` in CI; `KnowledgeIngestionStalled` in production |
| No half-states after an edit | Re-index swaps chunks in one transaction | `test_reingest_swaps_chunks_atomically_with_no_gap_for_readers` polls the chunk count during a re-index and fails on any empty read |
| Recall does not regress | recall@5, MRR and answer-in-context on the golden set, chunking-only and through the whole pipeline | `eval/test_golden_gates.py` against `eval/baseline.json` |

### Regression gates and the baseline

- `services/knowledge/tests/test_retrieval_gates.py` (runs by default, about 10 s): deadline held, typed status, no
  limiter slot, waiter or pool connection left after a fault burst or an over-limit burst, and an open breaker stops
  calling its dependency. The margin on the deadline is 250 ms (`KNOWLEDGE_GATE_MARGIN_MS`): the measured worst case
  on a laptop is about 205 ms against the 400 ms deadline, and the failure it catches is a stage running for its full
  500 ms to 5 s.
- `services/knowledge/tests/eval/test_golden_gates.py`: fails if recall@5, MRR or answer-in-context fall more than 0.05
  (about two of the 39 golden questions) below `eval/baseline.json`. Two views: the chunking strategies in memory
  (including `production (auto)`, the worker's default chunker), and the whole pipeline on Postgres once per chunker
  mode: `auto` (the shipped default, fixed windows for headed prose) and `structure` (the opt-in parent/child path,
  gated so it cannot rot). Baselines are what this code measured, with fake embedders, so they catch regressions rather
  than predict real-model scores. The pipeline eval is deterministic: it uses exact dense search, generous stage timeouts, no score floor and a total order on equal scores (identical templated chunks otherwise come back in physical row order, which differs on a database other tests have churned). With the bag-of-words embedder the default `auto` scores
  below `structure` through the pipeline (recall@5 0.77 vs 0.87), the opposite of Phase 4's hash-embedder sweep: neither
  predicts the real model, which is why the choice is per-deployment (`KNOWLEDGE_CHUNKER`).
- Refresh the baseline deliberately (a model change, a chunking change or a corpus change legitimately moves it):

  ```
  POSTGRES_DSN=postgresql://.../scratchdb python -m services.knowledge.tests.eval.gates --update-baseline
  ```

  Review the diff of `baseline.json` like code.

## Measured numbers

Run with `python services/knowledge/tests/bench_retrieval.py --n 300 --chunks 4000 --json-out results.json` against a
scratch database (it seeds and removes its own tenant). Measured on macOS-26.5.2-arm64-arm-64bit (arm),
Python 3.11.17, one process, Postgres on localhost, 4000 child chunks (plus parents) in one KB,
deterministic fake embedder with injected latency, N=300 calls per scenario, deadline 400 ms.
Statuses `unavailable(raised)` are the typed `RetrievalUnavailable` that the router turns into `status: "unavailable"`.

| Scenario | n | p50 ms | p95 ms | p99 ms | max ms | Statuses |
|---|---|---|---|---|---|---|
| A. warm plan + embed cache HIT | 300 | 27 | 43 | 50 | 64 | ok 300 |
| A8. same, 8 concurrent callers | 300 | 124 | 174 | 199 | 201 | ok 300 |
| B. warm plan + embed MISS (60 ms injected) | 300 | 86 | 91 | 102 | 114 | ok 300 |
| D. cold plan every call (embed hit) | 100 | 13 | 14 | 16 | 21 | ok 100 |
| C1. embedder hangs 5 s (breaker opens after 5) | 300 | 11 | 11 | 152 | 153 | partial 300 |
| C1b. embedder hangs 5 s, breaker disabled | 300 | 152 | 152 | 153 | 153 | partial 300 |
| C2. DB slowed 500 ms (pg_sleep) on both branches | 300 | 189 | 192 | 193 | 196 | unavailable(raised) 300 |
| C3. lexical branch errors (dense answers) | 300 | 84 | 122 | 167 | 177 | partial 300 |
| C4. dense DB statement slowed 500 ms (lexical answers) | 300 | 189 | 191 | 199 | 227 | partial 300 |
| C5. reranker dead, rerank=true | 300 | 88 | 95 | 108 | 175 | ok 299, partial 1 |
| C6. embedder hung AND DB slowed (typed unavailable) | 300 | 153 | 153 | 153 | 155 | unavailable(raised) 300 |

Against the targets (rerank off): p50 < 100 ms holds in every healthy scenario (27 ms warm, 86 ms with a 60 ms embedder
miss); p95 < 250 ms holds (43 ms warm, 91 ms miss, 174 ms with 8 concurrent callers queueing on the per-tenant limiter);
p99 <= 400 ms holds in every scenario including every injected fault (worst single call 227 ms). No pool connection or
limiter slot was left behind after any scenario.

Caveats, read before quoting these:
- The database is on localhost, so network round trips are about zero; a remote database adds its round trips to every
  DB stage. The fault scenarios measure the deadline logic, which does not depend on that.
- The embedder is a fake. A real provider's latency on a cache miss is the largest variable in production; the 60 ms
  "MISS" row is an assumption, not a measurement. The query-embedding cache makes repeated queries free.
- Developer laptop with other load running (the p50 of the warm row moved between 16 ms and 27 ms across runs). Treat
  differences under about 2x as noise. Rerun on production-shaped hardware before signing the SLOs off.
- 4,000 chunks is small. The 100k-chunk lexical benchmark (`bench_lexical_100k.py`) covers the lexical branch at scale;
  nothing here measures dense search at 100k+ chunks or with a real HNSW build under write load.
- Sequential callers except the 8-way row. This is latency of one call, not throughput.
- The C2/C6 scenarios answer `unavailable` by design (every branch lost, no prompt-mode content to fall back on): the
  point is that they say so inside the deadline instead of hanging.

## Dense search returns nothing after a bulk delete

The dense leg reads the shared HNSW index on `kb_chunks`. After a large delete (re-ingest of a big document, removing a
knowledge base) the index still holds the dead entries until vacuum runs; a filtered search can then exhaust
`hnsw.max_scan_tuples` inside dead entries and return zero rows, so hybrid retrieval silently falls back to the lexical leg.
The schema sets an aggressive autovacuum on `kb_chunks` (2% scale factor). After a very large delete, run
`VACUUM (ANALYZE) kb_chunks;` by hand rather than waiting.
