"""
ToolProviderManager tests — covers secondary_api_key_ref resolution (added
for per-tenant SMS booking confirmations) alongside the pre-existing
single-secret path, plus _make_sms_provider's own "feature simply off
unless fully configured" branches.
"""

from __future__ import annotations

from services.conversation.tools.policy_resolver import ResolvedToolPolicy
from services.conversation.tools.provider_manager import ToolProviderManager, _make_sms_provider
from services.conversation.tools.registry import ToolRegistry


def _policy(**extra_overrides) -> ResolvedToolPolicy:
    defn = ToolRegistry().resolve("book_appointment")
    extra = {"event_type_id": 123, **extra_overrides}
    return ResolvedToolPolicy(
        definition=defn, tool_provider_config_id="cfg1", engine="cal_com",
        api_key_ref="ref:cal", secondary_api_key_ref="ref:twilio",
        extra=extra, timeout_ms=None, max_calls_per_turn=None,
    )


class _FakeSecretResolver:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    async def resolve(self, ref: str) -> str:
        return self._values[ref]


async def test_get_resolves_both_secrets_and_wires_sms_provider():
    policy = _policy(
        sms_enabled=True, sms_account_sid="ACtest", sms_from_number="+19998887777",
    )
    manager = ToolProviderManager(
        _FakeSecretResolver({"ref:cal": "cal-api-key", "ref:twilio": "twilio-auth-token"}),
    )

    provider = await manager.get(policy)

    assert provider.sms_provider is not None


async def test_get_leaves_sms_off_when_not_enabled():
    policy = _policy(sms_enabled=False)
    manager = ToolProviderManager(
        _FakeSecretResolver({"ref:cal": "cal-api-key", "ref:twilio": "twilio-auth-token"}),
    )

    provider = await manager.get(policy)

    assert provider.sms_provider is None


def test_make_sms_provider_none_when_not_enabled():
    policy = _policy(sms_enabled=False)
    assert _make_sms_provider(policy, "some-token") is None


def test_make_sms_provider_none_when_enabled_but_missing_pieces():
    policy = _policy(sms_enabled=True, sms_account_sid="ACtest")  # no sms_from_number
    assert _make_sms_provider(policy, "some-token") is None


def test_make_sms_provider_none_when_no_secondary_secret_resolved():
    policy = _policy(sms_enabled=True, sms_account_sid="ACtest", sms_from_number="+19998887777")
    assert _make_sms_provider(policy, None) is None


def test_make_sms_provider_constructs_when_fully_configured():
    policy = _policy(sms_enabled=True, sms_account_sid="ACtest", sms_from_number="+19998887777")
    provider = _make_sms_provider(policy, "twilio-auth-token")
    assert provider is not None
