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
from xml.etree import ElementTree

import pytest
from conftest import ROOT, code_only

STATIC = ROOT / "src" / "optjournal" / "static"
#: Every pure module the page imports. A TUPLE rather than one path, because the
#: boundary tests below are properties of the SEAM and not of one file: the second
#: module arrived (watch.js, the watchlist's price and direction derivations) and
#: the checks that had been keeping replay.js honest for a year would have covered
#: none of it. Widened in the same diff that added the file, so the seam never has
#: an unpoliced member.
MODULES = (STATIC / "replay.js", STATIC / "watch.js")
SUITE = ROOT / "tests" / "frontend"
PAGE = ROOT / "src" / "optjournal" / "page.html"


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_the_module_exists_where_the_page_and_the_tests_both_expect_it(module):
    assert module.is_file(), f"no module at {module}"
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


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
@pytest.mark.parametrize("token", _BROWSER_ONLY)
def test_the_pure_module_touches_no_browser_api(token, module):
    source = code_only(module.read_text())
    assert token not in source, (
        f"{module.name} references {token!r}. Move it to page.html: these modules "
        "have to stay importable by node, with no DOM and no globals."
    )


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_the_page_imports_the_module_rather_than_duplicating_it(module):
    """Two copies of the same scale is worse than one untested copy: the tests
    would pass against a function the page no longer runs.
    """
    page = PAGE.read_text()
    assert f"/static/{module.name}" in page, f"page.html does not import {module.name}"
    assert 'type="module"' in page, "an ES module needs a module script tag"


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_the_page_imports_exactly_what_it_calls(module):
    """No stale names in the import list, and nothing called without importing.

    Both directions, because they fail differently. An unused import is a quiet
    lie about what the page does -- three of them (`clampIndex`, `domainOf`,
    `markAt`) had accumulated, each a function the page never calls and replay.js
    uses internally, so a reader auditing the seam saw twelve names where nine
    were live. A MISSING import is worse and louder: the page throws a
    ReferenceError at render, which no Python-side test would catch -- and the
    watchlist is one tab of nine, so a browser render of the Dashboard would not
    reach it either.
    """
    page = PAGE.read_text()
    block = re.search(
        rf"import\s*\{{([^}}]*)\}}\s*from\s*'/static/{re.escape(module.name)}'", page
    )
    assert block, f"no {module.name} import block found in page.html"
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
    exported = set(re.findall(r"^export (?:function|const) (\w+)", module.read_text(), re.M))
    referenced = {
        name for name in exported
        if re.search(rf"(?<![\w.]){name}\s*\(", body) or re.search(rf"\b{name}\b", body)
    }
    assert referenced <= imported, (
        f"page.html uses {sorted(referenced - imported)} without importing it, "
        "which is a ReferenceError at render time"
    )


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_the_page_does_not_redefine_what_the_module_exports(module):
    """Catches the specific rot this seam exists to prevent -- a helper copied
    back into the page during a quick fix, leaving the tested version orphaned.
    """
    exported = set(re.findall(r"^export function (\w+)", module.read_text(), re.M))
    assert exported, "no exports found; the extraction regex is wrong"
    page = PAGE.read_text()
    duplicated = sorted(
        name for name in exported if re.search(rf"\bfunction {name}\s*\(", page)
    )
    assert not duplicated, (
        f"page.html redefines {duplicated}, which {module.name} already exports"
    )


def test_every_shipped_svg_is_well_formed_xml():
    """An SVG is XML, and a browser refuses a malformed one outright.

    Written after the favicon shipped with a `--` inside a comment, which is a
    fatal parse error in XML and a non-event in HTML. Nothing caught it: the
    file existed, the server returned 200 with the right MIME type, and the
    only symptom was a browser rendering an error page instead of the mark. The
    repo's prose uses double hyphens everywhere, so the next person to comment
    one of these files is likely to reintroduce it.
    """
    svgs = sorted((ROOT / "src" / "optjournal" / "static").glob("*.svg"))
    assert svgs, "no SVG assets found; this test is guarding nothing"
    for path in svgs:
        try:
            ElementTree.parse(path)
        except ElementTree.ParseError as exc:
            raise AssertionError(
                f"{path.name} is not well-formed XML and will not render in a "
                f"browser: {exc}. A `--` inside an <!-- --> comment is the "
                f"usual cause."
            ) from exc
