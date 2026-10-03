"""Native services listen on loopback unless .env says otherwise.

Config, Conversation, the Gateway's audio WebSocket and the rest trust their
callers, so a LAN-wide bind lets any peer on the network act as any tenant.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[1]
_LAUNCHERS = ["scripts/start_local.sh", "scripts/start_web_test.sh"]
_ENTRY_POINTS = sorted(
    p for p in (_REPO / "services").glob("*/*.py") if p.name in {"__main__.py", "app.py"}
)


def test_launchers_never_bind_all_interfaces():
    for rel in _LAUNCHERS:
        text = (_REPO / rel).read_text()
        assert "0.0.0.0" not in text, f"{rel} binds 0.0.0.0; use \"$LISTEN_HOST\""
        hosts = re.findall(r"--host (\S+)", text)
        assert hosts and all(h == '"$LISTEN_HOST"' for h in hosts), f"{rel}: {hosts}"


def test_service_entry_points_default_to_loopback():
    assert _ENTRY_POINTS, "found no service entry points"
    offenders = [str(p.relative_to(_REPO)) for p in _ENTRY_POINTS
                 if re.search(r"""["'](0\.0\.0\.0|\[::\])""", p.read_text())]
    assert not offenders, f"hardcoded all-interfaces bind: {offenders}"
    readers = [p for p in _ENTRY_POINTS if 'os.environ.get("LISTEN_HOST", "127.0.0.1")' in p.read_text()]
    assert len(readers) >= 4, "conversation, telephony, toolexec and webcall must read LISTEN_HOST"


def test_gateway_websocket_defaults_to_loopback():
    ws = yaml.safe_load((_REPO / "config/gateway.yaml").read_text())["gateway"]["websocket"]
    assert "host" not in ws, "the bind comes from GATEWAY_LISTEN_HOST in .env, not gateway.yaml"
    header = (_REPO / "gateway/include/config/Config.h").read_text()
    assert 'std::string host{"127.0.0.1"};   // GATEWAY_LISTEN_HOST' in header


def test_every_envoy_listener_binds_loopback():
    listeners = yaml.safe_load((_REPO / "config/envoy.yaml").read_text())["static_resources"]["listeners"]
    assert listeners
    exposed = {l["name"]: l["address"]["socket_address"]["address"] for l in listeners
               if l["address"]["socket_address"]["address"] not in {"127.0.0.1", "::1"}}
    assert not exposed, f"Envoy listeners must bind loopback: {exposed}"


def test_containers_listening_widely_publish_only_to_loopback_by_default():
    services = yaml.safe_load((_REPO / "deployment/docker/docker-compose.yml").read_text())["services"]
    wide = [name for name, svc in services.items()
            if (svc.get("environment") or {}).get("LISTEN_HOST") == "0.0.0.0"]
    assert wide, "no container sets LISTEN_HOST"
    for name in wide:
        for port in services[name].get("ports", []):
            assert re.match(r"\$\{[A-Z_]*BIND_ADDR:-127\.0\.0\.1\}:", str(port)), f"{name}: {port}"
