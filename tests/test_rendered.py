"""The page executed once, in a real browser engine, against a demo journal.

A third of the web tests are regex analysis of the page's JS source, and three
bug classes are structurally invisible to that: conditional rendering of
nullable fields, data binding that renders blank or literal "undefined", and a
template error that blanks a whole panel. All three have shipped past a green
suite. One executed render with DOM assertions closes the class, at the cost
of one browser launch.

The skip/fail contract is deliberate: a missing browser or a launch the
machine kills is an ENVIRONMENT problem and skips loudly, but a DOM that came
back and fails an assertion is the PAGE being wrong and fails the suite. A
skip here must never hide a rendering bug -- it may only hide the absence of
Chrome.

Browser discovery and the DOM dump live in ``optjournal.browser``: the
hazards there (a Rosetta Chrome that hangs in every headless variant, the
sandbox that cannot initialise) were probed rather than guessed, and
``sweep.py`` needs the same knowledge -- holding it in two places is how one
copy goes stale.
"""

from __future__ import annotations

import json
import re
import urllib.request

import pytest
from conftest import connect_migrated

from optjournal import browser, web
from optjournal.demo import write_demo_statement
from optjournal.ingest import ingest_file


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    """A live optjournal server over a demo journal, on an ephemeral port.

    Through ``web.serve_ephemeral``, which wires the same ServeConfig and
    handler production uses -- so the render exercises the real request path,
    not a lookalike, and the OS picks the port so the suite never collides with
    a journal already serving on 8765/8766. That spin-up used to be twelve lines
    here and twelve byte-identical lines in sweep.py, both reaching through
    ``web._Handler``.
    """
    root = tmp_path_factory.mktemp("render")
    statement = write_demo_statement(root / "demo", root / "demo.db")
    conn = connect_migrated(root / "demo.db")
    ingest_file(conn, statement)
    conn.close()

    with web.serve_ephemeral(
        db_path=root / "demo.db", archive_dir=statement.parent,
    ) as base:
        yield base


def test_the_dashboard_renders_from_the_payload(served, tmp_path):
    if not browser.browsers():
        pytest.skip("no Chrome/Chromium on this machine")
    dom = browser.dump_dom(served + "/", tmp_path / "profile")
    if dom is None:
        pytest.skip("no browser produced a DOM (environment, not the page)")

    with urllib.request.urlopen(served + "/api/state") as res:
        payload = json.load(res)
    text = browser.rendered_text(dom)

    # The tab bar is the first thing draw() emits; its absence means the
    # page JS threw before rendering anything at all.
    assert dom.count('data-tab="') >= 5, "tab bar missing -- page JS crashed on load"

    # Data binding: the month dropdown holds exactly the browsable range,
    # plus its one "All time" head. An off-by-anything here is the render
    # disagreeing with the payload it was handed.
    sel = re.search(r'<select id="month">(.*?)</select>', dom, re.S)
    assert sel, "the filter bar never rendered"
    assert sel.group(1).count("<option") == len(payload["month_range"]) + 1

    # Conditional rendering, oracle-driven from the same payload the page
    # fetched: quotes present means the currency toggle exists and offers
    # exactly the base plus each quote; no quotes means no toggle. This is
    # the class the source-analysis tests are structurally blind to.
    if payload["fx"]["quotes"]:
        assert '<div class="ccytog">' in dom
        for code in {payload["fx"]["base"], *(q["code"] for q in payload["fx"]["quotes"])}:
            assert code in text, f"currency toggle is missing {code}"
    else:
        assert '<div class="ccytog">' not in dom

    # Catastrophic binding failures do not throw in a template literal --
    # they render as literal junk text. None of these words belong on the
    # page as prose.
    assert not re.search(r"\b(undefined|NaN)\b", text), "a binding rendered as junk"
    assert "[object Object]" not in text

    # The stat-card grid actually populated.
    assert "Trades" in text
    assert "Commissions" in text

    # The header dateline, which SHIPS EMPTY in the markup and is filled by JS.
    # That is precisely the shape source analysis cannot check: a renderKicker
    # that never runs, or throws, leaves the page's most prominent small line
    # blank and every other assertion here still passes. Oracle-driven from the
    # payload the page itself fetched.
    kicker = re.search(r'<div class="kicker" id="kicker">(.*?)</div>', dom, re.S)
    assert kicker, "the kicker slot is gone from the header"
    line = kicker.group(1).strip()
    assert line, "the kicker rendered EMPTY -- nothing filled the slot"
    day = payload["logbook"]["day"]
    assert day, "the demo journal has no first activity, so this proves nothing"
    assert f"Log day {day}" in line, (
        f"the header says {line!r}, which does not carry the payload's day {day}"
    )
    # The open book, named by UNDERLYING. A count here would contradict the
    # Positions tab for a multi-leg position, which is why the line names symbols.
    names = {p["underlying_symbol"] or p["symbol"] for p in payload["positions"]}
    if names:
        assert "carrying" in line.lower(), f"{line!r} omits the open book"
        assert any(n in line for n in names), (
            f"the header names none of the open underlyings {sorted(names)}"
        )
    else:
        assert "flat" in line.lower(), f"{line!r} does not say the book is flat"

    # The default theme is applied by JS on load, so an un-themed URL must still
    # come back with the attribute set -- if `applyTheme` never ran, `:root` would
    # still style the page and every colour assertion would pass while the chip
    # and the switcher were dead.
    assert 'data-theme="leather"' in dom, (
        "the default theme was never applied to <html>, so the edition chip and "
        "the palette can disagree"
    )
    assert re.search(r'<button class="edition" id="edition"', dom), (
        "the edition chip is not a <button>, so switching themes is unreachable "
        "by keyboard"
    )
    assert "Leather Edition" in text


def test_a_themed_url_repaints_the_whole_page(served, tmp_path):
    """A theme in the hash must survive the load, which is the one thing the
    stylesheet alone cannot demonstrate.

    Checks the ATTRIBUTE and the CHIP together: the attribute is what selects the
    palette and the chip is what claims which palette is active, so a page where
    they disagree is lying to the reader. An unknown id is checked too -- it must
    heal to the default rather than leaving `data-theme="nope"` on <html>, where
    no block matches and the page silently renders Leather while the chip says
    otherwise.
    """
    if not browser.browsers():
        pytest.skip("no Chrome/Chromium on this machine")
    for requested, expected, label in (
        ("admiralty", "admiralty", "Admiralty Edition"),
        ("oxblood", "oxblood", "Oxblood Edition"),
        ("nope", "leather", "Leather Edition"),
    ):
        dom = browser.dump_dom(f"{served}/#theme={requested}",
                               tmp_path / f"profile-{requested}")
        if dom is None:
            pytest.skip("no browser produced a DOM (environment, not the page)")
        assert f'data-theme="{expected}"' in dom, (
            f"#theme={requested} did not put data-theme={expected} on <html>"
        )
        assert label in browser.rendered_text(dom), (
            f"#theme={requested} left the edition chip claiming something other "
            f"than {label!r}, so the chip and the palette disagree"
        )
