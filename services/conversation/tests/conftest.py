"""Shared fixtures for the Conversation Service tests."""

from __future__ import annotations

import pytest

from services.conversation.workflow.runner import _GRAPH_CACHE


@pytest.fixture(autouse=True)
def _clear_graph_cache():
    """graph_for caches by (tenant, agent); clear between tests."""
    _GRAPH_CACHE.clear()
    yield
    _GRAPH_CACHE.clear()
