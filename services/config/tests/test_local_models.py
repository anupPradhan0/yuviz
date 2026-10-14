"""ModelDownloads job rules and the /local-models routes. Disk, Ollama and process spawning are
stubbed: nothing here downloads a model."""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from services.config import auth
from services.config import users as users_service
from services.config.app import app
from services.config.model_downloads import DownloadCapReached, ModelDownloads


@pytest_asyncio.fixture(loop_scope="session")
async def downloads(monkeypatch):
    """Nothing on disk, and `_start` counts spawns instead of running them."""
    d = ModelDownloads(ollama_url="http://ollama.invalid")
    d.spawned = []

    async def not_on_disk(model, ollama_tags=None):
        await asyncio.sleep(0)  # yield, as the real thread-pool check does
        return False

    async def no_tags():
        return set()

    monkeypatch.setattr(d, "_on_disk", not_on_disk)
    monkeypatch.setattr(d, "_ollama_tags", no_tags)
    monkeypatch.setattr(d, "_start", lambda job, model: d.spawned.append(model.name))
    yield d
    await d.aclose()


def mark_ready(monkeypatch, d: ModelDownloads) -> None:
    async def on_disk(model, ollama_tags=None):
        await asyncio.sleep(0)
        return True

    monkeypatch.setattr(d, "_on_disk", on_disk)


async def test_ready_model_spawns_nothing(downloads, monkeypatch):
    mark_ready(monkeypatch, downloads)
    assert await downloads.trigger("faster_whisper", "tiny") == "ready"
    assert downloads.spawned == []


@pytest.mark.parametrize("engine,name", [("faster_whisper", "no-such"), ("nope", "tiny"), ("macos", "Samantha")])
async def test_unknown_or_macos_pair_is_a_lookup_error(downloads, engine, name):
    with pytest.raises(LookupError):
        await downloads.trigger(engine, name)
    assert downloads.spawned == []


async def test_any_official_kokoro_voice_is_triggerable(downloads):
    assert await downloads.trigger("kokoro", "af_heart") == "downloading"
    assert downloads.spawned == ["af_heart"]


async def test_second_trigger_while_running_spawns_once(downloads):
    assert await downloads.trigger("faster_whisper", "tiny") == "downloading"
    assert await downloads.trigger("faster_whisper", "tiny") == "downloading"
    assert downloads.spawned == ["tiny"]


async def test_concurrent_triggers_of_one_model_spawn_once(downloads):
    results = await asyncio.gather(*(downloads.trigger("faster_whisper", "tiny") for _ in range(2)))
    assert results == ["downloading", "downloading"]
    assert downloads.spawned == ["tiny"]


async def test_concurrent_distinct_triggers_cannot_both_pass_a_full_cap(downloads):
    await downloads.trigger("faster_whisper", "tiny")
    results = await asyncio.gather(
        downloads.trigger("faster_whisper", "base"),
        downloads.trigger("faster_whisper", "small"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, DownloadCapReached) for r in results) == 1
    assert len(downloads.spawned) == 2


async def test_trigger_at_the_cap_leaves_running_jobs_untouched(downloads):
    await downloads.trigger("faster_whisper", "tiny")
    await downloads.trigger("faster_whisper", "base")
    jobs_before = dict(downloads._jobs)
    with pytest.raises(DownloadCapReached):
        await downloads.trigger("faster_whisper", "small")
    assert downloads._jobs == jobs_before
    assert all(j.status == "downloading" for j in jobs_before.values())
    assert downloads.spawned == ["tiny", "base"]


async def test_failed_job_reports_exit_code_without_paths(monkeypatch):
    d = ModelDownloads(ollama_url="http://ollama.invalid")

    async def not_on_disk(model, ollama_tags=None):
        return False

    async def no_tags():
        return set()

    async def exits_3(job, model):
        return 3

    monkeypatch.setattr(d, "_on_disk", not_on_disk)
    monkeypatch.setattr(d, "_ollama_tags", no_tags)
    monkeypatch.setattr(d, "_download_hf", exits_3)
    await d.trigger("faster_whisper", "tiny")
    await d._jobs[("faster_whisper", "tiny")].task
    entry = next(e for e in await d.status() if (e["engine"], e["name"]) == ("faster_whisper", "tiny"))
    assert (entry["status"], entry["error"]) == ("failed", "exit 3")
    await d.aclose()


async def test_failed_pull_reports_only_the_exception_class(monkeypatch):
    d = ModelDownloads(ollama_url="http://ollama.invalid")

    async def not_on_disk(model, ollama_tags=None):
        return False

    async def no_tags():
        return set()

    async def boom(name):
        raise OSError("/home/someone/.ollama/secret")

    monkeypatch.setattr(d, "_on_disk", not_on_disk)
    monkeypatch.setattr(d, "_ollama_tags", no_tags)
    monkeypatch.setattr(d, "_pull_ollama", boom)
    await d.trigger("ollama", "llama3.2")
    await d._jobs[("ollama", "llama3.2")].task
    entry = next(e for e in await d.status() if (e["engine"], e["name"]) == ("ollama", "llama3.2"))
    assert entry["error"] == "download failed (OSError)"
    await d.aclose()


async def test_stalled_pull_times_out_and_frees_its_slot(monkeypatch):
    d = ModelDownloads(ollama_url="http://ollama.invalid")

    async def not_on_disk(model, ollama_tags=None):
        return False

    async def no_tags():
        return set()

    async def stalls(name):
        await asyncio.Event().wait()

    monkeypatch.setattr(d, "_on_disk", not_on_disk)
    monkeypatch.setattr(d, "_ollama_tags", no_tags)
    monkeypatch.setattr(d, "_pull_ollama", stalls)
    monkeypatch.setattr("services.config.model_downloads._OLLAMA_PULL_TIMEOUT_S", 0.01)
    await d.trigger("ollama", "llama3.2")
    await d._jobs[("ollama", "llama3.2")].task
    entry = next(e for e in await d.status() if (e["engine"], e["name"]) == ("ollama", "llama3.2"))
    assert (entry["status"], entry["error"]) == ("failed", "download failed (TimeoutError)")
    await d.aclose()


async def test_hf_subprocess_argv_comes_from_the_registry_entry(monkeypatch):
    d = ModelDownloads(ollama_url="http://ollama.invalid")
    seen = []

    class FakeProc:
        async def wait(self):
            return 0

    async def fake_exec(*argv):
        seen.append(argv)
        return FakeProc()

    async def not_on_disk(model, ollama_tags=None):
        return False

    monkeypatch.setattr(d, "_on_disk", not_on_disk)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await d.trigger("faster_whisper", "tiny")
    await d._jobs[("faster_whisper", "tiny")].task
    assert seen == [(sys.executable, "-m", "services.config.model_downloads", "faster_whisper", "tiny")]
    await d.aclose()


async def test_aclose_ends_a_live_subprocess_within_the_kill_window(monkeypatch):
    d = ModelDownloads(ollama_url="http://ollama.invalid")

    async def not_on_disk(model, ollama_tags=None):
        return False

    monkeypatch.setattr(d, "_on_disk", not_on_disk)
    monkeypatch.setattr("services.config.model_downloads._hf_argv", lambda model: ("sleep", "30"))
    await d.trigger("faster_whisper", "tiny")
    job = d._jobs[("faster_whisper", "tiny")]
    while job.proc is None:
        await asyncio.sleep(0.01)
    started = time.monotonic()
    await d.aclose()
    assert time.monotonic() - started < 5
    assert job.proc.returncode is not None
    assert job.task.done()


# ---- routes ----

@pytest_asyncio.fixture(loop_scope="session")
async def routed(downloads):
    app.state.model_downloads = downloads
    yield downloads
    del app.state.model_downloads


def client_for(token: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


async def test_post_unknown_pair_is_400_with_no_job(routed, test_admin):
    async with client_for(test_admin["token"]) as c:
        resp = await c.post("/local-models/downloads", json={"engine": "faster_whisper", "name": "no-such"})
    assert resp.status_code == 400
    assert routed._jobs == {}


async def test_post_new_download_is_202_and_ready_is_200(routed, test_admin, monkeypatch):
    async with client_for(test_admin["token"]) as c:
        resp = await c.post("/local-models/downloads", json={"engine": "faster_whisper", "name": "tiny"})
        assert (resp.status_code, resp.json()) == (202, {"status": "downloading"})
        mark_ready(monkeypatch, routed)
        resp = await c.post("/local-models/downloads", json={"engine": "faster_whisper", "name": "base"})
        assert (resp.status_code, resp.json()) == (200, {"status": "ready"})
    assert routed.spawned == ["tiny"]


async def test_post_at_the_cap_is_429(routed, test_admin):
    await routed.trigger("faster_whisper", "tiny")
    await routed.trigger("faster_whisper", "base")
    async with client_for(test_admin["token"]) as c:
        resp = await c.post("/local-models/downloads", json={"engine": "faster_whisper", "name": "small"})
    assert resp.status_code == 429
    assert routed.spawned == ["tiny", "base"]


async def test_viewer_cannot_post(routed, test_viewer):
    async with client_for(test_viewer["token"]) as c:
        resp = await c.post("/local-models/downloads", json={"engine": "faster_whisper", "name": "tiny"})
    assert resp.status_code == 403
    assert routed._jobs == {}


@pytest_asyncio.fixture(loop_scope="session")
async def other_tenant_admin(pool):
    slug = f"test-{uuid.uuid4().hex[:8]}"
    tenant = await pool.fetchrow(
        "INSERT INTO tenants (name, slug) VALUES ($1, $2) RETURNING *", f"Other {slug}", slug,
    )
    user = await users_service.create_user(
        email=f"test-admin-{uuid.uuid4().hex[:8]}@example.com", password="test-password-not-real",
        role="admin", tenant_id=tenant["id"],
    )
    yield {"tenant": dict(tenant), "token": auth.create_access_token(user)}
    await pool.execute("DELETE FROM users WHERE id = $1", user["id"])
    await pool.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


async def test_get_is_identical_across_tenants_and_names_no_tenant(
    routed, test_admin, test_tenant, other_tenant_admin,
):
    async with client_for(test_admin["token"]) as a, client_for(other_tenant_admin["token"]) as b:
        resp_a, resp_b = await a.get("/local-models"), await b.get("/local-models")
    assert resp_a.status_code == resp_b.status_code == 200
    assert resp_a.text == resp_b.text
    for tenant in (test_tenant, other_tenant_admin["tenant"]):
        assert str(tenant["id"]) not in resp_a.text and tenant["slug"] not in resp_a.text
    assert {"engine", "role", "name", "label", "languages", "status", "error"} == set(resp_a.json()[0])


async def test_failed_pull_surfaces_ollamas_own_error_and_logs(monkeypatch, caplog):
    d = ModelDownloads(ollama_url="http://ollama.invalid")

    async def not_on_disk(model, ollama_tags=None):
        return False

    async def no_tags():
        return set()

    class _Resp:
        def raise_for_status(self):
            pass

        async def aiter_lines(self):
            yield json.dumps({"error": "pull model manifest: file does not exist"})

    class _Stream:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(d, "_on_disk", not_on_disk)
    monkeypatch.setattr(d, "_ollama_tags", no_tags)
    monkeypatch.setattr(d._http, "stream", lambda *a, **kw: _Stream())
    with caplog.at_level("ERROR", logger="services.config.model_downloads"):
        await d.trigger("ollama", "llama3.2")
        await d._jobs[("ollama", "llama3.2")].task
    entry = next(e for e in await d.status() if (e["engine"], e["name"]) == ("ollama", "llama3.2"))
    assert entry["error"] == "ollama pull failed: pull model manifest: file does not exist"
    assert any("engine=ollama name=llama3.2" in r.getMessage() for r in caplog.records)
    await d.aclose()
