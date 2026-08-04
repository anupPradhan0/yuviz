"""
Lazy module-level singleton for DidProviderManager — same convention as
services/knowledge/runtime.py (see that module's docstring for why this
lives outside app.py: routers depend on this getter directly, not on
app.state, so it works the same whether the real lifespan ran or not —
relevant since httpx.ASGITransport-based tests never trigger lifespan).
"""

from __future__ import annotations

from .provider_manager import DidProviderManager
from .secret_resolver import CompositeSecretResolver

_provider_manager: DidProviderManager | None = None


def get_provider_manager() -> DidProviderManager:
    global _provider_manager
    if _provider_manager is None:
        _provider_manager = DidProviderManager(CompositeSecretResolver())
    return _provider_manager
