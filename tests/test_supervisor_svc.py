import os
import subprocess
from pathlib import Path

import pytest

SVC = Path(__file__).resolve().parents[1] / "deployment" / "svc"


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "supervisorctl").write_text('#!/bin/sh\necho "supervisorctl $*"\n')

    def _run(*args, open_calls="0", fs_running=False, fs_cli=None):
        (bin_dir / "pgrep").write_text(f"#!/bin/sh\nexit {0 if fs_running else 1}\n")
        fs_bin = tmp_path / "fs" / "bin"
        fs_bin.mkdir(parents=True, exist_ok=True)
        (fs_bin / "freeswitch").write_text("#!/bin/sh\n")
        (fs_bin / "fs_cli").write_text(fs_cli or "#!/bin/sh\nexit 1\n")
        for f in fs_bin.iterdir():
            f.chmod(0o755)
        psql = "#!/bin/sh\nexit 2\n" if open_calls is None else f"#!/bin/sh\necho {open_calls}\n"
        (bin_dir / "psql").write_text(psql)
        for f in bin_dir.iterdir():
            f.chmod(0o755)
        env = {**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin", "FS_PREFIX": str(tmp_path / "fs")}
        return subprocess.run([str(SVC), *args], env=env, capture_output=True, text=True, timeout=30)

    return _run


def test_restart_of_a_call_path_service_is_refused_while_calls_are_live(run):
    r = run("restart", "gateway", open_calls="2")
    assert r.returncode == 1 and "Refusing to restart" in r.stderr and "supervisorctl" not in r.stdout


def test_it_goes_ahead_when_no_calls_are_live(run):
    r = run("restart", "gateway", open_calls="0")
    assert r.returncode == 0 and r.stdout.strip().endswith("restart gateway")


def test_services_off_the_call_path_are_not_guarded(run):
    assert run("restart", "admin-ui", open_calls="2").returncode == 0
    assert run("status", open_calls=None).returncode == 0


@pytest.mark.parametrize("args", [("restart", "all"), ("stop", "conversation-1"), ("update",), ("stop", "voice:gateway"),
                                  ("restart", "gateway:*"), ("signal", "KILL", "freeswitch"), ("remove", "gateway"),
                                  ("restart", "toolexec")])
def test_every_way_of_hitting_the_call_path_is_guarded(run, args):
    assert run(*args, open_calls="1").returncode == 1


def test_force_overrides_and_is_not_passed_on(run):
    r = run("restart", "gateway", "--force", open_calls="3")
    assert r.returncode == 0 and "--force" not in r.stdout


def test_unknown_call_count_refuses(run):
    r = run("restart", "gateway", open_calls=None)
    assert r.returncode == 1 and "could not count live calls" in r.stderr


def test_running_freeswitch_whose_calls_cannot_be_counted_refuses(run):
    r = run("restart", "gateway", open_calls="0", fs_running=True)  # fs_cli exits 1, e.g. a wrong ESL password
    assert r.returncode == 1 and "could not be counted" in r.stderr


def test_sip_calls_on_freeswitch_refuse_even_with_no_calls_rows(run):
    r = run("restart", "gateway", open_calls="0", fs_running=True, fs_cli='#!/bin/sh\necho "2 total."\n')
    assert r.returncode == 1 and "2 SIP call(s)" in r.stderr
