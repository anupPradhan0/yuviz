"""Issue #97: a tenant-supplied embedding base_url is an origin, never a URL to fetch, and provider failures come back
as fixed codes. Real sockets (a recording fake server) and a real database; no Ollama."""

from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest

from libs import egress_guard as eg
from libs.egress_guard import UnsafeProviderUrl, validate_base_url
from libs.tenancy import set_caller_tenant
from services.config import provider_configs
from services.knowledge import documents as documents_service
from services.knowledge import embedding_manager as em
from services.knowledge import health
from services.knowledge import ingestion_worker as worker
from services.knowledge.embedding_manager import (
    EmbeddingProviderConfig, EmbeddingProviderManager, OllamaEmbeddingProvider, ProviderUrlRejected,
)
from services.knowledge.secret_resolver import CompositeSecretResolver
from services.knowledge.storage import LocalStorageProvider
from services.knowledge.tests.test_ingestion_queue import (
    _claim_next_job, _drain_pending, _events, _job, _make_kb, _upload,
)

MARKER = "INTERNAL-SECRET-MARKER-7731"
GOOD = {"embedding": [0.1] * 768}


class FakeServer:
    """A recording HTTP/1.1 server. `handler(path) -> (status, body, extra_headers)`; `hang` never answers."""

    def __init__(self, handler=None, hang: bool = False) -> None:
        self.handler = handler or (lambda path: (200, json.dumps(GOOD), {}))
        self.hang = hang
        self.connections = 0
        self.requests: list[tuple[str, dict[str, str]]] = []
        self._server = None
        self.port = 0

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    async def _serve(self, reader, writer):
        self.connections += 1
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode().split("\r\n")
            path = lines[0].split(" ")[1]
            headers = {k.lower(): v for k, v in (ln.split(": ", 1) for ln in lines[1:] if ": " in ln)}
            self.requests.append((path, headers))
            length = int(headers.get("content-length", 0))
            if length:
                await reader.readexactly(length)
            if self.hang:
                await asyncio.sleep(30)
                return
            status, body, extra = self.handler(path)
            out = "".join(f"{k}: {v}\r\n" for k, v in extra.items())
            writer.write(f"HTTP/1.1 {status} {MARKER}-status-text\r\ncontent-type: application/json\r\n{out}"
                         f"content-length: {len(body)}\r\nconnection: close\r\n\r\n{body}".encode())
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()


@pytest.fixture
def allow(monkeypatch):
    """Allow-list the loopback fake server the way an operator would."""
    def _allow(*entries: str) -> None:
        monkeypatch.setenv(eg.ALLOWED_HOSTS_ENV, ",".join(entries))
    return _allow


@pytest.fixture
def short_timeout(monkeypatch):
    monkeypatch.setattr(em, "guarded_client", lambda timeout=30.0: eg.guarded_client(timeout=0.5))


# ── address and URL syntax (no network) ─────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://ollama.example.com:11434", "https://ollama.example.com", "https://ollama.example.com/", "http://8.8.8.8:11434",
    "http://[2606:4700:4700::1111]:11434",
])
def test_public_origins_pass(url):
    assert validate_base_url(url).host


@pytest.mark.parametrize("url", [
    "http://host.example.com:11434/api/pull#", "http://host.example.com:11434/api/pull", "http://host.example.com:11434?x=",
    "http://host.example.com:11434/?x=1", "http://host.example.com:11434/#f", "http://host.example.com/a/b",
    "http://user:pw@host.example.com", "http://user@host.example.com", "ftp://host.example.com", "file:///etc/passwd",
    "gopher://host.example.com", "//host.example.com", "http://", "http:///path", "", "   ", None, 5,
    "http://host.example.com:0", "http://host.example.com:99999", "http://host.example.com:abc",
    "http://host.example.com\\@evil.com", "http://host.example.com\n", "http://ex ample.com", "http://host.example.com./",
    "http://exämple.com", "http://host%2eexample.com", "http://-bad.example.com",
])
def test_paths_queries_fragments_credentials_and_malformed_urls_are_rejected(url):
    with pytest.raises(UnsafeProviderUrl):
        validate_base_url(url)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1", "http://127.1", "http://127.0.0.1:8080", "http://127.0.0.1:11435",
    "http://169.254.169.254/", "http://169.254.169.254:80", "http://10.0.0.5", "http://172.16.0.1", "http://172.31.255.255",
    "http://192.168.1.1", "http://100.64.0.1", "http://100.127.255.254", "http://0.0.0.0", "http://224.0.0.1",
    "http://240.0.0.1", "http://255.255.255.255", "http://192.0.0.1", "http://198.18.0.1", "http://[::1]", "http://[::]",
    "http://[fe80::1]", "http://[fd00::1]", "http://[fc00::1]", "http://[ff02::1]", "http://[::ffff:127.0.0.1]",
    "http://[::ffff:169.254.169.254]", "http://[::ffff:7f00:1]", "http://[64:ff9b::7f00:1]", "http://[2002:7f00:1::]",
    "http://2130706433", "http://0x7f000001", "http://0x7f.0.0.1", "http://017700000001", "http://0177.0.0.1",
    "http://127.000.000.001", "http://1.1", "http://10.1", "http://3232235777", "http://0", "http://[::1%25eth0]",
])
def test_unsafe_address_spellings_are_refused(url):
    with pytest.raises(UnsafeProviderUrl):
        validate_base_url(url)


def test_default_allow_list_is_exactly_the_operators_ollama(monkeypatch):
    monkeypatch.delenv(eg.ALLOWED_HOSTS_ENV, raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    assert validate_base_url("http://127.0.0.1:11434").port == 11434      # dev works with no extra config
    assert validate_base_url("http://localhost:11434").host == "localhost"
    for url in ("http://127.0.0.1:9999", "http://127.0.0.1:6379", "http://10.0.0.5:11434", "http://[::1]:11434"):
        with pytest.raises(UnsafeProviderUrl):
            validate_base_url(url)


def test_default_follows_the_operators_ollama_base_url(monkeypatch):
    monkeypatch.delenv(eg.ALLOWED_HOSTS_ENV, raising=False)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://10.9.9.9:11434")
    assert validate_base_url("http://10.9.9.9:11434").host == "10.9.9.9"
    with pytest.raises(UnsafeProviderUrl):
        validate_base_url("http://127.0.0.1:11434")


def test_operator_allow_list_entries_allow_host_or_host_port(monkeypatch):
    monkeypatch.setenv(eg.ALLOWED_HOSTS_ENV, "10.0.0.5:11434, 10.0.0.6 ,ollama.internal:11434")
    assert validate_base_url("http://10.0.0.5:11434").port == 11434
    assert validate_base_url("http://10.0.0.6:1234").port == 1234          # host-only entry: any port
    with pytest.raises(UnsafeProviderUrl):
        validate_base_url("http://10.0.0.5:11435")
    with pytest.raises(UnsafeProviderUrl):
        validate_base_url("http://127.0.0.1:11434")                        # setting the variable replaces the default
    monkeypatch.setenv(eg.ALLOWED_HOSTS_ENV, "")
    with pytest.raises(UnsafeProviderUrl):
        validate_base_url("http://127.0.0.1:11434")


# ── connect time: real sockets ──────────────────────────────────────────

async def _embed(base_url: str, texts=("hello",)):
    return await OllamaEmbeddingProvider(model="m", base_url=base_url).embed(list(texts))


async def test_the_path_is_always_api_embeddings(allow):
    async with FakeServer() as srv:
        allow(f"127.0.0.1:{srv.port}")
        for suffix in ("", "/"):
            assert len(await _embed(f"http://127.0.0.1:{srv.port}{suffix}")) == 1
        assert [p for p, _ in srv.requests] == ["/api/embeddings", "/api/embeddings"]


@pytest.mark.parametrize("suffix", ["/api/pull#", "/api/pull", "?x=", "/api/pull?x=", "#", "/api/pull#/api/embeddings"])
async def test_the_fragment_and_query_tricks_never_reach_another_path(allow, suffix):
    async with FakeServer() as srv:
        allow(f"127.0.0.1:{srv.port}")
        with pytest.raises(ProviderUrlRejected):
            await _embed(f"http://127.0.0.1:{srv.port}{suffix}")
        assert srv.requests == [] and srv.connections == 0


@pytest.mark.parametrize("spelling", [
    "2130706433", "0x7f000001", "0x7f.0.0.1", "017700000001", "0177.0.0.1", "127.1", "127.000.000.001", "[::1]",
    "[::ffff:127.0.0.1]", "[::ffff:7f00:1]", "0", "0.0.0.0", "[::]",
])
async def test_every_loopback_spelling_is_refused_before_any_connection(spelling):
    async with FakeServer() as srv:  # not allow-listed; it listens on 127.0.0.1 AND would answer if reached
        with pytest.raises(ProviderUrlRejected):
            await _embed(f"http://{spelling}:{srv.port}")
        assert srv.connections == 0


async def test_a_hostname_that_resolves_to_a_private_address_is_refused(monkeypatch):
    async def resolver(host, port):
        return ["127.0.0.1"]
    monkeypatch.setattr(eg, "_resolve", resolver)
    async with FakeServer() as srv:
        with pytest.raises(em.ProviderTransientError) as e:
            await _embed(f"http://innocent.example.com:{srv.port}")
        assert str(e.value) == "provider_unreachable" and srv.connections == 0


@pytest.mark.parametrize("addresses", [
    ["93.184.216.34", "127.0.0.1"], ["127.0.0.1", "93.184.216.34"], ["93.184.216.34", "169.254.169.254"],
    ["93.184.216.34", "::ffff:10.0.0.1"], ["93.184.216.34", "fd00::1"],
])
async def test_a_host_with_any_private_address_is_refused_whatever_the_order(monkeypatch, addresses):
    async def resolver(host, port):
        return addresses
    monkeypatch.setattr(eg, "_resolve", resolver)
    async with FakeServer() as srv:
        with pytest.raises(em.ProviderTransientError) as e:
            await _embed(f"http://rebind.example.com:{srv.port}")
        assert str(e.value) == "provider_unreachable" and srv.connections == 0


async def test_dns_rebinding_cannot_swap_the_address_after_validation(monkeypatch, allow):
    """First answer is the allow-listed server; every later answer is an internal one. The connection uses the first."""
    async with FakeServer() as srv:
        allow(f"pinned.example:{srv.port}")
        calls = []

        async def resolver(host, port):
            calls.append(host)
            return ["127.0.0.1"] if len(calls) == 1 else ["10.9.9.9"]
        monkeypatch.setattr(eg, "_resolve", resolver)
        assert len(await _embed(f"http://pinned.example:{srv.port}")) == 1
        assert len(calls) == 1  # one resolution per connection: the HTTP client never resolves a second time
        path, headers = srv.requests[0]
        assert path == "/api/embeddings" and headers["host"] == f"pinned.example:{srv.port}"


async def test_a_redirect_to_an_internal_host_is_not_followed(allow):
    async with FakeServer() as internal:
        async with FakeServer(lambda path: (302, "", {"location": f"http://127.0.0.1:{internal.port}/api/pull"})) as srv:
            allow(f"127.0.0.1:{srv.port}")
            with pytest.raises(em.ProviderPermanentError) as e:
                await _embed(f"http://127.0.0.1:{srv.port}")
            assert str(e.value) == "provider_error"
            assert internal.connections == 0 and [p for p, _ in srv.requests] == ["/api/embeddings"]


async def test_an_allow_listed_endpoint_works_and_a_neighbouring_port_does_not(allow):
    async with FakeServer() as srv, FakeServer() as other:
        allow(f"127.0.0.1:{srv.port}")
        assert len(await _embed(f"http://127.0.0.1:{srv.port}")) == 1
        with pytest.raises(ProviderUrlRejected):
            await _embed(f"http://127.0.0.1:{other.port}")
        assert other.connections == 0


async def test_the_default_dev_endpoint_needs_no_configuration(monkeypatch):
    """No env at all: localhost:11434 is allowed. Uses the real port when it is free, else is skipped."""
    monkeypatch.delenv(eg.ALLOWED_HOSTS_ENV, raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 11434))
    except OSError:
        pytest.skip("11434 is in use (a real Ollama); the default is covered by test_default_allow_list_*")
    probe.close()
    srv = FakeServer()
    server = await asyncio.start_server(srv._serve, "127.0.0.1", 11434)
    try:
        assert len(await _embed("http://localhost:11434")) == 1
        assert srv.requests[0][0] == "/api/embeddings"
    finally:
        server.close()
        await server.wait_closed()


async def test_the_operators_ollama_url_is_allowed_by_name_without_the_list(monkeypatch):
    """The default allow-list is derived from OLLAMA_BASE_URL; `localhost` resolves for real (v4 and v6) and still connects."""
    monkeypatch.delenv(eg.ALLOWED_HOSTS_ENV, raising=False)
    async with FakeServer() as srv:
        monkeypatch.setenv("OLLAMA_BASE_URL", f"http://localhost:{srv.port}")
        assert len(await _embed(f"http://localhost:{srv.port}")) == 1
        assert srv.requests[0][0] == "/api/embeddings" and srv.requests[0][1]["host"] == f"localhost:{srv.port}"


# ── fixed error codes ───────────────────────────────────────────────────

_CASES = [
    (401, "provider_auth_failed", "permanent"), (403, "provider_auth_failed", "permanent"),
    (429, "provider_rate_limited", "transient"), (500, "provider_error", "transient"),
    (503, "provider_error", "transient"), (404, "provider_error", "permanent"), (400, "provider_error", "permanent"),
    (408, "provider_error", "transient"),
]


@pytest.mark.parametrize(("status", "code", "klass"), _CASES)
async def test_http_failures_become_fixed_codes(allow, status, code, klass):
    body = json.dumps({"error": f"{MARKER} model not found at http://10.1.2.3:11434/api/pull"})
    async with FakeServer(lambda path: (status, body, {})) as srv:
        allow(f"127.0.0.1:{srv.port}")
        with pytest.raises(em.ProviderError) as e:
            await em.embed_in_batches(OllamaEmbeddingProvider("m", f"http://127.0.0.1:{srv.port}"), ["x"], retries=0)
        assert str(e.value) == code and worker.classify_error(e.value) == klass
        assert worker.stored_error(e.value) == code
        assert MARKER not in repr(e.value) and "10.1.2.3" not in repr(e.value) and str(srv.port) not in repr(e.value)


@pytest.mark.parametrize("body", ["not json " + MARKER, "[1,2,3]", json.dumps({"other": MARKER}), json.dumps({"embedding": MARKER})])
async def test_garbage_responses_are_provider_bad_response(allow, body):
    async with FakeServer(lambda path: (200, body, {})) as srv:
        allow(f"127.0.0.1:{srv.port}")
        with pytest.raises(em.ProviderError) as e:
            await em.embed_in_batches(OllamaEmbeddingProvider("m", f"http://127.0.0.1:{srv.port}"), ["x"], retries=0)
        assert str(e.value) == "provider_bad_response" and worker.classify_error(e.value) == "transient"


async def test_refused_connect_and_timeout_are_indistinguishable(allow, short_timeout):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    probe.close()
    async with FakeServer(hang=True) as hung:
        allow(f"127.0.0.1:{closed_port}", f"127.0.0.1:{hung.port}")
        seen = []
        for port in (closed_port, hung.port):
            with pytest.raises(em.ProviderError) as e:
                await em.embed_in_batches(OllamaEmbeddingProvider("m", f"http://127.0.0.1:{port}"), ["x"], retries=0)
            seen.append((type(e.value), str(e.value), e.value.__cause__, e.value.__suppress_context__))
    assert seen[0] == seen[1] == (em.ProviderTransientError, "provider_unreachable", None, True)


async def test_tls_failure_is_unreachable(allow, short_timeout):
    async with FakeServer() as srv:  # speaks plain HTTP, the client expects TLS
        allow(f"127.0.0.1:{srv.port}")
        with pytest.raises(em.ProviderError) as e:
            await em.embed_in_batches(OllamaEmbeddingProvider("m", f"https://127.0.0.1:{srv.port}"), ["x"], retries=0)
        assert str(e.value) == "provider_unreachable"


def test_provider_error_leaves_other_exceptions_alone():
    assert em.provider_error(ValueError("x")) is None
    assert em.provider_error(httpx.ConnectTimeout("t")).code == "provider_unreachable"
    assert worker.stored_error(ValueError("plain")) == "ValueError: plain"  # non-provider errors keep redact_error


# ── what is stored and what the tenant can read back ────────────────────

async def _ingest_against(tenant, pool, srv_port: int, slug: str):
    await _drain_pending(pool)
    set_caller_tenant(str(tenant["id"]))
    cfg = await provider_configs.create_provider_config(
        tenant_id=tenant["id"], name=f"E-{slug}", role="embedding", engine="ollama",
        extra={"base_url": f"http://127.0.0.1:{srv_port}"}, allow_pointer_schemes=False,
    )
    from services.knowledge import knowledge_bases as kb_service
    kb = await kb_service.create_knowledge_base(
        tenant_id=tenant["id"], slug=slug, name=slug, embedding_config_id=cfg["id"])
    doc = await _upload(tenant, kb, slug)
    job = await _claim_next_job(pool, tenant_cap=100)
    manager = EmbeddingProviderManager(CompositeSecretResolver())
    await worker.process_one_job(pool, LocalStorageProvider(), manager, job)
    return doc


@pytest.mark.parametrize(("status", "code"), [(401, "provider_auth_failed"), (429, "provider_rate_limited"),
                                               (500, "provider_error"), (404, "provider_error")])
async def test_nothing_from_the_target_reaches_stored_errors_events_or_health(
        tenant_agent, pool, allow, monkeypatch, status, code):
    monkeypatch.setattr(em, "EMBED_RETRY_BASE_S", 0.0)
    tenant, _ = tenant_agent
    body = json.dumps({"error": f"{MARKER} at http://10.1.2.3:11434/api/pull"})
    async with FakeServer(lambda path: (status, body, {})) as srv:
        allow(f"127.0.0.1:{srv.port}")
        doc = await _ingest_against(tenant, pool, srv.port, f"leak-{status}")
        job = await _job(pool, doc["ingestion_job_id"])
        stored = [job["error"], (await documents_service.get_document(doc["id"]))["error"]]
        stored += [json.dumps(e["detail"]) for e in await _events(pool, doc["ingestion_job_id"])]
        set_caller_tenant(str(tenant["id"]))
        stored.append(json.dumps(await health.tenant_health(), default=str))
        stored.append(json.dumps(await health.document_events(doc["id"]), default=str))
    blob = " ".join(str(t) for t in stored)
    assert MARKER not in blob and "10.1.2.3" not in blob and f"127.0.0.1:{srv.port}" not in blob
    assert job["error"] == code
    assert str(status) not in (job["error"] or "")


async def test_a_refused_target_is_stored_as_unreachable_not_as_a_guard_message(tenant_agent, pool, monkeypatch, allow):
    monkeypatch.setattr(em, "EMBED_RETRY_BASE_S", 0.0)
    tenant, _ = tenant_agent
    async with FakeServer() as srv:
        allow(f"127.0.0.1:{srv.port}")
        await _drain_pending(pool)
        set_caller_tenant(str(tenant["id"]))
        cfg = await provider_configs.create_provider_config(
            tenant_id=tenant["id"], name="E-resolve", role="embedding", engine="ollama",
            extra={"base_url": f"http://internal-name.example.com:{srv.port}"}, allow_pointer_schemes=False)
        from services.knowledge import knowledge_bases as kb_service
        kb = await kb_service.create_knowledge_base(
            tenant_id=tenant["id"], slug="e-resolve", name="e", embedding_config_id=cfg["id"])
        doc = await _upload(tenant, kb, "resolve")

        async def resolver(host, port):
            return ["127.0.0.1"]  # a name that resolves to an internal address, and is not allow-listed
        monkeypatch.setattr(eg, "_resolve", resolver)
        job = await _claim_next_job(pool, tenant_cap=100)
        await worker.process_one_job(pool, LocalStorageProvider(), EmbeddingProviderManager(CompositeSecretResolver()), job)
        assert srv.connections == 0
    assert (await _job(pool, doc["ingestion_job_id"]))["error"] == "provider_unreachable"


async def test_an_unsafe_config_already_in_the_database_fails_with_the_url_code(tenant_agent, pool):
    """Rows written before the Config service check existed (or by a superuser): the client constructor refuses them."""
    tenant, _ = tenant_agent
    cfg = EmbeddingProviderConfig(id="c1", engine="ollama", extra={"base_url": "http://169.254.169.254/latest/meta-data/"})
    with pytest.raises(ProviderUrlRejected) as e:
        await EmbeddingProviderManager(CompositeSecretResolver()).get(cfg)
    assert str(e.value) == "provider_url_invalid" and worker.classify_error(e.value) == "permanent"
