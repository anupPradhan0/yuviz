"""
ToolPolicyResolver._add_auto_derived_companions tests — pure in-memory
logic, no Postgres needed (the method never touches self._pool). Covers
the 2026-07-23 design decision: cancel_appointment/reschedule_appointment
are never independently configured, both are derived from
book_appointment's own tool_provider_config.
"""

from __future__ import annotations

from services.conversation.tools.policy_resolver import ResolvedToolPolicy, ToolPolicyResolver
from services.conversation.tools.registry import ToolRegistry


def _policy(tool_name: str, tool_provider_config_id: str = "cfg1") -> ResolvedToolPolicy:
    defn = ToolRegistry().resolve(tool_name)
    return ResolvedToolPolicy(
        definition=defn, tool_provider_config_id=tool_provider_config_id, engine="cal_com",
        api_key_ref="env:CAL_API_KEY", extra={"event_type_id": 123}, timeout_ms=None, max_calls_per_turn=None,
    )


def _resolver() -> ToolPolicyResolver:
    return ToolPolicyResolver(pool=None, registry=ToolRegistry())


def test_book_appointment_present_derives_both_companions():
    resolver = _resolver()
    resolved = [_policy("book_appointment", tool_provider_config_id="cfg-abc")]

    resolver._add_auto_derived_companions(resolved, agent_id="a1")

    names = {p.definition.name for p in resolved}
    assert names == {"book_appointment", "cancel_appointment", "reschedule_appointment"}
    for derived_name in ("cancel_appointment", "reschedule_appointment"):
        derived = next(p for p in resolved if p.definition.name == derived_name)
        # Reuses book_appointment's own config — never a separate one.
        assert derived.tool_provider_config_id == "cfg-abc"
        assert derived.api_key_ref == "env:CAL_API_KEY"
        assert derived.extra == {"event_type_id": 123}


def test_no_book_appointment_means_no_derived_companions():
    resolver = _resolver()
    resolved: list[ResolvedToolPolicy] = []

    resolver._add_auto_derived_companions(resolved, agent_id="a1")

    assert resolved == []


def test_explicit_companion_row_is_never_overridden():
    """An explicit agent_tool_policies row for a derived tool (however it
    got there) always wins over the derived one — this only fills a gap,
    never clobbers real configuration."""
    resolver = _resolver()
    explicit_cancel = _policy("cancel_appointment", tool_provider_config_id="cfg-explicit")
    resolved = [_policy("book_appointment", tool_provider_config_id="cfg-abc"), explicit_cancel]

    resolver._add_auto_derived_companions(resolved, agent_id="a1")

    cancel_policies = [p for p in resolved if p.definition.name == "cancel_appointment"]
    assert len(cancel_policies) == 1
    assert cancel_policies[0].tool_provider_config_id == "cfg-explicit"
    # reschedule_appointment still gets derived normally alongside the
    # explicit cancel_appointment override.
    reschedule_policies = [p for p in resolved if p.definition.name == "reschedule_appointment"]
    assert len(reschedule_policies) == 1
    assert reschedule_policies[0].tool_provider_config_id == "cfg-abc"


def test_disabling_book_appointment_removes_both_derived_companions():
    """Simulates the DB query already filtering out a disabled
    book_appointment row before this method ever runs — neither derived
    tool gets derived because there's nothing to derive them from."""
    resolver = _resolver()
    resolved: list[ResolvedToolPolicy] = []  # book_appointment row excluded upstream (enabled=false)

    resolver._add_auto_derived_companions(resolved, agent_id="a1")

    assert resolved == []
