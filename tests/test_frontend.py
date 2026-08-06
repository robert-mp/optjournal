"""The frontend seam: run the JS unit suite, and police what may live in it.

`pytest` is the one command that has to be green, so the node suite runs from
here rather than needing to be remembered separately. It is SKIPPED, not failed,
when node is absent -- the Python half of this project must stay installable and
testable on a machine with no JavaScript runtime at all.

The boundary tests matter as much as the suite. A seam only keeps its value while
the pure side stays pure: the moment a DOM call lands in replay.js, the module
stops being importable by `node --test` and the tests quietly stop covering the
code that actually runs.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest
from conftest import ROOT, code_only

MODULE = ROOT / "src" / "optjournal" / "static" / "replay.js"
SUITE = ROOT / "tests" / "frontend"
PAGE = ROOT / "src" / "optjournal" / "page.html"


def test_the_module_exists_where_the_page_and_the_tests_both_expect_it():
    assert MODULE.is_file(), f"no module at {MODULE}"
    assert SUITE.is_dir()


@pytest.mark.skipif(shutil.which("node") is None, reason="no node runtime")
def test_the_javascript_suite_passes():
    """One `pytest` covers both languages, so neither half can rot unnoticed."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [shutil.which("node") or "node", "--test", str(SUITE)],
        capture_output=True, text=True, cwd=ROOT, timeout=120, check=False,
    )
    assert result.returncode == 0, (
        "the JS unit suite failed:\n"
        + result.stdout[-4000:] + "\n" + result.stderr[-2000:]
    )
    passed = re.search(r"^# pass (\d+)$", result.stdout, re.M)
    assert passed and int(passed.group(1)) > 0, (
        "node reported no passing tests -- the suite is not being discovered"
    )


#: Anything that only exists in a browser. A seam is only worth having while the
#: pure side stays pure: one `document` here and the module stops being importable
#: by `node --test`, at which point the tests quietly stop covering the code that
#: actually runs.
_BROWSER_ONLY = (
    "document",
    "window",
    "localStorage",
    "setInterval",
    "setTimeout",
    "fetch(",
    "location",
    "innerHTML",
    "querySelector",
)


@pytest.mark.parametrize("token", _BROWSER_ONLY)
def test_the_pure_module_touches_no_browser_api(token):
    source = code_only(MODULE.read_text())
    assert token not in source, (
        f"replay.js references {token!r}. Move it to page.html: this module has "
        "to stay importable by node, with no DOM and no globals."
    )


def test_the_page_imports_the_module_rather_than_duplicating_it():
    """Two copies of the same scale is worse than one untested copy: the tests
    would pass against a function the page no longer runs.
    """
    page = PAGE.read_text()
    assert "/static/replay.js" in page, "page.html does not import the module"
    assert 'type="module"' in page, "an ES module needs a module script tag"


def test_the_page_imports_exactly_what_it_calls():
    """No stale names in the import list, and nothing called without importing.

    Both directions, because they fail differently. An unused import is a quiet
    lie about what the page does -- three of them (`clampIndex`, `domainOf`,
    `markAt`) had accumulated, each a function the page never calls and replay.js
    uses internally, so a reader auditing the seam saw twelve names where nine
    were live. A MISSING import is worse and louder: the page throws a
    ReferenceError at render, which no Python-side test would catch.
    """
    page = PAGE.read_text()
    block = re.search(r"import\s*\{([^}]*)\}\s*from\s*'/static/replay\.js'", page)
    assert block, "no replay.js import block found in page.html"
    imported = {n.strip() for n in block.group(1).split(",") if n.strip()}

    # Comments stripped, or the comment explaining WHY a name was dropped from
    # the import list counts as a use of it and the guard can never go green.
    # Same line the payload-read guard draws, through the same helper.
    body = code_only(page.replace(block.group(0), ""))
    called = {name for name in imported if re.search(rf"\b{name}\b", body)}

    assert imported == called, (
        f"unused imports: {sorted(imported - called)}. Each is a name the page "
        "claims to use and does not; drop it from the import list."
    )
    # The other direction: every exported name the page references must be
    # imported, or it is an undefined identifier at runtime.
    exported = set(re.findall(r"^export (?:function|const) (\w+)", MODULE.read_text(), re.M))
    referenced = {
        name for name in exported
        if re.search(rf"(?<![\w.]){name}\s*\(", body) or re.search(rf"\b{name}\b", body)
    }
    assert referenced <= imported, (
        f"page.html uses {sorted(referenced - imported)} without importing it, "
        "which is a ReferenceError at render time"
    )


def test_the_page_does_not_redefine_what_the_module_exports():
    """Catches the specific rot this seam exists to prevent -- a helper copied
    back into the page during a quick fix, leaving the tested version orphaned.
    """
    exported = set(re.findall(r"^export function (\w+)", MODULE.read_text(), re.M))
    assert exported, "no exports found; the extraction regex is wrong"
    page = PAGE.read_text()
    duplicated = sorted(
        name for name in exported if re.search(rf"\bfunction {name}\s*\(", page)
    )
    assert not duplicated, (
        f"page.html redefines {duplicated}, which replay.js already exports"
    )
