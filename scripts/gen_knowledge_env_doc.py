#!/usr/bin/env python3
"""Generate docs/knowledge-env-vars.md from the code.

Walks the AST of the knowledge path (services/knowledge, libs/knowledge_sdk, libs/egress_guard.py, services/conversation;
tests excluded) for every environment read: os.environ.get / os.getenv / os.environ[...] and the env helpers
(env_float, env_int, env_flag, _env_float, _env_int, _env, _env_bool, ...). It records the variable name, default, type
and file:line. Meanings, grouping and flags are hand-written in scripts/knowledge_env_meanings.py.

    python scripts/gen_knowledge_env_doc.py            # rewrite docs/knowledge-env-vars.md
    python scripts/gen_knowledge_env_doc.py --check    # exit 1 if the doc differs from a fresh render (stricter than the test, which checks names only)

When the drift test fails because someone added, renamed or removed a variable: run the first command, then give any
new variable a one-line meaning in knowledge_env_meanings.py (it shows as "(undocumented)" until you do) and commit both."""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCOPE = [
    "services/knowledge", "libs/knowledge_sdk", "libs/egress_guard.py", "services/conversation",
    # Shared with the knowledge path: the JWT secret and the secret-encryption key must match across services.
    "services/config/auth.py", "libs/config_sdk/secrets.py", "libs/config_sdk/secret_resolver.py",
]
DOC = ROOT / "docs" / "knowledge-env-vars.md"
HELPER_RE = re.compile(r"^_?(env|getenv)(_[a-z]+)?$")
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
TYPE_BY_HELPER = {"float": "float", "int": "int", "flag": "flag (0/1)", "bool": "bool"}


@dataclass
class Read:
    name: str
    default: str
    type: str
    where: list[str] = field(default_factory=list)


def _files() -> list[Path]:
    out: list[Path] = []
    for rel in SCOPE:
        p = ROOT / rel
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            out += [f for f in sorted(p.rglob("*.py")) if "/tests/" not in f.as_posix() and "/generated/" not in f.as_posix()]
    return out


def _consts(tree: ast.Module) -> dict[str, str]:
    """Module-level NAME = "STRING" constants, so os.environ.get(_TIMEOUT_ENV) resolves."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = node.value.value
    return out


def _name_of(node: ast.AST, consts: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def _is_environ(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "environ" and isinstance(node.value, ast.Name) and node.value.id == "os"


def _field_defaults(tree: ast.Module) -> dict[str, ast.expr]:
    """Dataclass field defaults (name: type = value), so a config read like env_float(NAME, d.timeout_s) shows its value."""
    out: dict[str, ast.expr] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            out.setdefault(node.target.id, node.value)
    return out


class _Inline(ast.NodeTransformer):
    def __init__(self, fields: dict[str, ast.expr]) -> None:
        self.fields = fields

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        if isinstance(node.value, ast.Name) and node.value.id in ("d", "defaults", "self", "cls") and node.attr in self.fields:
            return self.fields[node.attr]
        return node


def _show(node: ast.expr, fields: dict[str, ast.expr]) -> str:
    node = _Inline(fields).visit(ast.fix_missing_locations(ast.parse(ast.unparse(node), mode="eval")).body)
    try:
        value = eval(compile(ast.Expression(ast.fix_missing_locations(node)), "<d>", "eval"), {"__builtins__": {}, "str": str, "max": max, "min": min, "int": int, "float": float}, {})  # noqa: S307
        if isinstance(value, str) and re.fullmatch(r"-?\d+(\.\d+)?", value):
            value = float(value) if "." in value else int(value)
        if isinstance(value, float) and value == int(value):
            value = int(value)
        return repr(value) if not isinstance(value, float) else f"{value:g}"
    except Exception:  # noqa: BLE001 - not a constant expression: show the source, shortened
        text = ast.unparse(node)
        return text if len(text) <= 36 else "derived (see code)"


def _infer_type(default: str, hint: str | None) -> str:
    if hint:
        return hint
    if default in ("None", "''", '""'):
        return "str"
    if re.fullmatch(r"-?\d+", default):
        return "int"
    if re.fullmatch(r"-?\d*\.\d+", default):
        return "float"
    return "str"


def collect() -> dict[str, Read]:
    reads: dict[str, Read] = {}

    def add(name: str, default: str, type_: str, path: Path, line: int) -> None:
        if not NAME_RE.match(name):
            return
        r = reads.setdefault(name, Read(name, default, type_))
        if r.default in ("(required)", "None") and default not in ("(required)", "None"):
            r.default, r.type = default, type_
        r.where.append(f"{path.relative_to(ROOT).as_posix()}:{line}")

    for path in _files():
        tree = ast.parse(path.read_text(), filename=str(path))
        consts = _consts(tree)
        fields = _field_defaults(tree)
        parents = {id(c): p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and _is_environ(node.value):
                name = _name_of(node.slice, consts)
                if name:
                    add(name, "(required)", "str", path, node.lineno)
            elif isinstance(node, ast.Call) and node.args:
                f = node.func
                fname = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
                is_env_get = isinstance(f, ast.Attribute) and fname in ("get", "setdefault", "pop") and _is_environ(f.value)
                is_getenv = isinstance(f, ast.Attribute) and fname == "getenv" and isinstance(f.value, ast.Name) and f.value.id == "os"
                helper = HELPER_RE.match(fname) if fname and not is_env_get else None
                if not (is_env_get or is_getenv or helper):
                    continue
                if fname == "pop":
                    continue
                name = _name_of(node.args[0], consts)
                if not name:
                    continue
                default = _show(node.args[1], fields) if len(node.args) > 1 else (
                    ast.unparse(node.keywords[0].value) if node.keywords and node.keywords[0].arg == "default" else "(required)"
                    if is_env_get or is_getenv else "(unset)")
                if (is_env_get or is_getenv) and len(node.args) == 1:
                    default = "None"
                hint = TYPE_BY_HELPER.get(fname.rsplit("_", 1)[-1]) if helper else None
                outer = parents.get(id(node))
                if hint is None and isinstance(outer, ast.Call) and isinstance(outer.func, ast.Name) \
                        and outer.func.id in ("int", "float", "bool"):
                    hint = outer.func.id
                add(name, default, _infer_type(default, hint), path, node.lineno)
    # Anything else spelled like our own variables (e.g. a name kept in a tuple or built into a config table).
    for path in _files():
        for m in re.finditer(r'["\'](KNOWLEDGE_[A-Z0-9_]+|KB_[A-Z0-9_]+)["\']', path.read_text()):
            line = path.read_text()[: m.start()].count("\n") + 1
            if m.group(1) not in reads:
                add(m.group(1), "(see code)", "str", path, line)
    return dict(sorted(reads.items()))


def render(reads: dict[str, Read]) -> str:
    sys.path.insert(0, str(ROOT / "scripts"))
    import knowledge_env_meanings as M

    groups: dict[str, list[Read]] = {g: [] for g, _ in M.GROUPS}
    for r in reads.values():
        groups.setdefault(M.group_of(r.name), []).append(r)
    lines = [M.INTRO.strip(), ""]
    for g, blurb in M.GROUPS:
        rows = groups.get(g) or []
        if not rows:
            continue
        lines += [f"## {g}", "", blurb, "", "| Variable | Default | Type | Meaning | Read at |", "|---|---|---|---|---|"]
        for r in rows:
            meaning, flags = M.meaning_of(r.name)
            badge = "".join(f" **[{f}]**" for f in flags)
            default = r.default.replace("|", "\\|")
            where = ", ".join(f"`{w}`" for w in r.where[:3]) + (f" (+{len(r.where) - 3})" if len(r.where) > 3 else "")
            lines.append(f"| `{r.name}` | `{default}` | {r.type} | {meaning}{badge} | {where} |")
        lines.append("")
    lines += [M.OUTRO.strip(), ""]
    return "\n".join(lines)


def main() -> int:
    text = render(collect())
    if "--check" in sys.argv:
        if not DOC.exists() or DOC.read_text() != text:
            print("docs/knowledge-env-vars.md is stale: run python scripts/gen_knowledge_env_doc.py", file=sys.stderr)
            return 1
        return 0
    DOC.write_text(text)
    print(f"wrote {DOC.relative_to(ROOT)}: {len(collect())} variables")
    return 0


if __name__ == "__main__":
    sys.exit(main())
