"""Hosted rerankers (Cohere, Voyage) against a fake HTTP transport: no network, no database. Every failure mode
must leave the fused order untouched, report a fixed degraded code and stay inside the stage budget."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from services.knowledge import ops_metrics, resilience, retrieval
from services.knowledge.reranker import (
    CohereReranker, RerankConfig, RerankError, VoyageReranker, build_reranker, is_decisive,
)
from services.knowledge.resilience import RetrievalConfig
from services.knowledge.vector_repository import VectorMatch

QUERY = "how long do refunds take"


class _Resolver:
    def __init__(self, value="sk-test-key"):
        self.value = value
        self.calls = 0

    async def resolve(self, ref):
        self.calls += 1
        if self.value is None:
            raise KeyError(ref)
        return self.value


def _m(i: int, score: float = 0.5, found_by=("dense",), content: str | None = None) -> VectorMatch:
    return VectorMatch(
        chunk_id=f"c{i}", document_id="d", kb_id="kb", content=content or f"chunk text {i}", score=score,
        token_count=3, page=None, language=None, tags={}, version=1, document_title="T", kb_slug="kb",
        found_by=found_by)


def _matches(n=5):
    return [_m(i, 0.6 - i * 0.01) for i in range(n)]


def _cohere(handler, **kw):
    return CohereReranker(resolver=_Resolver(), transport=httpx.MockTransport(handler), **kw)


def _voyage(handler, **kw):
    return VoyageReranker(resolver=_Resolver(), transport=httpx.MockTransport(handler), **kw)


def _cohere_ok(scores):
    def handler(request):
        return httpx.Response(200, json={"results": [{"index": i, "relevance_score": s} for i, s in enumerate(scores)]})
    return handler


def _voyage_ok(scores):
    def handler(request):
        return httpx.Response(200, json={"data": [{"index": i, "relevance_score": s} for i, s in enumerate(scores)]})
    return handler


# --- success -------------------------------------------------------------------------------------------------

async def test_cohere_reorders_by_relevance_and_keeps_the_tail():
    ms = _matches(5)
    rr = _cohere(_cohere_ok([0.1, 0.9, 0.2, 0.3]), config=RerankConfig(top_n=4))
    out = await rr.rerank(QUERY, ms)
    assert [m.chunk_id for m in out] == ["c1", "c3", "c2", "c0", "c4"]  # top 4 reranked, c4 stays behind them


async def test_voyage_reorders_by_relevance():
    ms = _matches(4)
    out = await _voyage(_voyage_ok([0.1, 0.2, 0.9, 0.3])).rerank(QUERY, ms)
    assert [m.chunk_id for m in out] == ["c2", "c3", "c1", "c0"]


async def test_request_shape_and_only_query_and_chunk_text_are_sent():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return _cohere_ok([0.3, 0.2, 0.1])(request)

    ms = _matches(3)
    await _cohere(handler).rerank(QUERY, ms)
    assert seen["auth"] == "Bearer sk-test-key" and seen["url"] == "https://api.cohere.com/v2/rerank"
    assert set(seen["body"]) == {"model", "query", "documents", "top_n"}
    assert seen["body"]["documents"] == [m.content for m in ms] and seen["body"]["query"] == QUERY
    blob = json.dumps(seen["body"])
    assert "c0" not in blob.replace("chunk text 0", "") and "document_id" not in blob  # no ids, titles or tenants


async def test_voyage_request_shape():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["url"] = str(request.url)
        return _voyage_ok([0.3, 0.2])(request)

    await _voyage(handler).rerank(QUERY, _matches(2))
    assert seen["url"] == "https://api.voyageai.com/v1/rerank" and set(seen["body"]) == {"model", "query", "documents", "top_k"}


async def test_payload_is_capped_per_document_and_in_total():
    seen = {}

    def handler(request):
        seen["docs"] = json.loads(request.content)["documents"]
        return _cohere_ok([0.1] * len(seen["docs"]))(request)

    ms = [_m(i, content="x" * 5000) for i in range(10)]
    cfg = RerankConfig(top_n=10, doc_chars=300, max_payload_chars=1000)
    out = await _cohere(handler, config=cfg).rerank(QUERY, ms)
    assert all(len(d) <= 300 for d in seen["docs"]) and sum(map(len, seen["docs"])) <= 1000
    assert len(seen["docs"]) == 3 and len(out) == 10  # unsent candidates stay in the result, in fused order
    assert [m.chunk_id for m in out[3:]] == [f"c{i}" for i in range(3, 10)]


async def test_top_n_default_is_twenty():
    seen = {}

    def handler(request):
        seen["n"] = len(json.loads(request.content)["documents"])
        return _cohere_ok([0.1] * seen["n"])(request)

    await _cohere(handler).rerank(QUERY, [_m(i) for i in range(30)])
    assert seen["n"] == 20


# --- decisive skip ---------------------------------------------------------------------------------------------

def test_decisive_rule():
    cfg = RerankConfig()
    both = ("dense", "lexical")
    assert is_decisive([_m(0, 0.9, both), _m(1, 0.6, both)], cfg)
    assert not is_decisive([_m(0, 0.9, both), _m(1, 0.85, both)], cfg)   # runner-up too close
    assert not is_decisive([_m(0, 0.8, both), _m(1, 0.3, both)], cfg)    # winner not strong enough
    assert not is_decisive([_m(0, 0.95, ("dense",)), _m(1, 0.3)], cfg)   # no lexical support
    assert is_decisive([_m(0, 0.9, both)], cfg)


async def test_decisive_top_hit_skips_the_provider_call():
    calls = []

    def handler(request):
        calls.append(1)
        return _cohere_ok([0.1, 0.2])(request)

    ms = [_m(0, 0.95, ("dense", "lexical")), _m(1, 0.3)]
    before = ops_metrics.snapshot()["rerank_decisive_skip"]
    out = await _cohere(handler).rerank(QUERY, ms)
    assert out == ms and not calls and ops_metrics.snapshot()["rerank_decisive_skip"] == before + 1


async def test_decisive_thresholds_are_env_tunable(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_RERANK_DECISIVE_SCORE", "0.99")
    monkeypatch.setenv("KNOWLEDGE_RERANK_TOP_N", "7")
    cfg = RerankConfig.from_env()
    assert cfg.decisive_score == 0.99 and cfg.top_n == 7
    assert not is_decisive([_m(0, 0.95, ("dense", "lexical")), _m(1, 0.3)], cfg)


# --- failure modes: fused order kept, fixed reason, bounded time --------------------------------------------

def _status(code):
    return lambda request: httpx.Response(code, json={"message": "provider says: secret-ish detail"})


@pytest.mark.parametrize("make", [_cohere, _voyage])
@pytest.mark.parametrize("status,reason", [(429, "rerank_rate_limited"), (500, "rerank_provider_error"),
                                           (503, "rerank_provider_error"), (401, "rerank_provider_error")])
async def test_http_errors_raise_a_fixed_reason(make, status, reason):
    with pytest.raises(RerankError) as err:
        await make(_status(status)).rerank(QUERY, _matches())
    assert err.value.degraded_reason == reason and "secret-ish" not in str(err.value)


@pytest.mark.parametrize("make,ok", [(_cohere, _cohere_ok), (_voyage, _voyage_ok)])
async def test_malformed_and_wrong_count_responses_are_rejected(make, ok):
    bad = [
        lambda r: httpx.Response(200, text="not json"),
        lambda r: httpx.Response(200, json={"unexpected": 1}),
        lambda r: httpx.Response(200, json={"results": "x", "data": "x"}),
        ok([0.5, 0.4, 0.3]),                                             # 3 results for 5 candidates
        ok([0.5] * 7),                                                   # more than were sent
        lambda r: httpx.Response(200, json={"results": [{"index": 0, "relevance_score": "high"}] * 5,
                                            "data": [{"index": 0, "relevance_score": "high"}] * 5}),
        lambda r: httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.5}] * 5,
                                            "data": [{"index": 0, "relevance_score": 0.5}] * 5}),  # duplicate index
        lambda r: httpx.Response(200, json={"results": [{"index": 9 + i, "relevance_score": 0.5} for i in range(5)],
                                            "data": [{"index": 9 + i, "relevance_score": 0.5} for i in range(5)]}),
    ]
    for handler in bad:
        with pytest.raises(RerankError) as err:
            await make(handler).rerank(QUERY, _matches(5))
        assert err.value.degraded_reason == "rerank_bad_response"


async def test_a_hung_provider_is_cut_at_the_adapter_timeout():
    async def handler(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    t0 = time.monotonic()
    with pytest.raises(RerankError) as err:
        await _cohere(handler, config=RerankConfig(http_timeout_s=0.05)).rerank(QUERY, _matches())
    assert err.value.degraded_reason == "rerank_timeout" and time.monotonic() - t0 < 0.3


async def test_connection_errors_are_provider_errors():
    def handler(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(RerankError) as err:
        await _cohere(handler).rerank(QUERY, _matches())
    assert err.value.degraded_reason == "rerank_provider_error"


async def test_unresolvable_key_is_not_configured_and_never_echoed(caplog):
    rr = CohereReranker(resolver=_Resolver(None), key_ref="env:NOPE", transport=httpx.MockTransport(_cohere_ok([1, 2])))
    with pytest.raises(RerankError) as err:
        await rr.rerank(QUERY, _matches(2))
    assert err.value.degraded_reason == "rerank_not_configured" and "NOPE" not in caplog.text


async def test_the_key_is_resolved_once():
    resolver = _Resolver()
    rr = CohereReranker(resolver=resolver, transport=httpx.MockTransport(_cohere_ok([0.1, 0.2, 0.3])))
    await rr.rerank(QUERY, _matches(3))
    await rr.rerank(QUERY, _matches(3))
    assert resolver.calls == 1


async def test_no_query_or_chunk_text_is_logged(caplog):
    caplog.set_level("DEBUG")
    secret_query = "my ssn is 123-45-6789"
    ms = [_m(0, content="confidential clause alpha"), _m(1, content="confidential clause beta")]
    await _cohere(_cohere_ok([0.1, 0.9])).rerank(secret_query, ms)
    with pytest.raises(RerankError):
        await _cohere(_status(500)).rerank(secret_query, ms)
    assert "123-45" not in caplog.text and "confidential" not in caplog.text


# --- inside retrieve(): breaker, deadline, degraded reasons ------------------------------------------------------

async def test_stage_reports_reason_and_keeps_fused_order_on_error():
    degraded, timings = [], {}
    ms = _matches()
    out = await retrieval._rerank(_cohere(_status(429)), QUERY, ms, RetrievalConfig(), 0.4, degraded, timings)
    assert out == ms and degraded == ["rerank_skipped", "rerank_rate_limited"] and "rerank" in timings


async def test_stage_budget_holds_even_when_the_adapter_timeout_is_longer():
    async def handler(request):
        await asyncio.sleep(5)

    rr = _cohere(handler, config=RerankConfig(http_timeout_s=3.0))
    degraded, ms = [], _matches()
    t0 = time.monotonic()
    out = await retrieval._rerank(rr, QUERY, ms, RetrievalConfig(rerank_timeout_s=0.05), 0.4, degraded, {})
    assert out == ms and degraded == ["rerank_skipped", "rerank_timeout"] and time.monotonic() - t0 < 0.3


async def test_breaker_opens_after_repeated_provider_failures():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500)

    cfg = RetrievalConfig(breaker_failures=2, breaker_cooldown_s=60)
    rr = _cohere(handler)
    for _ in range(4):
        degraded = []
        await retrieval._rerank(rr, QUERY, _matches(), cfg, 0.4, degraded, {})
        assert degraded[0] == "rerank_skipped"
    assert len(calls) == 2  # the third and fourth never reached the provider
    resilience.breakers.clear()


async def test_success_through_the_stage_reorders():
    degraded = []
    out = await retrieval._rerank(_voyage(_voyage_ok([0.1, 0.9, 0.2])), QUERY, _matches(3), RetrievalConfig(), 0.4, degraded, {})
    assert out[0].chunk_id == "c1" and degraded == []
    resilience.breakers.clear()


# --- selection ----------------------------------------------------------------------------------------------------

def test_default_is_off_and_selection_by_env():
    assert build_reranker({}) is None and build_reranker({"KNOWLEDGE_RERANKER": "none"}) is None
    assert isinstance(build_reranker({"KNOWLEDGE_RERANKER": "cohere"}), CohereReranker)
    assert isinstance(build_reranker({"KNOWLEDGE_RERANKER": " Voyage "}), VoyageReranker)
    assert build_reranker({"KNOWLEDGE_RERANKER": "bogus"}) is None


def test_runtime_default_reranker_is_the_unconfigured_noop(monkeypatch):
    from services.knowledge import runtime
    monkeypatch.delenv("KNOWLEDGE_RERANKER", raising=False)
    runtime.set_reranker(None)
    assert runtime.get_reranker().configured is False
    monkeypatch.setenv("KNOWLEDGE_RERANKER", "cohere")
    runtime.set_reranker(None)
    assert runtime.get_reranker().configured is True
    runtime.set_reranker(None)
