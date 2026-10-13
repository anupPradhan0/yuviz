"""Drift gate for docs/knowledge-env-vars.md: every KNOWLEDGE_* / KB_* environment variable the code reads must be in the
doc, and the doc must not list a variable the code no longer reads. The doc is generated from the code, so the fix is
always the same (the gate checks names only, so a moved line number or a changed default never fails it): run `python scripts/gen_knowledge_env_doc.py`, give any new variable a one-line meaning in
scripts/knowledge_env_meanings.py, and commit both."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
_ROW = re.compile(r"^\|\s*`([A-Z][A-Z0-9_]+)`\s*\|", re.M)
_OURS = re.compile(r"^(KNOWLEDGE_|KB_)")
FIX = "run `python scripts/gen_knowledge_env_doc.py`, add a meaning in scripts/knowledge_env_meanings.py, commit both"


def _generator():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec = importlib.util.spec_from_file_location("gen_knowledge_env_doc", ROOT / "scripts" / "gen_knowledge_env_doc.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # dataclasses resolves the module by name
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(ROOT / "scripts"))


def _documented(text: str) -> set[str]:
    return set(_ROW.findall(text))


def undocumented(code_names: set[str], doc_text: str) -> list[str]:
    return sorted(n for n in code_names if _OURS.match(n) and n not in _documented(doc_text))


def stale(code_names: set[str], doc_text: str) -> list[str]:
    return sorted(n for n in _documented(doc_text) if n not in code_names)


def test_every_knowledge_env_var_the_code_reads_is_documented():
    gen = _generator()
    doc = (ROOT / "docs" / "knowledge-env-vars.md").read_text()
    missing = undocumented(set(gen.collect()), doc)
    assert not missing, f"environment variable(s) read by the code but missing from docs/knowledge-env-vars.md: {missing}. To fix: {FIX}"


def test_the_doc_lists_no_variable_the_code_no_longer_reads():
    gen = _generator()
    doc = (ROOT / "docs" / "knowledge-env-vars.md").read_text()
    gone = stale(set(gen.collect()), doc)
    assert not gone, f"docs/knowledge-env-vars.md lists variable(s) the code no longer reads: {gone}. To fix: {FIX}"


def test_the_checks_themselves_can_fail():
    doc = "| `KNOWLEDGE_A` | x |\n| `KNOWLEDGE_OLD` | x |\n"
    assert undocumented({"KNOWLEDGE_A", "KNOWLEDGE_NEW", "KB_NEW", "OTHER_VAR"}, doc) == ["KB_NEW", "KNOWLEDGE_NEW"]
    assert stale({"KNOWLEDGE_A"}, doc) == ["KNOWLEDGE_OLD"]


def test_the_scanner_finds_each_way_of_reading_the_environment(tmp_path, monkeypatch):
    gen = _generator()
    pkg = tmp_path / "services" / "knowledge"
    pkg.mkdir(parents=True)
    (pkg / "m.py").write_text(
        "import os\nfrom .envparse import env_float\n_NAME = 'KNOWLEDGE_VIA_CONST'\n"
        "a = os.environ.get('KNOWLEDGE_GET', '5')\nb = os.getenv('KNOWLEDGE_GETENV')\nc = os.environ['KNOWLEDGE_SUBSCRIPT']\n"
        "d = env_float('KNOWLEDGE_HELPER', 1.5)\ne = os.environ.get(_NAME)\n")
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    monkeypatch.setattr(gen, "SCOPE", ["services/knowledge"])
    assert set(gen.collect()) == {
        "KNOWLEDGE_GET", "KNOWLEDGE_GETENV", "KNOWLEDGE_SUBSCRIPT", "KNOWLEDGE_HELPER", "KNOWLEDGE_VIA_CONST"}
