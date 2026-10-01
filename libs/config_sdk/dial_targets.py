"""Shapes a tenant-configured transfer destination or caller id may take.

These values end up inside FreeSWITCH ESL commands built by the Gateway
(gateway/src/telephony/EslClient.cpp), which applies the same allowlist as a
final check. Keep the two in sync.
"""

from __future__ import annotations

import re

_DIAL_NUMBER_RE = re.compile(r"\+?[0-9]{2,15}")
_SIP_URI_RE = re.compile(r"sips?:[A-Za-z0-9._+:;=~-]+@[A-Za-z0-9._+:;=~-]+")


def is_dial_number(value: str) -> bool:
    return _DIAL_NUMBER_RE.fullmatch(value) is not None


def is_transfer_destination(value: str) -> bool:
    if value.lower().startswith(("sip:", "sips:")):
        return _SIP_URI_RE.fullmatch(value) is not None
    return is_dial_number(value)
