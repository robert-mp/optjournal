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

from optjournal import browser, sweep, web
from optjournal.bars import upsert_bars
from optjournal.clock import epoch_et
from optjournal.demo import _UNDERLYING_CONID, write_demo_statement
from optjournal.ingest import ingest_file
from optjournal.marketdata import Bar, BarFetchError


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

    # The stat-card grid actually populated, in the Annual table's unit: a tile
    # counting closes says closes.
    for tile in ("Closes", "Winning Closes", "Losing Closes", "L / Close",
                 "Commissions"):
        assert tile in text, f"no {tile} tile"
    assert "Winning Trades" not in text and "L / Trade" not in text

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
    names = set(payload["logbook"]["carrying"])
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
    the markup the reader's browser builds rather than a template. A Trades row
    opens its card, and like a Positions row it is a table row, so it takes no
    button role.
    """
    if not browser.browsers():
        pytest.skip("no Chrome/Chromium on this machine")
    found = {}
    for tab, attr in (("calendar&month=all", "data-calday"),
                      ("trades", "data-topen"), ("positions", "data-replay")):
        dom = browser.dump_dom(f"{served}/#tab={tab}", tmp_path / f"kbd-{tab[:5]}")
        if dom is None:
            pytest.skip("no browser produced a DOM (environment, not the page)")
        tags = _opening_tags(browser.markup(dom), attr)
        found[tab] = len(tags)
        for tag in tags:
            assert 'tabindex="0"' in tag, f"{tab}: not a tab stop: {tag}"
            if tab.startswith("calendar"):
                assert 'role="button"' in tag, f"{tab}: no button role: {tag}"
    assert found["calendar&month=all"] and found["trades"], (
        f"nothing to check, so this proved nothing: {found}")


def _rail(markup: str) -> tuple[list[str], str, list[tuple[str, bool]]]:
    """The rail's labels, the one marked current, and the sub-view buttons with
    whether each is pressed."""
    nav = re.search(r'<nav class="rail"[^>]*>(.*?)</nav>', markup, re.S).group(1)
    items = re.findall(r'<button class="tab( on)?"[^>]*>.*?</span>([^<]*)</button>', nav, re.S)
    sub = re.search(r'<div class="seg subnav">(.*?)</div>', markup, re.S)
    leaves = re.findall(r'<button data-tab="[^"]*" class="(on)?"\s*>([^<]*)</button>',
                        sub.group(1)) if sub else []
    return ([label.strip() for _, label in items],
            next(label.strip() for on, label in items if on),
            [(label, bool(on)) for on, label in leaves])


def test_the_rail_groups_the_holdings_under_portfolio_and_old_links_still_land(
        served, tmp_path):
    """Trades stands alone, Portfolio holds the account value then its holdings,
    and Analysis leads with the years. The leaves keep their keys, so a link to
    `#tab=positions` or `#tab=costs` written before the regrouping opens the
    same view, now under its new destination."""
    if not browser.browsers():
        pytest.skip("no Chrome/Chromium on this machine")
    seen = {}
    for leaf in ("trades", "positions", "portfolio", "costs", "review"):
        dom = browser.dump_dom(f"{served}/#tab={leaf}", tmp_path / f"rail-{leaf}")
        if dom is None:
            pytest.skip("no browser produced a DOM (environment, not the page)")
        seen[leaf] = _rail(browser.markup(dom))
    rail = ["Dashboard", "Calendar", "Trades", "Portfolio", "Analysis", "0DTE",
            "Market", "Watchlist"]
    assert seen["trades"] == (rail, "Trades", []), seen["trades"]
    assert seen["positions"][1:] == ("Portfolio", [("Portfolio", False), ("Holdings", True)])
    assert seen["portfolio"][1:] == ("Portfolio", [("Portfolio", True), ("Holdings", False)])
    assert seen["costs"][1:] == (
        "Analysis", [("Annual", False), ("Costs", True), ("Journal", False)])
    assert seen["review"][1:] == (
        "Analysis", [("Annual", False), ("Costs", False), ("Journal", True)])


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


def test_the_dashboard_states_its_net_pnl_once(served, tmp_path, monkeypatch):
    """Net P&L sat in the header, in the first tile and in the chart's corner,
    and two pills repeated the Open Premium and Commissions tiles. The
    whole-journal 0DTE comparison sat under a month it ignored. The tile keeps
    the figure, the header keeps it where no tile does (Calendar), and the
    comparison is on the 0DTE tab."""
    if not browser.browsers():
        pytest.skip("no Chrome/Chromium on this machine")
    from optjournal import serialize  # noqa: PLC0415 - the server's own binding
    monkeypatch.setattr(serialize, "fetch_bars", lambda *a, **k: [])
    seen = {}
    for tab in ("dashboard", "calendar", "odte"):
        dom = browser.dump_dom(f"{served}/#tab={tab}", tmp_path / f"once-{tab}")
        if dom is None:
            pytest.skip("no browser produced a DOM (environment, not the page)")
        seen[tab] = (browser.markup(dom), browser.rendered_text(dom))
    dash, dash_text = seen["dashboard"]
    assert 'class="pfig"' not in dash, "the header repeats the first tile"
    assert 'class="pfig"' in seen["calendar"][0], "Calendar lost its period figure"
    perf = dash.split("Performance</h2>", 1)[1].split('<svg class="chart"', 1)[0]
    assert "€" not in perf and "$" not in perf, f"a figure is back in the chart's corner: {perf}"
    pills = [re.sub(r"<[^>]+>", "", pill).strip() for pill in
             re.findall(r'<span class="pill"[^>]*>(.*?)</span>', dash, re.S)]
    assert not [p for p in pills if p.startswith(("open premium", "OPT"))], pills
    assert any(p.startswith("open ") for p in pills), "no tile shows the open count"
    assert "Zero days to expiration" not in dash_text
    assert "Zero days to expiration" in seen["odte"][1]


def test_every_tab_writes_dates_and_counts_the_way_a_reader_does(
        served, tmp_path, monkeypatch):
    """ISO and compact dates leaked onto almost every tab ("From the 20261013
    snapshot", "2025-04-04 → 2026-10-14", "newest 2026-10-14"), the option book
    named contracts by their OSI symbol ("QQQ 261030P00735000"), and counts read
    "2 fill(s)". Dates now read "14 Oct" (the year only when it is not this one),
    a contract reads as its leg does, and a count takes its noun's number."""
    if not browser.browsers():
        pytest.skip("no Chrome/Chromium on this machine")
    from optjournal import serialize  # noqa: PLC0415 - the server's own binding
    monkeypatch.setattr(serialize, "fetch_bars", lambda *a, **k: [])
    leaks = {}
    for tab in sweep.TABS:
        dom = browser.dump_dom(f"{served}/#tab={tab}", tmp_path / f"dates-{tab}")
        if dom is None:
            pytest.skip("no browser produced a DOM (environment, not the page)")
        # What is on screen and what a tooltip says; the footer's path aside.
        text = re.sub(r"\S+\.db\b", "", " ".join(
            [browser.rendered_text(dom),
             *re.findall(r'title="([^"]*)"', browser.markup(dom))]))
        found = (re.findall(r"\b\d{4}-\d{2}-\d{2}\b|\b20\d{6}\b", text)
                 + re.findall(r"\w\(s\)", text)
                 + re.findall(r"\b[A-Z]{1,5} +\d{6}[CP]\d{8}\b", text))
        if found:
            leaks[tab] = found
    assert not leaks, f"machine dates, OSI symbols or (s) plurals on screen: {leaks}"


def test_an_estimated_close_is_marked_where_its_pnl_shows(tmp_path):
    """Same-session P&L priced from the fills was flagged only in the Dashboard's
    banner; the Calendar day, the Trades row, the card's Realised and the event
    line showed it as final. Each now carries a "≈" whose tooltip says the
    statement replaces it, and a journal with no estimate shows none."""
    if not browser.browsers():
        pytest.skip("no Chrome/Chromium on this machine")
    statement = write_demo_statement(tmp_path / "demo", tmp_path / "demo.db")
    conn = connect_migrated(tmp_path / "demo.db")
    ingest_file(conn, statement)
    conn.commit()

    def marks(base: str, card: dict, month: str, profile: str) -> dict[str, list[str]]:
        seen = {}
        for name, hash_ in (("calendar", f"tab=calendar&month={month}"),
                            ("trades", f"tab=trades&replay={card['replay_key']}")):
            dom = browser.dump_dom(f"{base}/#{hash_}", tmp_path / f"{profile}-{name}")
            if dom is None:
                pytest.skip("no browser produced a DOM (environment, not the page)")
            seen[name] = re.findall(r'<span class="approx" title="([^"]*)">≈</span>',
                                    browser.markup(dom))
        return seen

    with web.serve_ephemeral(db_path=tmp_path / "demo.db",
                             archive_dir=statement.parent) as base:
        with urllib.request.urlopen(base + "/api/state?month=all") as res:
            card = next(lc for lc in json.load(res)["lifecycles"]
                        if lc["status"] == "closed" and lc["replay_key"])
        leg = next(leg for ev in card["events"] for o in ev["orders"] for leg in o["legs"]
                   if "C" in leg["open_close"])
        month = leg["first_fill_at"][:7]
        settled = marks(base, card, month, "settled")
        conn.execute("UPDATE trades SET pnl_estimated = 1 WHERE ib_order_id = ?"
                     " AND conid = ?", (leg["ib_order_id"], leg["conid"]))
        conn.commit()
        estimated = marks(base, card, month, "estimated")
    conn.close()
    assert settled == {"calendar": [], "trades": []}, settled
    why = "Estimated from your fills; tomorrow's statement replaces it."
    # Both of the calendar cell's forms, the wide figure and the phone's compact one.
    assert estimated["calendar"] == [why, why], estimated
    assert estimated["trades"] == [why, why, why], (
        f"the row, the card's Realised and the event line: {estimated['trades']}")


def test_one_tap_shows_in_the_journal_review(tmp_path):
    """The review counted triggers only below a plan table that, with no plan
    answered, was every position under "Not reviewed". A tapped trigger now
    leads the review, with the positions not answered yet beside it, and the plan
    table waits for a plan answer."""
    if not browser.browsers():
        pytest.skip("no Chrome/Chromium on this machine")
    statement = write_demo_statement(tmp_path / "demo", tmp_path / "demo.db")
    conn = connect_migrated(tmp_path / "demo.db")
    ingest_file(conn, statement)
    conn.close()
    with web.serve_ephemeral(db_path=tmp_path / "demo.db",
                             archive_dir=statement.parent) as base:
        with urllib.request.urlopen(base + "/api/state") as res:
            closed = [lc for lc in json.load(res)["lifecycles"]
                      if lc["status"] == "closed" and lc["anchor"]]
        request = urllib.request.Request(
            base + "/api/journal", method="POST",
            data=json.dumps({"anchor": closed[0]["anchor"], "exit_trigger": "time"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request) as res:
            assert json.load(res)["ok"]
        dom = browser.dump_dom(f"{base}/#tab=review", tmp_path / "review")
    if dom is None:
        pytest.skip("no browser produced a DOM (environment, not the page)")
    text = re.sub(r"\s+", " ", browser.rendered_text(dom))
    assert "Why they came off" in text
    assert re.search(r"Time-based \(approaching expiry\) 1 ", text), text
    assert f"Not answered yet {len(closed) - 1} " in text, text
    assert "Did the plan hold" not in text


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


@pytest.fixture(scope="module")
def replayed(tmp_path_factory):
    """The demo journal with hourly SPY closes across its same-day 0DTE trade.

    The `served` journal stores no bars, so every replay panel there draws its
    empty state and nothing about time. Two sessions are enough for the 0DTE
    (sold 10:02, bought back 15:30 on 2026-01-16): its own, and the one before
    it that the chart keeps as context. Stamped at each bar's open, as the
    source stamps them.
    """
    root = tmp_path_factory.mktemp("replay")
    statement = write_demo_statement(root / "demo", root / "demo.db")
    conn = connect_migrated(root / "demo.db")
    ingest_file(conn, statement)
    closes = (690.0, 691.0, 692.5, 691.5, 693.0, 692.0, 692.5)
    upsert_bars(
        conn, conid=_UNDERLYING_CONID["SPY"], symbol="SPY", bar_size="1h",
        source="yahoo",
        bars=[Bar(ts=epoch_et(f"{day} {hour:02d}:30:00"), open=close, high=close,
                  low=close, close=close, volume=1)
              for day in ("2026-01-15", "2026-01-16")
              for hour, close in zip(range(9, 16), closes, strict=True)],
    )
    conn.close()
    with web.serve_ephemeral(
        db_path=root / "demo.db", archive_dir=statement.parent,
    ) as base:
        yield base


def test_a_replay_card_seeks_to_the_frame_that_reveals_its_fill(replayed, tmp_path):
    """Each event card seeks to the first frame whose reveal uncovers its fill.

    A point is stamped at its bar's close, the instant the bar is priced at, so
    a fill lies between the previous point and the one its card seeks to. The
    chart once drew each close at the bar's OPEN while the card sought to the
    bar holding the fill: the dot sat right of that frame, hidden by the reveal
    clip, and the card was dark, while the P&L readout already counted the fill.
    Rendered, because the card's seek, the dot's x and the points' x come from
    three places in the page and replay.js, and only the browser puts them on
    one axis.
    """
    if not browser.browsers():
        pytest.skip("no Chrome/Chromium on this machine")
    with urllib.request.urlopen(replayed + "/api/state") as res:
        payload = json.load(res)
    key = next(k for k in payload["replays"] if k.endswith("@2026-01-16"))
    replay = payload["replays"][key]
    assert replay["bar_size"] == "1h" and len(replay["events"]) == 2, (
        "the fixture no longer draws the 0DTE hourly, so this proves nothing")
    dom = browser.dump_dom(f"{replayed}/#tab=trades&replay={key}", tmp_path / "replay")
    if dom is None:
        pytest.skip("no browser produced a DOM (environment, not the page)")
    markup = browser.markup(dom)
    page = sweep.Page(tab="trades", ccy=None, kind=None, calday=None, replay=key,
                      dom=dom, markup=markup, text=browser.rendered_text(dom),
                      payload=payload)
    verdict = sweep.check_replay_renders_from_url(page)
    assert verdict.status == sweep.PASS, verdict.detail

    line = re.search(r'class="pxline" points="([^"]*)"', markup)
    assert line, "no price line drawn"
    xs = [float(pair.split(",")[0]) for pair in line.group(1).split()]
    dots = [float(cx) for cx in re.findall(r'<circle class="fill" cx="([\d.]+)"', markup)]
    assert len(dots) == len(replay["fills"]), "a fill dot is missing"
    cards = re.findall(r'data-rts="(\d+)" data-rseek="(\d+)"', markup)
    assert len(cards) == len(replay["events"])
    for raw_ts, raw_seek in cards:
        seek, dot = int(raw_seek), dots[replay["fills"].index(int(raw_ts))]
        # 0.05 of slack for the one-decimal rounding both coordinates carry.
        assert dot <= xs[seek] + 0.05, (
            f"the fill at {raw_ts} is right of the frame its card seeks to, so the "
            "reveal clip still hides it there")
        assert seek == 0 or dot > xs[seek - 1] + 0.05, (
            f"the fill at {raw_ts} was already on screen a frame before its card")


def test_the_watchlist_draws_an_iv_rank_column_only_once_a_rank_is_in(
        tmp_path, monkeypatch):
    """The IVR column was a column of dashes for the ten seconds CBOE takes to
    answer for eleven symbols, and for good when every request failed: ranks are
    fetched when the tab opens and never stored. It is drawn once a rank is in.
    The daily reading beside it is labelled "bx daily", since a bare "daily" over
    -39.0 reads as a percentage change.
    """
    if not browser.browsers():
        pytest.skip("no Chrome/Chromium on this machine")
    from optjournal import iv  # noqa: PLC0415 - the server's own binding
    statement = write_demo_statement(tmp_path / "demo", tmp_path / "demo.db")
    conn = connect_migrated(tmp_path / "demo.db")
    ingest_file(conn, statement)
    conn.execute("INSERT INTO watchlist (symbol, added_at) VALUES ('AMD', '2026-10-01')")
    conn.commit()
    conn.close()

    def headers(profile: str) -> list[str]:
        dom = browser.dump_dom(f"{base}/#tab=watchlist", tmp_path / profile)
        if dom is None:
            pytest.skip("no browser produced a DOM (environment, not the page)")
        markup = browser.markup(dom)
        return [re.sub(r"<[^>]+>|[⇅↑↓]", "", cell).strip() for cell in
                re.findall(r'<th[^>]*>(.*?)</th>', markup.split('class="wtab"')[1], re.S)]

    def no_quote(symbol: str):
        raise BarFetchError("offline")

    monkeypatch.setattr(web, "fetch_quote", no_quote)
    # Both loads ask CBOE: the second must not reuse the first's "not carried".
    monkeypatch.setattr(web, "IV_RANK_MAX_AGE_S", 0)
    with web.serve_ephemeral(db_path=tmp_path / "demo.db",
                             archive_dir=statement.parent) as base:
        monkeypatch.setattr(web, "fetch_iv_rank", lambda symbol: None)
        unranked = headers("unranked")
        monkeypatch.setattr(web, "fetch_iv_rank", lambda symbol: iv.IvRank(
            symbol=symbol, iv30=40.0, low=20.0, high=60.0, rank=50.0,
            as_of="2026-10-15 10:00:00"))
        ranked = headers("ranked")
    assert "bx daily" in unranked and "daily" not in unranked
    assert "ivr" not in unranked, f"a column with no rank in it was drawn: {unranked}"
    assert "ivr" in ranked, f"a ranked watchlist lost its IV rank column: {ranked}"
