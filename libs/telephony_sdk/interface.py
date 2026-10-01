"""
ITelephonyProvider — the shared interface every telephony provider
(Vobiz today; Twilio/Telnyx additively later) implements.

Covers only outbound call control, inbound webhook verification, and the
provider-specific answer-response shape. It does NOT cover the long-lived
WebSocket/media-bridging side (libs/media_stream_sdk/bridge.py,
libs/vad_sdk/) — that's a protocol/media concern, not a provider-config
one. The native Gateway/Kamailio/FreeSWITCH call path never implements
this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal

from .exceptions import TelephonyTransferUnsupported


@dataclass(frozen=True)
class NormalizedInboundCall:
    provider_call_id: str
    from_number: str
    to_number: str
    known_tenant_slug: str | None  # filled by adapters whose account binds the tenant
    raw: dict[str, Any]


@dataclass(frozen=True)
class ReconcileResult:
    outcome: Literal["placed", "not_placed", "indeterminate"]
    provider_call_id: str | None = None


@dataclass(frozen=True)
class InboundUrls:
    """Where the provider should send a number's inbound calls."""
    answer_url: str
    hangup_url: str


@dataclass(frozen=True)
class InboundSyncResult:
    """Outcome of pointing (or un-pointing) a number at this platform.
    `credentials_update` carries provider-side ids the caller must persist
    on the telephony config (e.g. a Vobiz Application created on first sync)."""
    ok: bool
    message: str | None = None
    credentials_update: dict[str, Any] | None = None


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

    @abstractmethod
    def normalize_inbound_webhook(
        self, *, url: str, headers: dict[str, str], fields: dict[str, Any],
        account_tenant_slug: str,
    ) -> NormalizedInboundCall:
        """Parses the vendor's inbound-call webhook shape into the
        provider-agnostic NormalizedInboundCall. Raises WebhookRejected for
        a vendor-specific rejection (Cloudonix's domain mismatch). Contains
        NO DID lookup and NO call-context work (AC9) — that is the
        orchestrator's job, not the adapter's."""

    @abstractmethod
    def parse_dtmf_digit(self, fields: dict[str, Any]) -> str | None:
        """Extracts DTMF digit from webhook payload; returns None if missing/invalid."""

    @classmethod
    def sensitive_credential_fields(cls) -> list[str]:
        """Field names in `credentials` this provider needs encrypted at
        rest — usable default (AC1): a provider with no secrets need not
        override this."""
        return []

    async def check_health(self) -> bool:
        """Cheap liveness probe against the vendor, called only from the
        health loop's own asyncio task — never on a request path. Usable
        default (AC3): a provider with no probe endpoint is always
        healthy."""
        return True

    async def transfer_call(self, *, call_id: str, destination: str) -> None:
        raise TelephonyTransferUnsupported(f"{self.PROVIDER_NAME}: transfer_call not yet supported")

    async def owns_number(self, number: str) -> bool | None:
        """Whether `number` is in this provider account. None means the
        provider has no way to check. Raises TelephonyProviderError when the
        lookup itself fails. No default: a new provider must decide, so it
        can't silently skip ownership checks."""
        raise NotImplementedError(f"{self.PROVIDER_NAME}: owns_number must be implemented")

    async def attach_inbound(
        self, number: str, urls: InboundUrls, *, label: str, refresh_app: bool = True,
    ) -> InboundSyncResult:
        """Point `number`'s inbound calls at `urls`, creating whatever the
        provider needs. Never raises for a provider rejection: returns ok=False
        with a message the admin can act on. refresh_app=False skips re-pushing
        `urls` to an existing shared resource (a bulk sync does that once)."""
        raise NotImplementedError(f"{self.PROVIDER_NAME}: attach_inbound must be implemented")

    async def refresh_inbound(self, urls: InboundUrls) -> InboundSyncResult | None:
        """Re-point a resource shared by all the account's numbers at `urls`.
        None when there is nothing shared (yet)."""
        return None

    async def discard_inbound_resources(self, credentials_update: dict[str, Any]) -> None:
        """Delete what an attach created when another sync's copy was stored
        first. Best effort; default has nothing to delete."""
        return None

    async def detach_inbound(self, number: str) -> InboundSyncResult:
        """Stop sending `number`'s inbound calls here. A number already
        detached, or no longer in the account, is ok=True."""
        raise NotImplementedError(f"{self.PROVIDER_NAME}: detach_inbound must be implemented")

    async def reconcile_call(
        self, *, reference: str, observed_call_id: str | None,
    ) -> ReconcileResult:
        """Default: if a callback already observed a vendor call id for this
        reference, confirm it with get_call_status() and report placed/
        not_placed; otherwise 'indeterminate'. A provider whose API can
        look up by our own reference overrides this. Never guesses
        'not_placed' (AC16/17)."""
        if observed_call_id is None:
            return ReconcileResult(outcome="indeterminate")
        try:
            await self.get_call_status(observed_call_id)
        except Exception:
            return ReconcileResult(outcome="indeterminate")
        return ReconcileResult(outcome="placed", provider_call_id=observed_call_id)


class ISmsProvider(ABC):
    PROVIDER_NAME: str

    @classmethod
    def sensitive_credential_fields(cls) -> list[str]:
        return []

    @abstractmethod
    async def send_sms(self, *, from_number: str, to_number: str, text: str) -> str:
        """Places an outbound SMS, returns the provider's own message id."""

    @abstractmethod
    async def get_message_status(self, message_id: str) -> dict[str, Any]:
        ...

    async def reconcile_message(
        self, *, reference: str, observed_message_id: str | None,
    ) -> ReconcileResult:
        if observed_message_id is None:
            return ReconcileResult(outcome="indeterminate")
        try:
            await self.get_message_status(observed_message_id)
        except Exception:
            return ReconcileResult(outcome="indeterminate")
        return ReconcileResult(outcome="placed", provider_call_id=observed_message_id)
