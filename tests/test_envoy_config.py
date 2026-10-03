"""Envoy fronts Conversation's unauthenticated gRPC, so it must never listen beyond loopback."""
from __future__ import annotations

from pathlib import Path

import yaml

_ENVOY = Path(__file__).resolve().parents[1] / "config" / "envoy.yaml"


def test_every_envoy_listener_binds_loopback():
    listeners = yaml.safe_load(_ENVOY.read_text())["static_resources"]["listeners"]
    assert listeners, "no listeners found; the test is not reading the real config"
    exposed = {
        l["name"]: l["address"]["socket_address"]["address"]
        for l in listeners
        if l["address"]["socket_address"]["address"] not in {"127.0.0.1", "::1"}
    }
    assert not exposed, f"Envoy listeners must bind loopback: {exposed}"
