"""Spoken system strings per language, falling back to English."""

from __future__ import annotations

import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

from ..fillers import FillerSelector, _TOOL_FILLERS
from ..i18n import en, hi, t, tool_fillers
from .. import pipeline
from .. import i18n


def test_every_hindi_key_exists_in_english():
    assert set(hi.STRINGS) <= set(en.STRINGS)


def test_hindi_strings_are_used_for_hindi():
    assert t("fallback_goodbye", "hi") == hi.STRINGS["fallback_goodbye"]
    assert t("max_duration_goodbye", "hi-IN") == hi.STRINGS["max_duration_goodbye"]


@pytest.mark.parametrize("language", ["fr", "xx", None, ""])
def test_unknown_language_falls_back_to_english(language):
    assert t("fallback_goodbye", language) == "Goodbye."
    assert tool_fillers(language) == en.TOOL_FILLERS


def test_missing_key_in_a_language_falls_back_to_english(monkeypatch):
    monkeypatch.delitem(hi.STRINGS, "fallback_llm_error")
    assert t("fallback_llm_error", "hi") == en.STRINGS["fallback_llm_error"]


def test_unknown_key_raises():
    with pytest.raises(KeyError):
        t("no_such_key", "en")


def test_english_pipeline_strings_are_byte_identical():
    assert pipeline._FALLBACK_GOODBYE == "Goodbye."
    assert pipeline._MAX_DURATION_GOODBYE == (
        "We're at the time limit for this call now. Thanks for calling — goodbye."
    )
    assert pipeline._FALLBACK_LLM_ERROR == (
        "Sorry, I'm having a little trouble right now. Could you say that again?"
    )
    assert pipeline._TRANSFER_FAILED_FALLBACK == "I'm sorry, I couldn't connect you to an agent right now."
    assert _TOOL_FILLERS[0] == ("One moment.", 1.0)


def test_filler_selector_uses_the_language_pool():
    phrases = {p for p, _ in hi.TOOL_FILLERS}
    for _ in range(20):
        assert FillerSelector().select_tool_filler("t", None, 1500, language="hi") in phrases
    assert FillerSelector().select_tool_filler("t", None, 1000) in {p for p, _ in en.TOOL_FILLERS}


_VALID_XX = '''
STRINGS = {"fallback_goodbye": "xx goodbye"}
TOOL_FILLERS = (("xx one", 1.0), ("xx two", 2.0))
'''


def _make_package(tmp_path, monkeypatch, modules: dict[str, str]):
    """A throwaway i18n-shaped package holding the real en.py plus the given modules."""
    name = f"i18n_{tmp_path.name.replace('-', '_')}"
    pkg = tmp_path / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "en.py").write_text((Path(i18n.__file__).parent / "en.py").read_text())
    for module, source in modules.items():
        (pkg / f"{module}.py").write_text(source)
    monkeypatch.syspath_prepend(str(tmp_path))
    return name, pkg


@pytest.mark.parametrize(
    ("module", "source", "rule"),
    [
        ("xx", 'STRINGS = {"fallback_goodbye": "x"}', "TOOL_FILLERS must be a non-empty tuple"),
        ("xx", 'STRINGS = {}\nTOOL_FILLERS = ()', "TOOL_FILLERS must be a non-empty tuple"),
        ("xx", 'STRINGS = {}\nTOOL_FILLERS = (("xx", "1.0"),)', "TOOL_FILLERS must be a non-empty tuple"),
        ("xx", 'STRINGS = {}\nTOOL_FILLERS = (("xx", float("nan")),)', "TOOL_FILLERS must be a non-empty tuple"),
        ("xx", 'STRINGS = {}\nTOOL_FILLERS = (("xx", 0),)', "TOOL_FILLERS must be a non-empty tuple"),
        ("xx", 'STRINGS = {}\nTOOL_FILLERS = (("xx", True),)', "TOOL_FILLERS must be a non-empty tuple"),
        ("xx", 'STRINGS = {"fallback_goodbye": ""}\nTOOL_FILLERS = (("xx", 1.0),)', "STRINGS must be a dict"),
        ("xx", 'STRINGS = {"fallback_godbye": "x"}\nTOOL_FILLERS = (("xx", 1.0),)', "keys not in en.STRINGS"),
        ("english", _VALID_XX, "name must be a 2-3 letter lowercase language code"),
    ],
)
def test_malformed_language_module_fails_discovery(tmp_path, monkeypatch, module, source, rule):
    name, pkg = _make_package(tmp_path, monkeypatch, {module: source})
    with pytest.raises(ValueError, match=rf"{module!r}.*{rule}"):
        i18n._discover([str(pkg)], name)


def test_discovery_requires_english(tmp_path, monkeypatch):
    name, pkg = _make_package(tmp_path, monkeypatch, {"xx": _VALID_XX})
    (pkg / "en.py").unlink()
    with pytest.raises(ValueError, match="en"):
        i18n._discover([str(pkg)], name)


def test_language_sorting_before_english_validates(tmp_path, monkeypatch):
    name, pkg = _make_package(tmp_path, monkeypatch, {"ar": _VALID_XX})
    assert sorted(i18n._discover([str(pkg)], name)) == ["ar", "en"]


def test_valid_language_module_is_discovered(tmp_path, monkeypatch):
    name, pkg = _make_package(tmp_path, monkeypatch, {"xx": _VALID_XX, "_helper": "raise RuntimeError('imported')"})
    assert set(i18n._discover([str(pkg)], name)) == {"en", "xx"}


def test_real_package_discovers_every_language_file():
    files = {
        p.stem for p in Path(i18n.__file__).parent.glob("*.py") if not p.stem.startswith("_")
    }
    assert len(files) >= 2
    assert set(i18n._TABLES) == files


_XX_THREE_TIERS = '''
STRINGS = {"fallback_goodbye": "xx goodbye"}
TOOL_FILLERS = (("xx short", 1.0), ("xx mid", 2.0), ("xx long", 3.0))
'''


def test_discovered_language_is_wired_into_strings_selector_and_shortest(tmp_path, monkeypatch):
    name, pkg = _make_package(tmp_path, monkeypatch, {"xx": _XX_THREE_TIERS})
    tables = i18n._discover([str(pkg)], name)
    monkeypatch.setattr(i18n, "_TABLES", tables)
    monkeypatch.setattr(
        i18n, "_SHORTEST", {c: min(m.TOOL_FILLERS, key=lambda f: f[1])[0] for c, m in tables.items()}
    )

    assert t("fallback_goodbye", "xx") == "xx goodbye"
    assert t("fallback_llm_error", "xx") == tables["en"].STRINGS["fallback_llm_error"]
    for ms, phrase in [(500, "xx short"), (2000, "xx mid"), (9000, "xx long")]:
        assert FillerSelector().select_tool_filler("t", None, ms, language="xx") == phrase
    assert i18n.shortest_tool_filler("xx-YY") == "xx short"


@pytest.mark.parametrize("code", sorted(i18n._TABLES))
def test_selector_error_falls_back_in_the_sessions_language(code, monkeypatch):
    def boom(_):
        raise RuntimeError("boom")

    monkeypatch.setattr(random, "choice", boom)
    phrase = FillerSelector().select_tool_filler("t", None, 1500, language=code)
    assert phrase == i18n.shortest_tool_filler(code)
    assert phrase in {p for p, _ in i18n._TABLES[code].TOOL_FILLERS}


@pytest.mark.parametrize("language", ["fr", "zz", None])
def test_unknown_language_gets_english_everywhere(language):
    assert t("fallback_goodbye", language) == en.STRINGS["fallback_goodbye"]
    assert tool_fillers(language) == en.TOOL_FILLERS
    assert i18n.shortest_tool_filler(language) in {p for p, _ in en.TOOL_FILLERS}


def test_unknown_key_still_raises_for_a_discovered_language():
    with pytest.raises(KeyError):
        t("no_such_key", "hi")


def test_shortest_tool_filler_is_the_real_shortest_phrase():
    assert i18n.shortest_tool_filler("en") == "One moment."
    assert i18n.shortest_tool_filler("hi") == "एक पल।"


def test_no_unrequested_language_modules_ship():
    assert set(i18n._TABLES).isdisjoint({"es", "fr", "de", "pt", "it", "ja", "zh"})


def test_lookups_do_no_module_discovery_at_call_time(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("discovery at call time")

    monkeypatch.setattr(i18n.pkgutil, "iter_modules", _boom)
    monkeypatch.setattr(i18n.importlib, "import_module", _boom)

    assert i18n.t("fallback_goodbye", "hi")
    assert i18n.tool_fillers("hi")
    assert i18n.shortest_tool_filler("hi")


def test_importing_the_package_registers_every_language_without_a_lookup():
    code = (
        "import sys, services.conversation.i18n as i;"
        "assert 'hi' in i._TABLES, sorted(i._TABLES);"
        "assert 'services.conversation.i18n.hi' in sys.modules"
    )
    root = Path(__file__).resolve().parents[3]
    env = {**os.environ, "PYTHONPATH": str(root)}
    done = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
