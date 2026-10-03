"""
httpx logs every request URL at INFO. Executor and provider URLs carry path
values (event_id, spreadsheetId, sensitive params), so __main__ quiets the
httpx/httpcore loggers.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from services.toolexec.__main__ import configure_logging

SENTINEL = "path-secret-7f3a91"


@pytest.fixture
def restore_http_loggers():
    saved = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


@pytest.mark.asyncio
async def test_configure_logging_keeps_request_urls_out_of_logs(caplog, restore_http_loggers):
    configure_logging()
    caplog.set_level(logging.INFO)

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get(f"https://api.example.com/events/{SENTINEL}")

    http_records = [r for r in caplog.records if r.name in ("httpx", "httpcore") or r.name.startswith("httpcore.")]
    assert http_records == []
