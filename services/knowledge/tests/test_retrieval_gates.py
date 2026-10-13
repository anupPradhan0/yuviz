"""Hard-invariant gates for the retrieval hot path, run on every build against a small seeded KB.

They assert things that must hold on any machine, not latencies that depend on one:
  - under each injected fault (hung embedder, slow database, erroring branch, dead reranker) the answer arrives
    inside the 400 ms deadline plus a documented margin;
  - every answer carries a typed status (ok | no_match | partial | unavailable), never a bare error;
  - after a burst nothing is left behind: no admitted slot, no waiter, no checked-out connection;
  - an open breaker stops calling the dependency.
The p50/p95/p99 figures are measured by `bench_retrieval.py` and written up in docs/knowledge-rag-ops.md."""

from __future__ import annotations

import asyncio
import os
import random

import pytest
import pytest_asyncio

from libs.tenancy import set_caller_tenant
from services.knowledge import db, ops_metrics, retrieval
from services.knowledge.resilience import RetrievalConfig, admission, breakers
from services.knowledge.tests import bench_retrieval as bench
from services.knowledge.tests.rag_fakes import FakeEmbedder
from services.knowledge.vector_repository import PgVectorRepository

# The deadline is 400 ms. The margin is for scheduler jitter and event-loop stalls on a shared CI runner: measured
# worst case on a laptop is ~205 ms, so 250 ms of slack is generous without hiding a real overrun (the failure
# this gate exists to catch is "a stage ignored the deadline and ran for its full 500 ms to 5 s").
MARGIN_MS = float(os.environ.get("KNOWLEDGE_GATE_MARGIN_MS", "250"))
DEADLINE_MS = 400.0  # the design's hard deadline, pinned here so changing the config cannot move the goalposts

# What each fault must degrade to. "unavailable" is only correct when every branch is lost and nothing else can answer.
EXPECTED = {
    "C1.": {"partial"}, "C1b.": {"partial"}, "C2.": {"unavailable(raised)"}, "C3.": {"partial"},
    "C4.": {"partial"}, "C5.": {"ok"}, "C6.": {"unavailable(raised)"},
}


@pytest_asyncio.fixture(loop_scope="session", scope="module")
async def kb():
    pool = await db.get_pool()  # not the function-scoped `pool` fixture: this one lives for the module
    tenant, agent, kb_row, cfg = await bench.seed(pool, 600)
    yield bench.Bench(pool, tenant, agent, {"tenant_slug": tenant["slug"], "agent_slug": agent["slug"]}, random.Random(5))
    await bench.cleanup(pool, tenant, agent, kb_row, cfg)


@pytest_asyncio.fixture(loop_scope="session", scope="module")
async def fault_results(kb):
    retrieval.clear_caches()
    results: list = []
    shared = PgVectorRepository()
    await kb.call(FakeEmbedder(), shared)(0)  # warm: plan, connections, capability probe
    await kb.run_faults(12, results, shared, slow_n=6)
    return results


def test_every_fault_scenario_ran(fault_results):
    assert {r["scenario"].split()[0] for r in fault_results} == set(EXPECTED), [r["scenario"] for r in fault_results]


def test_the_deadline_holds_under_every_injected_fault(fault_results):
    too_slow = [(r["scenario"], round(r["max_ms"])) for r in fault_results if r["max_ms"] > DEADLINE_MS + MARGIN_MS]
    assert not too_slow, f"deadline {DEADLINE_MS:.0f} ms + margin {MARGIN_MS:.0f} ms exceeded: {too_slow}"


def test_every_answer_under_fault_is_typed_and_degrades_the_documented_way(fault_results):
    for r in fault_results:
        assert r["untyped"] == 0, r
        expected = EXPECTED[r["scenario"].split()[0]]
        assert set(r["statuses"]) <= expected, (r["scenario"], r["statuses"], expected)


def test_no_limiter_slot_or_connection_leaks_after_a_fault_burst(fault_results):
    assert [(r["scenario"], r["leaks"]) for r in fault_results if any(r["leaks"].values())] == []


async def test_a_burst_over_the_admission_limit_is_all_typed_and_leaves_nothing_behind(kb):
    retrieval.clear_caches()
    ops_metrics.reset()
    results: list = []
    # 40 callers at once against 3 per tenant / 5 per process: most must be refused quickly, none may hang or leak.
    await bench.scenario("burst", 40, kb.call(FakeEmbedder(latency_s=0.03), PgVectorRepository()), results,
                         concurrency=40, pool=kb.pool)
    (row,) = results
    assert row["untyped"] == 0 and set(row["statuses"]) <= {"ok", "unavailable(raised)"}, row
    assert row["statuses"].get("ok", 0) >= 3  # the admitted ones were served
    assert row["max_ms"] <= DEADLINE_MS + MARGIN_MS
    assert not any(row["leaks"].values()), row["leaks"]
    assert ops_metrics.snapshot()["retrieval_overloaded"] == row["statuses"].get("unavailable(raised)", 0)
    assert admission.in_flight == 0


async def test_an_open_embedder_breaker_stops_calling_the_embedder(kb):
    retrieval.clear_caches()
    embedder = FakeEmbedder(latency_s=5.0)
    call = kb.call(embedder, PgVectorRepository())
    for i in range(RetrievalConfig().breaker_failures):
        assert (await call(i))["status"] == "partial"  # each one waits out the 150 ms embed timeout
    assert embedder.calls == RetrievalConfig().breaker_failures
    assert {b.state for key, b in breakers._breakers.items() if key.startswith("embed:")} == {"open"}
    for i in range(10):
        result = await call(100 + i)
        assert result["status"] == "partial" and "embedding_unavailable" in result["degraded"]
    assert embedder.calls == RetrievalConfig().breaker_failures  # open: not one more call reached the dependency
    set_caller_tenant(None)
