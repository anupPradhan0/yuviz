"""
ITelephonyProvider — the shared interface every telephony provider
(Vobiz today; Twilio/Telnyx additively later) implements.

Scope deliberately kept to exactly what a REST+webhook telephony provider's
own already-built, tested code needs (grounded in services/vobiz/'s real
client.py + signature.py, not a speculative superset copied from a richer
reference implementation): outbound call control, inbound webhook
verification, and the provider-specific "how do I tell you to start
streaming audio" response shape. It does NOT cover the long-lived
WebSocket/media-bridging side (that stays in services/vobiz/bridge.py,
vad.py, audio.py) — those are protocol/media concerns, not provider-config
concerns, exactly the same split Dograh's own ARI (direct-SIP) integration
draws between its request/response provider interface and its separate
long-lived channel-event process. Our own Gateway/Kamailio/FreeSWITCH path
is the equivalent of that separate process here and is deliberately never
made to implement this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ITelephonyProvider(ABC):
    """One instance per telephony_configs row — constructed with that row's
    `credentials` dict."""

    PROVIDER_NAME: str

    def __init__(self, credentials: dict[str, Any]) -> None:
        self._credentials = credentials

    @classmethod
    @abstractmethod
    def required_credential_fields(cls) -> list[str]:
        """Field names this provider needs in `credentials` — backs the
        Config Service's provider-discovery endpoint so an admin UI can
        render the right form without hardcoding per-provider fields."""

    @classmethod
    @abstractmethod
    def validate_credentials(cls, credentials: dict[str, Any]) -> None:
        """Raise TelephonyProviderError if credentials are missing/malformed.
        Called by Config Service at telephony_configs creation time, before
        the row is ever written — never at call time."""

    @abstractmethod
    async def initiate_call(
        self, *, from_number: str, to_number: str,
        answer_url: str, hangup_url: str | None = None, ring_url: str | None = None,
    ) -> str:
        """Places an outbound call, returns the provider's own call id."""

    @abstractmethod
    async def hangup_call(self, call_id: str) -> None:
        ...

    @abstractmethod
    async def get_call_status(self, call_id: str) -> dict[str, Any]:
        ...

    @abstractmethod
    def verify_webhook_signature(self, url: str, headers: dict[str, str]) -> bool:
        """headers should already be lower-cased keys. Fail closed: a
        missing/invalid signature returns False, never raises past this
        point — the caller (a webhook route) turns False into a 403 before
        touching any call state."""

    @abstractmethod
    def build_answer_response(self, websocket_url: str) -> str:
        """The provider-specific XML/markup response to the answer webhook
        that tells the provider to open a media WebSocket to websocket_url."""
