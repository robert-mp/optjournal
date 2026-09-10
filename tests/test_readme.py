"""The module table, against the modules that exist.

That table is the map a contributor reads first, and it had silently lost two
entries: `strategies.py` (317 lines, feeding two payload keys) and
`static/replay.js` (the unit-tested chart arithmetic). A map missing a
load-bearing file is worse than no map, because the reader concludes the file is
incidental -- which is exactly the wrong thing to believe about the module that
decides what counts as one trade.

Only presence is checked, never wording. A test that asserted on descriptions
would fail on every honest improvement to them, so it would be deleted within a
week and the drift would come back.

The table lives in `docs/architecture.md`, not the README: the README is the
install-and-run document and the design rationale moved out of it. This guard
follows the content rather than the filename -- searching only the README would
have kept passing on a file that no longer holds the map, which is the silent
no-op the whole module exists to prevent.
"""

from __future__ import annotations

import re

from conftest import ROOT

#: The document holding the map. One name, so moving the table again is one edit
#: here rather than a hunt through three regexes.
ARCHITECTURE = ROOT / "docs" / "architecture.md"
README = ROOT / "README.md"
PACKAGE = ROOT / "src" / "optjournal"

#: Files that are deliberately absent from the table.
_NOT_IN_TABLE = {
    # Exports the version and installs the compat shim; nothing to own.
    "__init__.py",
}


def _table_body() -> str:
    """The `| module | owns |` table, which is the map under test."""
    # `\Z` as a terminator, not just a blank line or a following heading: the
    # table is the last thing in the file, and without it this regex matched
    # nothing and the guard failed as though the table were gone.
    match = re.search(
        r"\| module \| owns \|(.*?)(?:\n\n|\n#|\Z)",
        ARCHITECTURE.read_text(encoding="utf-8"),
        re.S,
    )
    assert match, (
        f"{ARCHITECTURE.relative_to(ROOT)} no longer has a `| module | owns |` "
        "table. If it moved, point ARCHITECTURE at the new file -- do not delete "
        "this guard, or a shipped module can go undocumented in silence."
    )
    return match.group(1)


def test_every_shipped_module_is_in_the_table():
    """A new module must be described where contributors look for the map.

    Cheap to satisfy -- one row, in the same diff that adds the file -- and it
    is the only thing standing between this table and the slow rot that already
    dropped two entries from it.
    """
    body = _table_body()
    shipped = {p.name for p in PACKAGE.glob("*.py")} - _NOT_IN_TABLE
    shipped |= {p.name for p in PACKAGE.glob("*.html")}
    shipped |= {p.name for p in (PACKAGE / "static").glob("*.js")}

    missing = sorted(name for name in shipped if name not in body)
    assert not missing, (
        f"these modules ship but the README's module table does not mention "
        f"them: {missing}. Add a row saying what each owns."
    )


def test_the_table_names_no_module_that_was_deleted():
    """The other direction: a row outliving its file points the reader at
    nothing, and is how a table starts being distrusted wholesale."""
    body = _table_body()
    named = set(re.findall(r"`([A-Za-z_][\w./]*\.(?:py|html|js))`", body))
    # A row may name a file anywhere in the repo -- a description can cite the
    # test that binds the module to its contract -- so resolve against the whole
    # tree, and on the basename, since rows may qualify a path
    # (`static/replay.js`).
    existing = {
        p.name
        for p in ROOT.rglob("*.py")
        if ".venv" not in p.parts and "build" not in p.parts
    }
    existing |= {p.name for p in PACKAGE.rglob("*.html")}
    existing |= {p.name for p in PACKAGE.rglob("*.js")}
    stale = sorted(n for n in named if n.rsplit("/", 1)[-1] not in existing)
    assert not stale, f"the module table names files that no longer exist: {stale}"


def test_the_test_count_in_the_development_section_is_not_wildly_stale():
    """The README quotes a test count as a sanity signal for a fresh clone.

    Bounded rather than exact, because pinning it would fail on every commit
    that adds a test and would be deleted for noise. An order-of-magnitude drift
    means the number has stopped describing the suite at all.
    """
    text = README.read_text(encoding="utf-8")
    quoted = re.search(r"uv run pytest -q\s+#\s*(\d+) tests", text)
    if quoted is None:
        return  # the README stopped quoting a count; nothing to check
    claimed = int(quoted.group(1))
    actual = sum(
        len(re.findall(r"^def test_", path.read_text(encoding="utf-8"), re.M))
        for path in (ROOT / "tests").glob("test_*.py")
    )
    # Generous: parametrized cases make the real total higher than the count of
    # test functions, so this only catches a number that has stopped tracking.
    assert 0.4 * actual <= claimed <= 4 * actual, (
        f"the README claims {claimed} tests; there are {actual} test functions "
        f"(more once parametrized). Update the figure."
    )
