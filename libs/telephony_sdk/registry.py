"""
TelephonyProviderRegistry — name -> ITelephonyProvider class. Each provider
module (providers/vobiz.py, and later providers/twilio.py etc.) registers
itself on import; adding a new provider is a new file + one import line in
providers/__init__.py, no edits anywhere else (same "additive, not invasive"
shape as libs/knowledge_sdk's provider registration).
"""

from __future__ import annotations

from .interface import ITelephonyProvider


class TelephonyProviderRegistry:
    _providers: dict[str, type[ITelephonyProvider]] = {}

    @classmethod
    def register(cls, name: str, provider_cls: type[ITelephonyProvider]) -> None:
        cls._providers[name] = provider_cls

    @classmethod
    def get(cls, name: str) -> type[ITelephonyProvider]:
        try:
            return cls._providers[name]
        except KeyError:
            raise ValueError(f"Unknown telephony provider: {name!r}") from None

    @classmethod
    def all(cls) -> dict[str, type[ITelephonyProvider]]:
        return dict(cls._providers)
