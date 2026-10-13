"""Hosted rerankers (Cohere, Voyage) against a fake HTTP transport: no network, no database. Every failure mode
must leave the fused order untouched, report a fixed degraded code and stay inside the stage budget."""

from __future__ import annotations

import asyncio
import json
import ssl
import time

import httpx
import pytest

from services.knowledge import ops_metrics, resilience, retrieval
from services.knowledge.reranker import (
    CohereReranker, RerankConfig, RerankError, TenantBudget, VoyageReranker, build_reranker, is_decisive,
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
    assert is_decisive([_m(0, 0.9, ("dense",)), _m(1, 0.6)], cfg)           # lexical support is not required
    assert not is_decisive([_m(0, 0.9, both), _m(1, 0.85, both)], cfg)      # runner-up too close
    assert not is_decisive([_m(0, 0.8, both), _m(1, 0.3, both)], cfg)       # winner not strong enough
    assert is_decisive([_m(0, 0.9, both)], cfg)


def test_a_result_without_dense_support_is_never_decisive():
    cfg = RerankConfig()
    assert not is_decisive([_m(0, 0.99, ("lexical",)), _m(1, 0.2)], cfg)    # a lexical score is another scale
    assert not is_decisive([_m(0, 0.99, ("lexical",))], cfg)


def test_a_lexical_only_runner_up_is_not_compared_on_its_score():
    cfg = RerankConfig()
    # fused order puts a lexical-only hit second; its 0.95 is a ts_rank score, not a cosine, and must not count
    assert is_decisive([_m(0, 0.9, ("dense", "lexical")), _m(1, 0.95, ("lexical",)), _m(2, 0.5)], cfg)


def test_a_near_tied_dense_candidate_in_third_place_defeats_decisiveness():
    cfg = RerankConfig()
    ms = [_m(0, 0.9, ("dense", "lexical")), _m(1, 0.5, ("lexical",)), _m(2, 0.88, ("dense",))]
    assert not is_decisive(ms, cfg)
    assert not is_decisive([_m(0, 0.9, ("dense",)), _m(1, 0.3), _m(2, 0.95, ("dense",))], cfg)  # a better dense one behind


async def test_decisive_top_hit_skips_the_provider_call():
    calls = []

    def handler(request):
        calls.append(1)
        return _cohere_ok([0.1, 0.2])(request)

    ms = [_m(0, 0.95, ("dense", "lexical")), _m(1, 0.3, ("dense",))]
    before = ops_metrics.snapshot()["rerank_decisive_skip"]
    out = await _cohere(handler).rerank(QUERY, ms)
    assert out == ms and not calls and ops_metrics.snapshot()["rerank_decisive_skip"] == before + 1


async def test_decisive_thresholds_are_env_tunable(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_RERANK_DECISIVE_SCORE", "0.99")
    monkeypatch.setenv("KNOWLEDGE_RERANK_TOP_N", "7")
    cfg = RerankConfig.from_env()
    assert cfg.decisive_score == 0.99 and cfg.top_n == 7
    assert not is_decisive([_m(0, 0.95, ("dense", "lexical")), _m(1, 0.3, ("dense",))], cfg)


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


# --- per-tenant budget, quota responses and the shared breaker ---------------------------------------------------------

def test_tenant_budget_is_a_per_tenant_token_bucket():
    now = [0.0]
    b = TenantBudget(rate=2.0, burst=3, clock=lambda: now[0])
    assert [b.allow("a") for _ in range(4)] == [True, True, True, False]  # burst of 3, then refused
    assert b.allow("b")                                                    # another tenant is unaffected
    now[0] += 1.0                                                          # 2 tokens refill
    assert [b.allow("a") for _ in range(3)] == [True, True, False]


async def test_a_tenant_over_budget_is_skipped_without_calling_the_provider_or_opening_the_breaker():
    calls = []

    def handler(request):
        calls.append(1)
        return _cohere_ok([0.1, 0.2, 0.3])(request)

    rr = _cohere(handler, config=RerankConfig(tenant_rate=0.01, tenant_burst=2))
    ms = _matches(3)
    cfg = RetrievalConfig(breaker_failures=2, breaker_cooldown_s=60)
    outcomes = []
    for _ in range(6):
        degraded = []
        await retrieval._rerank(rr, QUERY, ms, cfg, 0.4, degraded, {}, "tenant-a")
        outcomes.append(degraded)
    assert len(calls) == 2 and outcomes[2:] == [["rerank_skipped", "rerank_budget"]] * 4
    assert resilience.breakers.get("rerank:tenant-a", cfg).state == "closed"  # four refusals did not open it for everyone
    degraded = []
    await retrieval._rerank(rr, QUERY, ms, cfg, 0.4, degraded, {}, "tenant-b")  # other tenants still get through
    assert degraded == [] and len(calls) == 3
    resilience.breakers.clear()


async def test_decisive_skips_do_not_spend_the_tenants_budget():
    rr = _cohere(_cohere_ok([0.1, 0.2]), config=RerankConfig(tenant_rate=0.01, tenant_burst=1))
    decisive = [_m(0, 0.95, ("dense",)), _m(1, 0.3)]
    for _ in range(5):
        assert await rr.rerank(QUERY, decisive, tenant_id="t") == decisive
    assert rr.budget.allow("t")  # the single token is still there


async def test_quota_429s_never_open_the_shared_breaker_but_5xx_do():
    cfg = RetrievalConfig(breaker_failures=2, breaker_cooldown_s=60)
    for status, state in ((429, "closed"), (500, "open")):
        resilience.breakers.clear()
        rr = _cohere(_status(status))
        for _ in range(4):
            await retrieval._rerank(rr, QUERY, _matches(), cfg, 0.4, [], {}, "t")
        assert resilience.breakers.get("rerank:t", cfg).state == state
    resilience.breakers.clear()


# --- URL safety ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://api.cohere.com/v2/rerank",              # cleartext key
    "https://evil.example/v2/rerank",               # host not allowed
    "https://api.cohere.com.evil.example/v2/rerank",
    "https://user:pw@api.cohere.com/v2/rerank",
    "ftp://api.cohere.com/x", "api.cohere.com/v2/rerank", "",
])
def test_unsafe_rerank_urls_are_refused(url):
    env = {"KNOWLEDGE_RERANKER": "cohere", "KNOWLEDGE_RERANK_URL": url}
    if url == "":
        assert isinstance(build_reranker(env), CohereReranker)  # empty = the vendor default
        return
    with pytest.raises(ValueError):
        build_reranker(env)


def test_a_custom_https_host_needs_an_explicit_allow_list():
    env = {"KNOWLEDGE_RERANKER": "voyage", "KNOWLEDGE_RERANK_URL": "https://rerank.eu.example.com/v1/rerank"}
    with pytest.raises(ValueError):
        build_reranker(env)
    ok = build_reranker({**env, "KNOWLEDGE_RERANK_ALLOWED_HOSTS": "rerank.eu.example.com, other.example.com"})
    assert isinstance(ok, VoyageReranker)


def test_the_service_refuses_to_start_on_an_unsafe_url_and_get_reranker_never_raises(monkeypatch):
    from services.knowledge import runtime
    monkeypatch.setenv("KNOWLEDGE_RERANKER", "cohere")
    monkeypatch.setenv("KNOWLEDGE_RERANK_URL", "http://api.cohere.com/v2/rerank")
    runtime.set_reranker(None)
    with pytest.raises(RuntimeError, match="https"):
        runtime.init_reranker()
    runtime.set_reranker(None)
    rr = runtime.get_reranker()  # a retrieval must never see the construction error
    assert rr.configured is True and runtime.get_reranker() is rr  # built once
    with pytest.raises(RerankError) as err:
        asyncio.run(rr.rerank(QUERY, _matches()))
    assert err.value.degraded_reason == "rerank_not_configured" and err.value.breaker_failure is False
    runtime.set_reranker(None)


# --- garbage environment ---------------------------------------------------------------------------------------------

GARBAGE = ["inf", "-inf", "nan", "-5", "", "abc", "1e999", "0x10", " "]


@pytest.mark.parametrize("value", GARBAGE)
def test_garbage_rerank_env_falls_back_to_defaults(monkeypatch, caplog, value):
    from services.knowledge import envparse
    envparse._warned.clear()
    for name in ("TOP_N", "DOC_CHARS", "MAX_PAYLOAD_CHARS", "HTTP_TIMEOUT_MS", "DECISIVE_SCORE", "DECISIVE_GAP",
                 "TENANT_BURST", "PLATFORM_RATE", "PROCESSES", "TENANT_SHARE"):
        monkeypatch.setenv(f"KNOWLEDGE_RERANK_{name}", value)
    cfg = RerankConfig.from_env()
    assert cfg == RerankConfig()
    warned = [r for r in caplog.records if "KNOWLEDGE_RERANK" in r.getMessage()]
    assert len(warned) == 10 and all(value not in r.getMessage() or value in ("", " ") for r in warned)
    RerankConfig.from_env()  # the second read warns no more
    assert len([r for r in caplog.records if "KNOWLEDGE_RERANK" in r.getMessage()]) == 10


async def test_garbage_env_does_not_take_retrieval_down(monkeypatch):
    from services.knowledge import runtime
    monkeypatch.setenv("KNOWLEDGE_RERANKER", "cohere")
    monkeypatch.setenv("KNOWLEDGE_RERANK_TOP_N", "inf")
    runtime.set_reranker(None)
    rr = runtime.get_reranker()
    assert isinstance(rr, CohereReranker) and rr.config.top_n == 20
    runtime.set_reranker(None)


# --- a warm connection ---------------------------------------------------------------------------------------------------

async def _tls_server(tmp_path):
    """A local TLS server speaking minimal keep-alive HTTP/1.1 that counts accepted connections."""
    import shutil
    import ssl
    import subprocess
    if shutil.which("openssl") is None:
        pytest.skip("openssl not available")
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                    "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                   check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    state = {"connections": 0, "requests": 0}

    async def handle(reader, writer):
        state["connections"] += 1
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":")[1])
                body = await reader.readexactly(length) if length else b""
                state["requests"] += 1
                payload = json.dumps({"results": [{"index": i, "relevance_score": 1.0 - i / 10}
                                                  for i in range(len(json.loads(body)["documents"]))]}).encode() if body else b"{}"
                is_head = head.startswith(b"HEAD ")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: keep-alive\r\n"
                             b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + (b"" if is_head else payload))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
    return server, server.sockets[0].getsockname()[1], cert, state


async def test_the_client_is_kept_warm_so_calls_reuse_one_connection(tmp_path):
    server, port, cert, state = await _tls_server(tmp_path)
    try:
        url = f"https://localhost:{port}/v2/rerank"
        rr = CohereReranker(resolver=_Resolver(), url=url, allowed_hosts=frozenset({"localhost"}), verify=ssl.create_default_context(cafile=str(cert)),
                            config=RerankConfig(http_timeout_s=0.5))
        t0 = time.monotonic()
        await rr.warm()
        warm_up = time.monotonic() - t0
        assert state["connections"] == 1 and state["requests"] == 1  # the warm-up opened it and sent no text
        times = []
        for _ in range(3):
            t0 = time.monotonic()
            out = await rr.rerank(QUERY, _matches(5))
            times.append(time.monotonic() - t0)
            assert [m.chunk_id for m in out] == [f"c{i}" for i in range(5)]
        assert state["connections"] == 1 and state["requests"] == 4  # three calls, zero new handshakes
        cold = CohereReranker(resolver=_Resolver(), url=url, allowed_hosts=frozenset({"localhost"}), verify=ssl.create_default_context(cafile=str(cert)),
                              config=RerankConfig(http_timeout_s=0.5))
        t0 = time.monotonic()
        await cold.rerank(QUERY, _matches(5))
        cold_s = time.monotonic() - t0
        print(f"\nlocal TLS, no network RTT: warm-up/handshake {warm_up * 1000:.1f} ms, cold first call "
              f"{cold_s * 1000:.1f} ms, warm calls {[round(t * 1000, 1) for t in times]} ms")
        assert state["connections"] == 2  # the un-warmed client paid its own connection
        await rr.aclose()
        await cold.aclose()
    finally:
        server.close()
        await server.wait_closed()


# --- one tenant cannot hold the shared breaker open ----------------------------------------------------------------------

async def test_an_attacker_whose_calls_time_out_cannot_switch_rerank_off_for_a_victim():
    async def hang(request):
        await asyncio.sleep(5)

    seen = []

    def healthy(request):
        seen.append(1)
        return _cohere_ok([0.1, 0.9, 0.2])(request)

    attacker = _cohere(hang, config=RerankConfig(http_timeout_s=0.03, tenant_burst=100))
    victim = _cohere(healthy, config=RerankConfig(tenant_burst=100))
    cfg = RetrievalConfig(breaker_failures=2, breaker_cooldown_s=60)
    for _ in range(6):  # more than enough to open a shared breaker, and to keep winning every half-open probe
        degraded = []
        await retrieval._rerank(attacker, QUERY, _matches(3), cfg, 0.4, degraded, {}, "attacker")
    assert resilience.breakers.get("rerank:attacker", cfg).state == "open"
    for _ in range(3):
        degraded = []
        out = await retrieval._rerank(victim, QUERY, _matches(3), cfg, 0.4, degraded, {}, "victim")
        assert degraded == [] and out[0].chunk_id == "c1"
    assert len(seen) == 3  # the provider was reached for every victim call
    assert resilience.rerank_valve.state == "closed"  # one tenant's failures never open the global valve
    resilience.breakers.clear()
    resilience.rerank_valve.clear()


def test_the_platform_valve_opens_only_for_several_distinct_tenants_and_recovers():
    now = [0.0]
    v = resilience.PlatformValve(tenants=3, window_s=30, cooldown_s=20, clock=lambda: now[0])
    for _ in range(50):
        v.record_failure("one-tenant")
    assert v.state == "closed" and v.allow()                      # a single tenant never opens it
    v.record_failure("t2")
    assert v.state == "closed"
    v.record_failure("t3")
    assert v.state == "open" and not v.allow()                    # three distinct tenants: a platform-wide outage
    now[0] += 21
    assert v.allow() and not v.allow()                            # exactly one probe after the cooldown
    v.record_failure("t4")
    assert v.state == "open"                                      # a failed probe re-opens
    now[0] += 21
    assert v.allow()
    v.record_success()
    assert v.state == "closed" and v.allow()


def test_valve_failures_age_out_of_the_window():
    now = [0.0]
    v = resilience.PlatformValve(tenants=2, window_s=10, cooldown_s=5, clock=lambda: now[0])
    v.record_failure("a")
    now[0] += 11
    v.record_failure("b")
    assert v.state == "closed"  # "a" is outside the window: two tenants did not fail together


async def test_a_platform_wide_outage_opens_the_valve_for_every_tenant():
    rr = _cohere(_status(503), config=RerankConfig(tenant_burst=100))
    cfg = RetrievalConfig(breaker_failures=50, breaker_cooldown_s=60)
    for tenant in ("a", "b", "c"):
        await retrieval._rerank(rr, QUERY, _matches(3), cfg, 0.4, [], {}, tenant)
    assert resilience.rerank_valve.state == "open"
    called = []
    healthy = _cohere(lambda r: (called.append(1), _cohere_ok([0.1, 0.2, 0.3])(r))[1])
    degraded = []
    await retrieval._rerank(healthy, QUERY, _matches(3), cfg, 0.4, degraded, {}, "d")
    assert degraded == ["rerank_skipped"] and not called  # fail fast while the vendor is down
    resilience.breakers.clear()
    resilience.rerank_valve.clear()


def test_the_breaker_table_is_bounded_and_evicts_closed_breakers_first():
    reg = resilience.BreakerRegistry()
    reg.MAX_BREAKERS = 10
    cfg = RetrievalConfig(breaker_failures=1)
    reg.get("rerank:open-one", cfg).record_failure()
    for i in range(200):
        reg.get(f"rerank:t{i}", cfg)
    assert len(reg._breakers) <= 11
    assert "rerank:open-one" in reg._breakers  # an open breaker is never evicted to make room


# --- the rate that protects the shared key -----------------------------------------------------------------------------------

def test_tenant_rate_is_derived_from_the_platform_quota_the_process_count_and_the_tenant_share(monkeypatch):
    for k in ("KNOWLEDGE_RERANK_TENANT_RATE", "KNOWLEDGE_RERANK_PLATFORM_RATE", "KNOWLEDGE_RERANK_PROCESSES", "KNOWLEDGE_RERANK_TENANT_SHARE"):
        monkeypatch.delenv(k, raising=False)
    d = RerankConfig.from_env()
    assert d.tenant_rate == pytest.approx(10 * 0.25 / 4) and d.process_rate == pytest.approx(2.5)  # conservative default
    monkeypatch.setenv("KNOWLEDGE_RERANK_PLATFORM_RATE", "100")
    monkeypatch.setenv("KNOWLEDGE_RERANK_PROCESSES", "10")
    monkeypatch.setenv("KNOWLEDGE_RERANK_TENANT_SHARE", "0.5")
    c = RerankConfig.from_env()
    assert c.tenant_rate == pytest.approx(5.0) and c.process_rate == pytest.approx(10.0)
    monkeypatch.setenv("KNOWLEDGE_RERANK_TENANT_RATE", "1.5")  # an explicit value wins
    assert RerankConfig.from_env().tenant_rate == 1.5
    monkeypatch.setenv("KNOWLEDGE_RERANK_TENANT_RATE", "inf")  # garbage: back to the derived value
    assert RerankConfig.from_env().tenant_rate == pytest.approx(5.0)


async def test_all_tenants_together_cannot_exceed_the_process_share_of_the_platform_quota():
    calls = []

    def handler(request):
        calls.append(1)
        return _cohere_ok([0.1, 0.2, 0.3])(request)

    cfg = RerankConfig(tenant_rate=100.0, tenant_burst=100, platform_rate=0.1, processes=1)  # process share: 10 burst
    rr = _cohere(handler, config=cfg)
    reasons = []
    for i in range(40):  # many tenants, each well inside its own budget
        try:
            await rr.rerank(QUERY, _matches(3), tenant_id=f"tenant-{i}")
        except RerankError as exc:
            reasons.append((exc.degraded_reason, exc.breaker_failure))
    assert len(calls) == 10 and reasons == [("rerank_budget", False)] * 30
