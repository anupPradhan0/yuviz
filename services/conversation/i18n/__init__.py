"""Spoken system strings (fillers, goodbyes, fallbacks) per language.

One module per language (i18n/<code>.py) exposing STRINGS (key -> text) and
TOOL_FILLERS ((phrase, approx spoken seconds), ...). Every module is discovered and
validated at import, so a malformed one fails startup. Missing languages or keys
fall back to English. Agent-level overrides (farewell_message, transfer_announcement) are
applied by the caller and always win over these.
"""

from __future__ import annotations

import importlib
import math
import pkgutil
from collections.abc import Iterable
from types import ModuleType

from libs.config_sdk.languages import normalize_language

from . import en


def _validate(code: str, module: ModuleType) -> None:
    """Raise ValueError naming the module when it breaks the language-table contract."""
    if normalize_language(code) != code:
        raise ValueError(f"i18n module {code!r}: name must be a 2-3 letter lowercase language code")
    strings = getattr(module, "STRINGS", None)
    if not isinstance(strings, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and v for k, v in strings.items()
    ):
        raise ValueError(f"i18n module {code!r}: STRINGS must be a dict[str, str] with non-empty values")
    unknown = set(strings) - set(en.STRINGS)
    if code != "en" and unknown:
        raise ValueError(f"i18n module {code!r}: STRINGS keys not in en.STRINGS: {sorted(unknown)}")
    fillers = getattr(module, "TOOL_FILLERS", None)
    if not isinstance(fillers, tuple) or not fillers or not all(
        isinstance(f, tuple)
        and len(f) == 2
        and isinstance(f[0], str)
        and f[0]
        and isinstance(f[1], (int, float))
        and not isinstance(f[1], bool)
        and math.isfinite(f[1])
        and f[1] > 0
        for f in fillers
    ):
        raise ValueError(
            f"i18n module {code!r}: TOOL_FILLERS must be a non-empty tuple of (phrase, seconds > 0) pairs"
        )


def _discover(path: Iterable[str], package: str) -> dict[str, ModuleType]:
    """Import every non-underscore module under path, require en, then validate each."""
    tables = {
        info.name: importlib.import_module(f"{package}.{info.name}")
        for info in pkgutil.iter_modules(path)
        if not info.name.startswith("_")
    }
    if "en" not in tables:
        raise ValueError(f"i18n package {package!r}: no en module found")
    for code, module in tables.items():
        _validate(code, module)
    return tables


_TABLES: dict[str, ModuleType] = _discover(__path__, __name__)
# Each language's shortest filler (first in table order on ties), for the selector's error path.
_SHORTEST: dict[str, str] = {
    code: min(module.TOOL_FILLERS, key=lambda f: f[1])[0] for code, module in _TABLES.items()
}


def t(key: str, language: str | None) -> str:
    """The string for key in language, else English. Raises KeyError for an unknown key."""
    table = _TABLES.get(normalize_language(language) or "en")
    if table is not None and key in table.STRINGS:
        return table.STRINGS[key]
    return en.STRINGS[key]


def tool_fillers(language: str | None) -> tuple[tuple[str, float], ...]:
    table = _TABLES.get(normalize_language(language) or "en")
    return table.TOOL_FILLERS if table is not None and table.TOOL_FILLERS else en.TOOL_FILLERS


def shortest_tool_filler(language: str | None) -> str:
    """The language's shortest tool filler, else English's."""
    return _SHORTEST.get(normalize_language(language) or "en", _SHORTEST["en"])
