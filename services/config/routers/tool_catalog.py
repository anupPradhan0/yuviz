"""
Static tool catalog for the Admin UI's "Tools" tab — what tools exist and
what an admin needs to fill in to configure one. No database, no auth
required beyond being logged in; this is metadata, not tenant data.

KNOWN DUPLICATION, flagged rather than silently done: this list's
tool_name/display shape overlaps services/conversation/tools/registry.py's
ToolRegistry (the actual LLM-facing schema catalog). They aren't unified
because Config Service and Conversation Service are deliberately separate
deployables with no shared import today (see architecture_decisions:
Gateway/ConvSvc/Config Service responsibility boundaries). Revisit once a
second INDEPENDENTLY CONFIGURABLE tool exists — cancel_appointment doesn't
count (see below), so this is still a single-entry list.

cancel_appointment (added 2026-07-23) is deliberately NOT listed here: it's
never independently configured by an admin — ToolPolicyResolver
auto-derives it from book_appointment's own tool_provider_config (same
Cal.com account/event type), since there's no real scenario where a tenant
wants booking without cancellation. See
services/conversation/tools/policy_resolver.py's _AUTO_DERIVED_COMPANIONS.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth import CurrentUser
from ..deps import get_current_user

router = APIRouter(prefix="/tools", tags=["tool_catalog"])

_CATALOG = [
    {
        "tool_name": "book_appointment",
        "display_name": "Book Appointment",
        "description": (
            "Lets the agent check calendar availability and book an appointment for the caller "
            "without exposing multiple tool calls to the LLM — availability checking, booking, "
            "and alternative-slot lookup all happen inside a single tool call. Also automatically "
            "gives the agent the ability to cancel appointments using this same configuration — "
            "no separate setup needed."
        ),
        "category": "calendar",
        "engines": [
            {
                "engine": "cal_com",
                "display_name": "Cal.com",
                "extra_fields": [
                    {
                        "key": "event_type_id",
                        "label": "Event Type ID",
                        "type": "number",
                        "required": True,
                        "help": "The Cal.com event type this agent books against — one event type per agent (see project design notes).",
                    },
                    {
                        "key": "timezone",
                        "label": "Default Timezone",
                        "type": "text",
                        "required": False,
                        "help": "IANA timezone name, e.g. America/New_York or Asia/Kolkata. Used whenever a caller doesn't state one. Defaults to UTC if left blank.",
                    },
                    {
                        "key": "default_attendee_phone",
                        "label": "Default Attendee Phone",
                        "type": "text",
                        "required": False,
                        "help": "Used only for sessions with no caller ID at all (e.g. a browser test call) and no phone number given. Leave blank to have the agent ask instead.",
                    },
                ],
            },
        ],
    },
]


@router.get("/catalog")
async def get_tool_catalog(current_user: CurrentUser = Depends(get_current_user)):
    return _CATALOG
