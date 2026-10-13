"""KNOWLEDGE_BINARY_PARSERS=0: no lxml/libxml2 anywhere (HTML through html.parser, PDF/Office refused, CSV/text pure
Python), with the same hidden-content stripping and limits. The HTML tests of test_parser_hardening are re-run here
under the pure-Python backend."""

from __future__ import annotations

import subprocess
import sys

import pytest

from services.knowledge.parsers import BINARY_DISABLED_REASON, Limits, ParseRejected, detect, formats, parse_document, sandbox
from services.knowledge.tests import test_parser_hardening as H
from services.knowledge.tests.corpus import builders as B


@pytest.fixture(autouse=True)
def _pure_python_mode(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_BINARY_PARSERS", "0")


# The same tests, now parsed by html.parser in the child (Limits.from_env() sees the switch).
_REUSED = [n for n in dir(H) if n.startswith("test_") and any(k in n for k in (
    "html", "stylesheet", "stray_close", "zero_size", "children_inheriting", "sizes_that", "tiny_sizes", "unknown_offsets",
    "white_text", "collapsed", "deeply_nested", "unclosed_optional", "keep_collapsed"))
    and "kill_switch" not in n and "upload_route" not in n and "rejected_not_leaked" not in n]
globals().update({n: getattr(H, n) for n in _REUSED})


def test_the_reused_html_tests_were_collected():
    assert len(_REUSED) >= 25


async def test_pdf_and_office_are_refused_and_the_child_never_starts_for_them():
    for raw in (B.text_pdf(), B.docx_with_tables(), B.pptx_deck(), B.xlsx_sheet()):
        with pytest.raises(ParseRejected) as e:
            await parse_document(raw)
        assert e.value.reason == BINARY_DISABLED_REASON
    # and the child itself refuses, whatever the parent decided
    for ct in (detect.PDF, detect.DOCX, detect.PPTX, detect.XLSX):
        with pytest.raises(ParseRejected):
            formats.parse_by_type(b"x", ct, Limits(binary_parsers=False))


async def test_html_csv_markdown_and_text_still_parse_through_the_real_sandbox():
    html = await parse_document(B.HTML_PAGE)
    assert html.parser_used == "bs4-main" and "30 days of delivery" in html.text() and "LOGIN-NAV-LINK" not in html.text()
    assert (await parse_document(B.CSV_DATA)).parser_used == "csv"
    assert "Policy" in (await parse_document(b"# Policy\n\nBody text here.\n")).text()
    assert (await parse_document(b"Plain text of a sensible length.")).parser_used == "text"


_PROBE = r"""
import json, sys
sys.path.insert(0, {root!r})
from services.knowledge.parsers import _worker
_worker.block_native_xml()
from services.knowledge.parsers import formats
from services.knowledge.parsers.limits import Limits
from services.knowledge.tests.corpus import builders as B
limits = Limits(binary_parsers=False)
docs = [formats.parse_by_type(B.HTML_PAGE, "text/html", limits),
        formats.parse_by_type(B.CSV_DATA, "text/csv", limits),
        formats.parse_by_type(b"# Title\n\nSome markdown body text here.\n", "text/markdown", limits),
        formats.parse_by_type(b"Plain text of a sensible length.", "text/plain", limits)]
loaded = sorted(k for k, v in sys.modules.items() if v is not None and (k == "lxml" or k.startswith("lxml.")))
try:
    import lxml
    importable = True
except ImportError:
    importable = False
print(json.dumps({{"loaded": loaded, "importable": importable, "parsers": [d.parser_used for d in docs]}}))
"""


def test_lxml_is_never_imported_in_pure_python_mode():
    r = subprocess.run([sys.executable, "-c", _PROBE.format(root=sandbox._REPO_ROOT)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-800:]
    import json
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["loaded"] == [] and out["importable"] is False and out["parsers"] == ["bs4-main", "csv", "text", "text"]


def test_with_lxml_poisoned_the_lxml_backend_cannot_be_selected():
    """Why the mutation test fails: asking for the lxml tree builder under the block raises, it does not fall back."""
    code = ("import sys\nsys.path.insert(0, %r)\nfrom services.knowledge.parsers import _worker\n_worker.block_native_xml()\n"
            "from bs4 import BeautifulSoup\ntry:\n    BeautifulSoup('<p>x</p>', 'lxml')\n    print('used')\nexcept Exception as e:\n    print(type(e).__name__)\n"
            % sandbox._REPO_ROOT)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert r.stdout.strip() == "FeatureNotFound"


async def test_the_flag_reaches_the_child_through_limits():
    from services.knowledge.parsers import limits as L
    assert L.Limits.from_env().binary_parsers is False
    # a poisoned child cannot parse HTML with lxml: a successful real-sandbox parse proves html.parser was used
    doc = await sandbox.run_parse(B.HTML_PAGE, detect.HTML, Limits(binary_parsers=False))
    assert doc.parser_used == "bs4-main" and "30 days of delivery" in doc.text()


def test_the_two_backends_agree_on_the_corpus_page():
    """Same page, both builders in-process (no poisoning): identical extracted text for well-formed markup."""
    pure = formats.parse_by_type(B.HTML_PAGE, "text/html", Limits(binary_parsers=False))
    native = formats.parse_by_type(B.HTML_PAGE, "text/html", Limits(binary_parsers=True))
    formats._LIMITS = Limits()
    assert pure.text() == native.text()
