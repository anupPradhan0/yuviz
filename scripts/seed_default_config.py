#!/usr/bin/env python3
"""
Seeds the 'default' tenant with a 'default' agent and matching STT/LLM/TTS
provider_configs, so the Config Service has something real to resolve for a
local/dev call (see services/conversation/agent_resolver.py). Idempotent —
safe to run more than once.

Content mirrors today's fallback path (config/agents/default.yaml +
PipelineConfig's defaults in services/conversation/pipeline_config.py) so
seeding this does not change what a local call sounds like — it just moves
where that configuration comes from.

Goes through the Config Service's own audited write path (services.config.*),
not raw SQL, so these writes show up in audit_log like any real admin edit.

Usage: python3 scripts/seed_default_config.py
Requires: POSTGRES_DSN, REDIS_URL (see services/config/db.py, cache.py)
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.config import agents, provider_configs, tenants  # noqa: E402

TENANT_SLUG = "default"
AGENT_SLUG = "default"

# Matches config/agents/default.yaml.
GREETING = "Hello! How can I help you today?"
SYSTEM_PROMPT = (
    "You are a helpful voice assistant on a phone call. Answer in at most 2-3 short "
    "spoken sentences. Never enumerate long lists; give the single most likely "
    "answer and offer to go deeper if the caller wants. Plain conversational "
    "speech only - no markdown, no bullet points, no numbered lists."
)

# Ollama as seen by the Conversation Service, which under docker-compose is a
# container where "localhost" is itself. Read back from provider_configs.extra
# by ai_provider_manager.py at call time.
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

# Matches PipelineConfig's defaults (services/conversation/pipeline_config.py).
PROVIDER_DEFAULTS = [
    {"role": "stt", "engine": "faster_whisper", "name": "FasterWhisper (default)",
     "model": "small", "extra": {"device": "cpu", "compute_type": "int8"}},
    {"role": "llm", "engine": "ollama", "name": "Ollama llama3.2 (default)",
     "model": "llama3.2", "extra": {"temperature": 0.7, "base_url": OLLAMA_BASE_URL}},
    # Kokoro, not macOS's `say` — found live 2026-08-04 while auditing
    # fork-reproducibility: `engine="macos"` shells out to a macOS-only
    # binary and silently can't work at all on Linux (or even reliably as
    # a "default" on a fresh Mac). Kokoro is the local, cross-platform
    # engine every real demo tenant in this project actually uses; its
    # model downloads from Hugging Face on first use instead of depending
    # on the host OS.
    {"role": "tts", "engine": "kokoro", "name": "Kokoro TTS (default)",
     "voice": "af_sarah", "extra": {"lang_code": "a"}},
]


async def main() -> None:
    tenant = await tenants.get_tenant(TENANT_SLUG)
    if tenant is None:
        raise RuntimeError(
            f"tenant {TENANT_SLUG!r} not found — apply database/schema.sql first "
            "(it seeds this row)."
        )

    provider_ids: dict[str, str] = {}
    for spec in PROVIDER_DEFAULTS:
        existing = await provider_configs.list_provider_configs(tenant["id"], role=spec["role"])
        match = next((p for p in existing if p["engine"] == spec["engine"]), None)
        if match is not None:
            provider_ids[spec["role"]] = match["id"]
            print(f"provider_config already exists: {spec['role']}/{spec['engine']} ({match['id']})")
            continue
        created = await provider_configs.create_provider_config(
            tenant_id=tenant["id"], name=spec["name"], role=spec["role"], engine=spec["engine"],
            model=spec.get("model"), voice=spec.get("voice"), extra=spec.get("extra"),
        )
        provider_ids[spec["role"]] = created["id"]
        print(f"created provider_config: {spec['role']}/{spec['engine']} ({created['id']})")

    defaults_to_set = {
        "default_stt_config_id": provider_ids["stt"],
        "default_llm_config_id": provider_ids["llm"],
        "default_tts_config_id": provider_ids["tts"],
    }
    if any(tenant.get(k) != v for k, v in defaults_to_set.items()):
        tenant = await tenants.update_tenant(tenant["id"], **defaults_to_set)
        print(f"updated tenant {TENANT_SLUG!r} default providers")
    else:
        print(f"tenant {TENANT_SLUG!r} default providers already set")

    agent = await agents.get_agent(TENANT_SLUG, AGENT_SLUG)
    if agent is None:
        agent = await agents.create_agent(
            tenant_id=tenant["id"], slug=AGENT_SLUG, name="Default Assistant",
            greeting=GREETING, system_prompt=SYSTEM_PROMPT,
        )
        print(f"created agent {AGENT_SLUG!r} ({agent['id']})")
    else:
        print(f"agent {AGENT_SLUG!r} already exists ({agent['id']})")

    print("Seed complete.")


if __name__ == "__main__":
    asyncio.run(main())
