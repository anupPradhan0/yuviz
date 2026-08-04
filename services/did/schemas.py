"""
Pydantic request models for DID Service's REST API — same "responses are
the plain dicts the service modules already return" convention as
services/config/schemas.py.
"""

from __future__ import annotations

from pydantic import BaseModel


class PurchaseNumberRequest(BaseModel):
    carrier_id:   str
    phone_number: str  # E.164 — must be one of the numbers a prior search returned
