"""Status and downloads of the free local models in LOCAL_MODELS, on this host.

Jobs are in-memory and per-process. Ollama pulls stream over httpx in a task; Hugging Face
downloads run in a killable subprocess (this module's __main__) so they stay off the event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
from dataclasses import dataclass

import httpx
from huggingface_hub import hf_hub_download
from huggingface_hub.errors import LocalEntryNotFoundError

from libs.config_sdk.local_models import LOCAL_MODELS, LocalModel, find_local_model

log = logging.getLogger(__name__)

_MAX_CONCURRENT_DOWNLOADS = 2
_KILL_AFTER_S = 5
_OLLAMA_PULL_TIMEOUT_S = 3600  # a stalled pull must not hold its download slot forever
_KOKORO_REPO = "hexgrad/Kokoro-82M"


class DownloadCapReached(Exception):
    """Too many downloads are already running on this host."""


class _OllamaPullError(Exception):
    """Ollama answered the pull with an `error` (its text is safe to show the admin)."""


@dataclass
class _Job:
    status: str = "downloading"  # downloading | failed
    error: str | None = None
    proc: asyncio.subprocess.Process | None = None
    task: asyncio.Task | None = None


def _kokoro_files(voice: str) -> tuple[str, ...]:
    return ("config.json", "kokoro-v1_0.pth", f"voices/{voice}.pt")


def _hf_argv(model: LocalModel) -> tuple[str, ...]:
    return (sys.executable, "-m", "services.config.model_downloads", model.engine, model.name)


def _whisper_on_disk(name: str) -> bool:
    from faster_whisper.utils import download_model

    try:
        download_model(name, local_files_only=True)
    except LocalEntryNotFoundError:
        return False
    return True


def _kokoro_on_disk(voice: str) -> bool:
    try:
        for filename in _kokoro_files(voice):
            hf_hub_download(_KOKORO_REPO, filename, local_files_only=True)
    except LocalEntryNotFoundError:
        return False
    return True


class ModelDownloads:
    def __init__(self, *, ollama_url: str) -> None:
        self._http = httpx.AsyncClient(base_url=ollama_url, timeout=10.0)
        self._jobs: dict[tuple[str, str], _Job] = {}

    async def status(self) -> list[dict]:
        ollama_tags = await self._ollama_tags()
        out = []
        for m in LOCAL_MODELS:
            state, error = await self._state(m, ollama_tags)
            out.append({
                "engine": m.engine, "role": m.role, "name": m.name, "label": m.label,
                "languages": None if m.languages is None else sorted(m.languages),
                "status": state, "error": error,
            })
        return out

    async def trigger(self, engine: str, name: str) -> str:
        model = find_local_model(engine, name)
        if model is None or engine == "macos":
            raise LookupError("not a downloadable local model")
        key = (engine, name)
        if self._is_running(key):
            return "downloading"
        if await self._on_disk(model):
            return "ready"
        # No await from here to the insert: a concurrent trigger that won the disk-check race
        # is visible now, and two triggers cannot both pass a full cap.
        if self._is_running(key):
            return "downloading"
        if sum(j.status == "downloading" for j in self._jobs.values()) >= _MAX_CONCURRENT_DOWNLOADS:
            raise DownloadCapReached("too many downloads are running; try again shortly")
        job = self._jobs[key] = _Job()
        self._start(job, model)
        return "downloading"

    async def aclose(self) -> None:
        jobs = list(self._jobs.values())
        await asyncio.gather(*(self._stop(j.proc) for j in jobs if j.proc is not None))
        tasks = [j.task for j in jobs if j.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._http.aclose()

    def _is_running(self, key: tuple[str, str]) -> bool:
        job = self._jobs.get(key)
        return job is not None and job.status == "downloading"

    def _start(self, job: _Job, model: LocalModel) -> None:
        job.task = asyncio.create_task(self._run(job, model))

    async def _run(self, job: _Job, model: LocalModel) -> None:
        key = (model.engine, model.name)
        try:
            if model.engine == "ollama":
                await asyncio.wait_for(self._pull_ollama(model.name), _OLLAMA_PULL_TIMEOUT_S)
            else:
                code = await self._download_hf(job, model)
                if code:
                    log.error("local model download exited %s engine=%s name=%s", code, model.engine, model.name)
                    job.status, job.error = "failed", f"exit {code}"
                    return
        except Exception as exc:
            log.exception("local model download failed engine=%s name=%s", model.engine, model.name)
            job.status = "failed"
            job.error = (
                f"ollama pull failed: {exc}" if isinstance(exc, _OllamaPullError)
                else f"download failed ({type(exc).__name__})"
            )
            return
        del self._jobs[key]  # on disk now; status() reads the disk from here on

    async def _download_hf(self, job: _Job, model: LocalModel) -> int:
        job.proc = await asyncio.create_subprocess_exec(*_hf_argv(model))
        return await job.proc.wait()

    async def _pull_ollama(self, name: str) -> None:
        async with self._http.stream("POST", "/api/pull", json={"model": name}, timeout=None) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if error := json.loads(line).get("error"):
                    raise _OllamaPullError(error)

    @staticmethod
    async def _stop(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), _KILL_AFTER_S)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    async def _ollama_tags(self) -> set[str]:
        try:
            resp = await self._http.get("/api/tags")
            resp.raise_for_status()
        except httpx.HTTPError:
            return set()  # Ollama down: nothing is pulled as far as this host can tell
        return {m["name"] for m in resp.json().get("models", [])}

    async def _on_disk(self, model: LocalModel, ollama_tags: set[str] | None = None) -> bool:
        if model.engine == "faster_whisper":
            return await asyncio.to_thread(_whisper_on_disk, model.name)
        if model.engine == "kokoro":
            return await asyncio.to_thread(_kokoro_on_disk, model.name)
        if model.engine == "ollama":
            tags = await self._ollama_tags() if ollama_tags is None else ollama_tags
            return model.name in tags or f"{model.name}:latest" in tags
        return await asyncio.to_thread(shutil.which, "say") is not None  # macos

    async def _state(self, model: LocalModel, ollama_tags: set[str]) -> tuple[str, str | None]:
        job = self._jobs.get((model.engine, model.name))
        if job is not None and job.status == "downloading":
            return "downloading", None
        if await self._on_disk(model, ollama_tags):
            return "ready", None
        if model.engine == "macos":
            return "unavailable", None
        if job is not None:
            return "failed", job.error
        return "missing", None


def _main(engine: str, name: str) -> None:
    model = find_local_model(engine, name)
    if model is None:
        raise SystemExit(f"unknown local model {engine}/{name}")
    if engine == "faster_whisper":
        from faster_whisper.utils import download_model

        download_model(name)
    elif engine == "kokoro":
        for filename in _kokoro_files(name):
            hf_hub_download(_KOKORO_REPO, filename)
    else:
        raise SystemExit(f"{engine} is not downloaded by subprocess")


if __name__ == "__main__":
    _main(*sys.argv[1:])
