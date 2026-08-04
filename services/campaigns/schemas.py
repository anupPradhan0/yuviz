"""
Pydantic request models for Campaign Service's REST API — same "responses
are the plain dicts the service modules already return" convention as
services/config/schemas.py.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

CampaignStatus = Literal["draft", "running", "paused", "completed"]


class CampaignCreate(BaseModel):
    agent_id:               str
    name:                   str
    caller_id:              str | None = None
    max_concurrent_calls:   int = 1
    pacing_seconds:         int = 5
    max_attempts:           int = 1
    # "HH:MM" 24h strings, both required together or both omitted — see
    # worker.py's _within_calling_hours(). None/None means unrestricted.
    calling_hours_start:    str | None = None
    calling_hours_end:      str | None = None
    calling_hours_timezone: str = "UTC"


class CampaignUpdate(BaseModel):
    name:                   str | None = None
    caller_id:              str | None = None
    max_concurrent_calls:   int | None = None
    pacing_seconds:         int | None = None
    max_attempts:           int | None = None
    calling_hours_start:    str | None = None
    calling_hours_end:      str | None = None
    calling_hours_timezone: str | None = None


class ContactCreate(BaseModel):
    phone_number: str
    name:         str | None = None


class DncNumberCreate(BaseModel):
    phone_number: str
    reason:       str | None = None
