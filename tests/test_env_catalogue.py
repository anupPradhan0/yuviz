"""Settings live in one place: the root .env (template: .env.example).

1. Every environment variable the Python services read is listed in
   .env.example, so nothing configurable is hidden in code.
2. .env.example carries no real secret values.
3. No committed non-test file holds a credential: FreeSWITCH's public ESL
   default password, or a URL with an embedded password.
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_EXAMPLE = _REPO / ".env.example"

# Set per process by each start command, not a platform setting.
_NOT_IN_CATALOGUE = {"PORT"}
# Helpers that read os.environ by name (pipeline_config._env, email._env, ...).
_ENV_HELPERS = {"_env", "_env_int", "_env_float", "_env_bool"}
_SECRET_NAME = re.compile(r"(PASSWORD|SECRET|_KEY|TOKEN)$")


def _tracked(*patterns: str) -> list[Path]:
    out = subprocess.run(["git", "ls-files", *patterns], cwd=_REPO, capture_output=True, text=True, check=True)
    return [_REPO / p for p in out.stdout.split()]


def _is_test(path: Path) -> bool:
    rel = path.relative_to(_REPO).parts
    return "tests" in rel or rel[0] == "tests" or path.name.endswith("_test.cpp")


def _catalogue() -> dict[str, str]:
    pairs = (line.split("=", 1) for line in _EXAMPLE.read_text().splitlines() if re.match(r"^[A-Z][A-Z0-9_]*=", line))
    return {k: v for k, v in pairs}


def _env_names_read_by(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    constants = {
        t.id: n.value.value
        for n in ast.walk(tree) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
        and isinstance(n.value.value, str)
        for t in n.targets if isinstance(t, ast.Name)
    }
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and n.args:
            fn = ast.unparse(n.func)
            if fn.endswith(("environ.get", "getenv", "environ.setdefault")) or fn in _ENV_HELPERS:
                arg = n.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    names.add(arg.value)
                elif isinstance(arg, ast.Name) and arg.id in constants:
                    names.add(constants[arg.id])
        if isinstance(n, ast.Subscript) and ast.unparse(n.value).endswith("environ"):
            if isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str):
                names.add(n.slice.value)
    return {x for x in names if re.fullmatch(r"[A-Z][A-Z0-9_]+", x)}


def test_every_env_var_the_services_read_is_in_env_example():
    sources = [p for p in _tracked("services/*.py", "libs/*.py") if not _is_test(p) and "generated" not in p.parts]
    read = {name for p in sources for name in _env_names_read_by(p)}
    assert len(read) > 30, "the scan found almost nothing; it is not looking at the services"
    missing = read - set(_catalogue()) - _NOT_IN_CATALOGUE
    assert not missing, f"read by code but not listed in .env.example: {sorted(missing)}"


def test_env_example_has_no_secret_values():
    filled = [k for k, v in _catalogue().items() if _SECRET_NAME.search(k) and not k.endswith("_REF") and v.strip()]
    assert not filled, f".env.example must leave secrets blank: {filled}"


_CREDENTIALED_URL = re.compile(r"[a-z][a-z0-9+]*://[^\s/:@\"'`]+:([^\s@\"'`]+)@")


def _is_placeholder(secret: str) -> bool:
    return secret.startswith("<") or "$" in secret or secret in {"pass", "password"}


def test_no_committed_file_holds_a_credential():
    files = [p for p in _tracked() if not _is_test(p) and p.is_file() and p.suffix not in {".png", ".jpg", ".onnx", ".xlsx", ".csv"}]
    leaks = []
    for p in files:
        try:
            text = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        rel = p.relative_to(_REPO)
        if "ClueCon" in text:
            leaks.append(f"{rel}: FreeSWITCH's default ESL password")
        for m in _CREDENTIALED_URL.finditer(text):
            if not _is_placeholder(m.group(1)):
                leaks.append(f"{rel}: URL with an embedded password ({m.group(0)[:40]}…)")
    assert not leaks, "credentials belong in .env, not the repository:\n  " + "\n  ".join(leaks)
