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
