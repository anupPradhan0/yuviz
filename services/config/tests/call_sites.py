"""
Mechanical enumeration of call sites (lessons 29, 42): walks every .py file in
the repo and returns each call to one of `names`, with whether it passes a
given keyword. A hand-written list of callers goes stale the first time
someone adds one.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
_SKIP_DIRS = {"node_modules", "venv", "site-packages"}


def find_calls(names: set[str], keyword: str) -> list[tuple[str, int, str, bool]]:
    found = []
    for path in sorted(REPO.rglob("*.py")):
        rel = path.relative_to(REPO)
        if any(part.startswith(".") or part in _SKIP_DIRS for part in rel.parts):
            continue
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in names:
                found.append((str(rel), node.lineno, name, any(k.arg == keyword for k in node.keywords)))
    return found


def functions_taking(keyword: str) -> set[str]:
    """Every function in services/config that declares `keyword`, so the set of
    callees is derived from the signatures rather than listed by hand."""
    names = set()
    for path in (REPO / "services" / "config").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(a.arg == keyword for a in node.args.args + node.args.kwonlyargs):
                    names.add(node.name)
    return names
