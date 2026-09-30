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
from datetime import date

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


def _month_label(month: str) -> str:
    """`format.js` monthLabel, for the one label this test reads."""
    return date(int(month[:4]), int(month[5:7]), 1).strftime("%b %Y")


def test_the_dashboard_renders_from_the_payload(served, tmp_path):
    if not browser.browsers():
        pytest.skip("no Chrome/Chromium on this machine")
    dom = browser.dump_dom(served + "/", tmp_path / "profile")
    if dom is None:
        pytest.skip("no browser produced a DOM (environment, not the page)")
    assert not browser.last_console_errors(), (
        "the page raised in the browser: " + "; ".join(browser.last_console_errors())
    )

    with urllib.request.urlopen(served + "/api/state") as res:
        payload = json.load(res)
    markup = browser.markup(dom)
    text = browser.rendered_text(dom)

    # The tab bar is the first thing draw() emits; its absence means the
    # page JS threw before rendering anything at all.
    assert markup.count('data-tab="') >= 5, "tab bar missing -- page JS crashed on load"

    # Data binding: the month stepper, at the default view, is at the CURRENT
    # month -- the newest of the browsable range, and the end of the walk -- so
    # forward is disabled and back leads to the month before it, or to All time
    # when the account is one month old. The reference app opens the same way.
    # Asks whether the render agrees with the payload it was handed, at the end.
    step = re.search(r'<div class="pstep">(.*?)</div>', markup, re.S)
    assert step, "the period stepper never rendered"
    back, fwd = re.findall(r"<button([^>]*)>", step.group(1))
    assert "disabled" in fwd, "at the current month there is nowhere forward to go"
    rng = payload["month_range"]
    if rng:
        assert _month_label(rng[0]) in step.group(1), "the default is not the current month"
        want = rng[1] if len(rng) > 1 else "all"
        assert f'data-month="{want}"' in back, (
            "one step back from the current month must land on the month before it"
        )

    # Conditional rendering, oracle-driven from the same payload the page
    # fetched: quotes present means the currency toggle exists and offers
    # exactly the base plus each quote; no quotes means no toggle. This is
    # the class the source-analysis tests are structurally blind to.
    if payload["fx"]["quotes"]:
        assert '<div class="ccytog">' in markup
        for code in {payload["fx"]["base"], *(q["code"] for q in payload["fx"]["quotes"])}:
            assert code in text, f"currency toggle is missing {code}"
    else:
        assert '<div class="ccytog">' not in markup

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
    kicker = re.search(r'<div class="kicker" id="kicker">(.*?)</div>', markup, re.S)
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
    assert 'data-theme="leather"' in markup, (
        "the default theme was never applied to <html>, so the edition chip and "
        "the palette can disagree"
    )
    assert re.search(r'<button class="edition" id="edition"', markup), (
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
        ("ledger", "ledger", "Ledger Edition"),
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


def _opening_tags(markup: str, attr: str) -> list[str]:
    return re.findall(rf"<[a-z]+[^>]*\b{attr}=\"[^\"]*\"[^>]*>", markup)


def test_every_drill_down_is_a_keyboard_stop(served, tmp_path):
    """M31: a calendar day with fills and a replay row, on Trades and on Positions,
    opened on click only. No tab stop and no role, so Tab walked past every one
    of them and the drill-downs were out of reach without a mouse; the Market
    strip's days were the one surface that had both. Rendered, so the check sees
    the markup the reader's browser builds rather than a template.
    """
    if not browser.browsers():
        pytest.skip("no Chrome/Chromium on this machine")
    found = {}
    for tab, attr in (("calendar&month=all", "data-calday"),
                      ("trades", "data-replay"), ("positions", "data-replay")):
        dom = browser.dump_dom(f"{served}/#tab={tab}", tmp_path / f"kbd-{tab[:5]}")
        if dom is None:
            pytest.skip("no browser produced a DOM (environment, not the page)")
        tags = _opening_tags(browser.markup(dom), attr)
        found[tab] = len(tags)
        for tag in tags:
            assert 'tabindex="0"' in tag, f"{tab}: not a tab stop: {tag}"
            if tab != "positions":
                assert 'role="button"' in tag, f"{tab}: no button role: {tag}"
    assert found["calendar&month=all"] and found["trades"], (
        f"nothing to check, so this proved nothing: {found}")


def test_the_0dte_tab_works_on_typed_readings_before_any_index_bar(
        served, tmp_path, monkeypatch):
    """L45: with no S&P or VIX bars stored (this demo journal has none), the tab
    replaced the whole calculator with a note, so there were no fields to type
    into and a `#spx=…&vix=…` link drew nothing. It now draws the fields, holds
    the linked readings and builds the ladder from them, with the note above.

    Opening the tab asks the server to refresh the index bars, which is a Yahoo
    request: `serialize` holds its own `fetch_bars`, so the suite's autouse patch
    on `bars` does not reach it, and it is patched here.
    """
    if not browser.browsers():
        pytest.skip("no Chrome/Chromium on this machine")
    from optjournal import serialize  # noqa: PLC0415 - the server's own binding
    monkeypatch.setattr(serialize, "fetch_bars", lambda *a, **k: [])
    with urllib.request.urlopen(served + "/api/state") as res:
        assert json.load(res)["odte"]["context"] is None, "the fixture now has index bars"
    dom = browser.dump_dom(f"{served}/#tab=odte&spx=7000&vix=20", tmp_path / "odte")
    if dom is None:
        pytest.skip("no browser produced a DOM (environment, not the page)")
    markup = browser.markup(dom)
    assert re.search(r'<input id="zspx"[^>]*value="7000"', markup), "no S&P field"
    assert re.search(r'<input id="zvix"[^>]*value="20"', markup), "no VIX field"
    assert '<table class="zlad">' in markup, "the typed readings drew no ladder"
    assert "Not available yet" in browser.rendered_text(dom), "the note went missing"


def test_the_dashboard_renders_the_tiles_the_reader_stored(served, tmp_path):
    """The chosen arrangement, in its order -- and the default when what is
    stored cannot be drawn.

    The second half is the one only a browser can check. The server refuses a
    bad list on the way in, but `.optjournal.json` is hand-editable and a tile
    can be retired after a reader chose it, and the page's own check in
    `tileKeys` is all that stands between that file and a dashboard that
    throws halfway through rendering. Source analysis can see the check exists;
    only an executed render shows it holds.
    """
    if not browser.browsers():
        pytest.skip("no Chrome/Chromium on this machine")
    from optjournal import settings  # noqa: PLC0415 - the session's settings home

    def rendered_tiles(profile: str) -> list[str]:
        dom = browser.dump_dom(served + "/", tmp_path / profile)
        if dom is None:
            pytest.skip("no browser produced a DOM (environment, not the page)")
        assert not browser.last_console_errors(), (
            "the page raised: " + "; ".join(browser.last_console_errors())
        )
        return re.findall(r'data-tile="([a-z_]+)"', browser.markup(dom))

    chosen = ["win_rate", "net_pnl", "profit_factor", "avg_pnl"]
    try:
        settings.update(tiles=chosen)
        assert rendered_tiles("chosen") == chosen, "the stored order did not render"
        # A hand-edited file holding a count the grid cannot divide.
        settings.update(tiles=["net_pnl", "trades", "wins"])
        assert rendered_tiles("bad") == list(web.TILE_DEFAULT), (
            "a stored arrangement the grid cannot hold was drawn instead of the default"
        )
    finally:
        settings.update(tiles=None)
