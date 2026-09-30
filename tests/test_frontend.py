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

import json
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
MODULES = (
    STATIC / "format.js",
    STATIC / "market.js",
    STATIC / "replay.js",
    STATIC / "watch.js",
    # The 0DTE calculator's ladder. The first module on this seam whose arithmetic
    # runs on a KEYSTROKE -- both its inputs are typed -- which is why it is here
    # rather than in Python beside the rest of the journal's numbers.
    STATIC / "zdte.js",
)
SUITE = ROOT / "tests" / "frontend"
PAGE = ROOT / "src" / "optjournal" / "page.html"
#: The second document `app.css` dresses and the second importer of these modules:
#: the 0DTE tab's Broker Companion window. Held to the same seam as the page below,
#: because a floating window that recomputed `scratchRead` in its own script would
#: be the exact duplication this directory exists to prevent -- and it would be
#: unexecuted by any test, since only page.html is scanned by the payload contract.
COMPANION = ROOT / "src" / "optjournal" / "companion.html"


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_the_module_exists_where_the_page_and_the_tests_both_expect_it(module):
    assert module.is_file(), f"no module at {module}"
    assert SUITE.is_dir()


@pytest.mark.skipif(shutil.which("node") is None, reason="no node runtime")
def test_the_javascript_suite_passes():
    """One `pytest` covers both languages, so neither half can rot unnoticed."""
    files = sorted(SUITE.glob("*.test.mjs"))
    assert files, f"no JavaScript tests found in {SUITE}"
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [shutil.which("node") or "node", "--test", *(str(path) for path in files)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=ROOT, timeout=120, check=False,
    )
    assert result.returncode == 0, (
        "the JS unit suite failed:\n"
        + result.stdout[-4000:] + "\n" + result.stderr[-2000:]
    )
    passed = re.search(r"^# pass (\d+)$", result.stdout, re.M)
    assert passed and int(passed.group(1)) > 0, (
        "node reported no passing tests -- the suite is not being discovered"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="no node runtime")
def test_the_pages_inline_script_parses(tmp_path):
    """THE PAGE'S OWN 6000 LINES, PARSED. Nothing else here does that.

    Every other frontend check in this repo reads the inline script as TEXT -- the
    contract guard, the layout assertions, the escaping rules -- so a syntax error
    passes all of them and the page simply does not run. There is no build step to
    catch it either. The browser says one line into the console and renders a blank
    document, which is the worst available failure mode: silent, total, and visible
    only if you happen to have devtools open.

    Earned. An HTML comment was added inside a template literal, explaining a change
    to a caption, and it contained a backtick around an identifier -- which ENDS the
    template literal it sits in. The page died with `Unexpected identifier 'i'`
    before rendering a single element, and the full suite stayed green, because the
    helper that extracts the script for analysis strips HTML comments first and
    removed the offending character before looking at it.

    So this parses the RAW script, comments intact, with `node --check`. It catches
    that shape and every other syntax error, which is the right level to check at:
    the specific rule ("no backtick in an HTML comment in a template") would have
    been a rule about one bug rather than about the file being valid JavaScript.

    Imports are not resolved by `--check`, only parsed, so the `/static/*.js`
    specifiers do not need to exist for this to run.
    """
    page = (ROOT / "src" / "optjournal" / "page.html").read_text(encoding="utf-8")
    body = page.split("<script", 1)[1]
    script = body.split(">", 1)[1].split("</script>")[0]
    # `.mjs` so node parses it as a module: the page's script is `type="module"`
    # and top-level `import` is a syntax error in a script.
    scratch = tmp_path / "inline.mjs"
    scratch.write_text(script, encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [shutil.which("node") or "node", "--check", str(scratch)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, check=False,
    )
    assert result.returncode == 0, (
        "page.html's inline script is not valid JavaScript, so the page will render "
        "blank with one line in the console and nothing else in this suite will "
        f"notice:\n{result.stderr[-3000:]}"
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
    source = code_only(module.read_text(encoding="utf-8"))
    assert token not in source, (
        f"{module.name} references {token!r}. Move it to page.html: these modules "
        "have to stay importable by node, with no DOM and no globals."
    )


@pytest.mark.parametrize("module", MODULES, ids=lambda p: p.name)
def test_the_page_imports_the_module_rather_than_duplicating_it(module):
    """Two copies of the same scale is worse than one untested copy: the tests
    would pass against a function the page no longer runs.
    """
    page = PAGE.read_text(encoding="utf-8")
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
    page = PAGE.read_text(encoding="utf-8")
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
    exported = set(re.findall(
        r"^export (?:function|const) (\w+)",
        module.read_text(encoding="utf-8"),
        re.M,
    ))
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
    exported = set(re.findall(
        r"^export function (\w+)", module.read_text(encoding="utf-8"), re.M
    ))
    assert exported, "no exports found; the extraction regex is wrong"
    page = PAGE.read_text(encoding="utf-8")
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


#: What the Broker Companion legitimately needs: the shared formatters and the
#: calculator's arithmetic. Not the whole seam -- it draws no chart and holds no
#: watchlist -- so the two it does import are named rather than parametrising over
#: MODULES, which would assert an import the window has no reason to carry.
_COMPANION_MODULES = (STATIC / "format.js", STATIC / "zdte.js")


@pytest.mark.parametrize("module", _COMPANION_MODULES, ids=lambda p: p.name)
def test_the_companion_imports_the_module_rather_than_duplicating_it(module):
    """The floating window must derive its figures from the same code as the tab.

    This is the failure the window is most likely to grow: it prints three numbers
    from three inputs, which is small enough to retype in its own script, and the
    copy would then drift from the ladder it was opened from -- while looking right
    on the day it was written. A reader mid-trade comparing the two would have no
    way to tell which was lying.
    """
    markup = COMPANION.read_text(encoding="utf-8")
    assert f"/static/{module.name}" in markup, (
        f"companion.html does not import {module.name}"
    )
    assert 'type="module"' in markup, "an ES module needs a module script tag"
    exported = set(re.findall(
        r"^export (?:function|const) (\w+)", module.read_text(encoding="utf-8"), re.M
    ))
    body = code_only(markup)
    duplicated = sorted(
        name for name in exported
        if re.search(rf"\b(?:function|const|let)\s+{name}\b", body)
    )
    assert not duplicated, (
        f"companion.html redefines {duplicated}, which {module.name} exports"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="no node runtime")
def test_the_companion_keeps_the_levels_it_was_last_sent_across_a_reload():
    """L44: the window's hash was only its OPENING state, so a level moved on the
    tab (which posts into the window) was lost on reload and the old one came
    back. The companion's own `take` now writes what it shows into its hash; run
    here under node with `location`, `history` and `render` as the stand-ins.
    """
    script = code_only(COMPANION.read_text(encoding="utf-8")
                       .split('<script type="module">')[1].split("</script>")[0])
    fns = re.findall(r"^function \w+\(.*?\n\}", script, re.S | re.M)
    harness = "\n".join([
        "const location={hash:'#spx=7706.03&call=7780&theme=ledger',pathname:'/companion'};",
        "const history={replaceState(s,t,u){location.hash=u.slice(u.indexOf('#'));}};",
        "function render(){}",
        re.search(r"^const Z=.*?;$", script, re.M).group(0),
        *(fn for fn in fns if not fn.startswith("function render(")),
        "fromHash();",
        "take({spx:'7710.50',call:'7800',put:'7600',theme:'ledger'});",
        "const moved=location.hash; Z.call='';",
        "fromHash();",
        "console.log(JSON.stringify({moved,reloaded:Z}));",
    ])
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [shutil.which("node") or "node", "--input-type=module", "-e", harness],
        capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    out = json.loads(result.stdout)
    assert out["moved"] == "#spx=7710.50&call=7800&put=7600&theme=ledger"
    assert out["reloaded"] == {"spx": "7710.50", "call": "7800", "put": "7600",
                               "theme": "ledger"}


def test_the_companion_wears_the_shared_stylesheet_and_no_rules_of_its_own():
    """Every styling decision stays in app.css, which is where the tests can read
    it -- the same rule that emptied page.html's `<style>` block, applied to the
    second document that block's rules now dress.
    """
    markup = COMPANION.read_text(encoding="utf-8")
    assert '/static/app.css' in markup, "the companion must share the stylesheet"
    assert "<style" not in markup, "a stylesheet was embedded in the companion"
