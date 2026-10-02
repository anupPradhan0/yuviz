"""SIP_PROXY_HOST must reach the Gateway and Campaigns blank until
scripts/update_kamailio_ip.sh writes Kamailio's real IP. A shipped default
(it used to be 127.0.0.1) overrides gateway.yaml's "" and defeats the
Gateway's sip_proxy_host_unset refusal: transfers then dial a host Kamailio
is not listening on and the caller hears ~32 s of silence.

Drives the real scripts/lib/env.sh in bash and zsh (start_local.sh is
sourced into either), against a scratch copy of the repo's .env.example.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENV_SH = REPO / "scripts" / "lib" / "env.sh"
SHELLS = [s for s in ("bash", "zsh") if shutil.which(s)]


def _run(shell: str, repo: Path, script: str, **env: str) -> str:
    clean = {k: v for k, v in os.environ.items() if k != "SIP_PROXY_HOST"}
    clean.update(env)
    clean["REPO"] = str(repo)
    proc = subprocess.run(
        [shell, "-c", f'source "{ENV_SH}"; {script}'],
        env=clean, capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture
def scratch_repo(tmp_path: Path) -> Path:
    shutil.copy(REPO / ".env.example", tmp_path / ".env.example")
    return tmp_path


def test_shipped_env_example_leaves_sip_proxy_host_blank():
    lines = [l for l in (REPO / ".env.example").read_text().splitlines() if l.startswith("SIP_PROXY_HOST=")]
    assert lines == ["SIP_PROXY_HOST="]


@pytest.mark.parametrize("shell", SHELLS)
def test_env_init_then_load_env_does_not_export_a_sip_proxy_host(shell, scratch_repo):
    out = _run(shell, scratch_repo,
               '_env_init >/dev/null; _load_env; printf "%s" "${SIP_PROXY_HOST-<unset>}"')
    assert out == "<unset>"


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("initial", [
    "A=1\nSIP_PROXY_HOST=127.0.0.1\nB=2\n",   # present (an old .env)
    "A=1\nSIP_PROXY_HOST=\nB=2\n",            # present, blank
    "A=1\nB=2\n",                             # missing
    "A=1\nB=2",                               # missing, no trailing newline
    "",                                       # empty file
])
def test_env_put_adds_or_replaces_and_keeps_other_lines(shell, scratch_repo, initial):
    (scratch_repo / ".env").write_text(initial)
    out = _run(shell, scratch_repo, '_env_put SIP_PROXY_HOST 10.1.2.3 && _env_get SIP_PROXY_HOST')
    assert out == "10.1.2.3\n"
    lines = (scratch_repo / ".env").read_text().splitlines()
    assert lines.count("SIP_PROXY_HOST=10.1.2.3") == 1
    assert [l for l in lines if l.startswith("SIP_PROXY_HOST=")] == ["SIP_PROXY_HOST=10.1.2.3"]
    for kept in ("A=1", "B=2"):
        if kept in initial:
            assert kept in lines


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("present", [True, False])
def test_env_put_writes_special_characters_literally(shell, scratch_repo, present):
    (scratch_repo / ".env").write_text("SIP_PROXY_HOST=old\n" if present else "A=1\n")
    value = r"a&b|c\d$e`f"
    _run(shell, scratch_repo, '_env_put SIP_PROXY_HOST "$V"', V=value)
    assert f"SIP_PROXY_HOST={value}" in (scratch_repo / ".env").read_text().splitlines()
