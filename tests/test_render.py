"""The terminal reports, over the payloads they actually receive.

These exist because `render.py` had no test at all, and it shipped broken:
the Money conversion replaced an order's `proceeds`/`proceeds_base` pair and
an episode's `realized_pnl_base`/`commission_base` with nested `Money`
objects, `render_orders` and `render_history` kept reading the removed flat
keys, and `optjournal orders` and `optjournal history` both died on
`float(dict)`. Every other layer was held to the payload by a test -- the page
by test_web's contract guards, the JSON by the sweep -- so the renderers were
the one consumer nothing bound to the serializer.

The binding here is deliberately the same shape as the page's: the payload is
built by the REAL serializer from a real database rather than hand-written, so
a serializer that changes shape fails these tests instead of only failing at a
terminal. A hand-built dict would have passed the whole time the CLI was
crashing.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime, timedelta

import pytest
from conftest import RAW_DIR, STATEMENTS, connect_migrated

from optjournal.bars import upsert_bars
from optjournal.clock import MARKET_TZ
from optjournal.history import build_history
from optjournal.ingest import ASSET_FILTER_ALL, ingest_file
from optjournal.marketdata import Bar
from optjournal.render import (
    render_history,
    render_orders,
    render_positions,
    render_statements,
    render_summary,
    render_watchlist,
    table,
)
from optjournal.serialize import (
    history_data,
    orders_data,
    positions_data,
    statements_data,
    watchlist_data,
)
from optjournal.trend import MIN_SETTLED, PARAMS_CAPTION
from optjournal.vol import RANK_BANDS_SOURCE, RANK_MIDPOINT, RANK_MIN_WINDOWS


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    """A journal with every archived statement folded in, as the CLI sees it."""
    if not STATEMENTS:
        pytest.skip("needs an archived statement")
    c = connect_migrated(tmp_path / "render.db")
    for path in STATEMENTS:
        ingest_file(c, path, assets=ASSET_FILTER_ALL)
    return c


# The failure mode these close: a renderer reaching a key the serializer no
# longer sends. `float(dict)` raises TypeError, and a missing key raises
# KeyError, so simply calling each renderer over the real payload catches both.


def test_orders_render_over_the_real_payload(conn):
    data = orders_data(conn)
    assert data, "the archive should hold option orders"
    out = render_orders(data)
    assert "option order(s)" in out
    # The Money keys are read through their own halves, not as a dict: a
    # stringified dict would put "{'base'" in the output rather than raising.
    assert "{" not in out


def test_history_renders_both_tables_over_the_real_payload(conn):
    data = history_data(build_history(conn))
    out = render_history(data)
    assert "Position history" in out
    assert "Closed" in out and "Open" in out
    assert "{" not in out


def test_positions_render_over_the_real_payload(conn):
    out = render_positions(positions_data(conn))
    assert "{" not in out


def test_statements_render_over_the_real_payload(conn):
    out = render_statements(statements_data(RAW_DIR, conn))
    assert "archived statement(s)" in out


def test_an_order_shows_the_charge_and_the_translation(conn):
    """Both readings, from one `Money` -- not one figure printed twice.

    The bug replaced `proceeds_base` with a nested object, so the "base"
    column silently became the same source as the proceeds column. Asserting
    they come from different halves is what pins that they still differ.
    """
    orders = orders_data(conn)
    order = next(
        o for o in orders
        if o["proceeds"]["native"] is not None
        and o["proceeds"]["base"] != o["proceeds"]["native"]
    )
    out = render_orders([order])
    assert f"{order['proceeds']['native']:,.2f}" in out
    assert f"{order['proceeds']['base']:,.2f}" in out


def test_a_withheld_native_renders_as_a_dash_not_a_crash():
    """An order spanning currencies has `native: null`, and must still render.

    Hand-built because this account's option orders are all single-currency,
    so real data cannot reach the withheld branch -- the same reason
    test_analysis builds statements for the tax gate.
    """
    order = {
        "ib_order_id": "1", "underlyings": "GOOG", "leg_count": 1, "fills": 1,
        "first_fill_at": "2026-08-04 11:24:00",
        "proceeds": {"base": 373.74, "native": None, "ccy": None},
        "commission": {"base": -0.61, "native": None, "ccy": None},
        "legs": [],
    }
    out = render_orders([order])
    assert "proceeds -" in out
    # The base translation always answers, so the row is never information-free.
    assert "373.74" in out


def test_summary_renders_without_a_statement_path():
    out = render_summary({"statements": [{
        "account_id": "U1", "from_date": "2026-01-01", "to_date": "2026-12-31",
        "trades": 2, "distinct_orders": 2, "underlyings": 1,
        "cash_transactions": 0, "by_asset": {"OPT": 2},
        "by_open_close": {"O": 2}, "cash_by_type": {},
    }]})
    assert "account U1" in out


@pytest.mark.parametrize(
    "render, empty",
    [
        (render_orders, []),
        (render_positions, []),
        (render_statements, []),
        # The watchlist's empty state is a sentence naming the command that fills
        # it, which is this report's whole answer on a fresh journal.
        (render_watchlist, []),
    ],
)
def test_an_empty_payload_says_so_rather_than_raising(render, empty):
    assert render(empty)


# ------------------------------------------------------------ the watchlist
#
# `render_watchlist` had no test at all until this slice, while holding a literal
# header list beside an align string -- the shape where a column added to the body
# and forgotten in the header prints a misaligned table and nothing fails. It now
# carries eleven columns -- four derived, one typed -- so the payload is built with
# the REAL serializer over a seeded journal for the same reason every other case here
# is: a hand-written dict would have passed throughout the window when `orders` and
# `history` were both crashing.


def _watched(conn: sqlite3.Connection, symbol: str, sessions: int) -> None:
    """Seed one watched symbol with `sessions` daily closes, newest ending Friday.

    Local rather than shared with `test_serialize.py`'s version: the closes there
    are the SUBJECT (which arithmetic runs over which sessions), while here they are
    scaffolding for "does the renderer read the shapes the serializer sends". That is
    the line `conftest.py` draws in its own docstring.

    The path's step cycles so realised vol varies window to window, which is what
    gives the rank a range to sit in; a constant step would make the year flat and
    the rank correctly absent, testing the wrong branch.
    """
    day = date(2026, 8, 7)
    days: list[str] = []
    while len(days) < sessions:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day -= timedelta(days=1)
    price = 100.0
    bars = []
    for index, stamp_day in enumerate(reversed(days)):
        step = 0.004 + 0.016 * (1 + math.sin(index / 17.0)) / 2
        price *= math.exp(step if index % 2 == 0 else -step)
        stamp = int(
            datetime.strptime(stamp_day, "%Y-%m-%d")
            .replace(hour=0, tzinfo=MARKET_TZ).timestamp()
        )
        bars.append(Bar(ts=stamp, open=price, high=price, low=price,
                        close=price, volume=1))
    conn.execute(
        "INSERT INTO watchlist (symbol, note, added_at) VALUES (?, NULL, ?)",
        (symbol, days[-1]),
    )
    conn.commit()
    upsert_bars(conn, conid=f"watch:{symbol}", symbol=symbol, bar_size="1d",
                source="yahoo", bars=bars)


def _prose(text: str) -> str:
    """Text with every run of whitespace collapsed, for reading a wrapped note.

    The watchlist's footnotes are wrapped to a terminal width from sentences that
    live in `vol.py` and `trend.py`, so a test that matched them verbatim would be
    asserting where the wrap fell -- which is a property of their length, not of
    what they say.
    """
    return " ".join(text.split())


#: The ET instant every watchlist assertion here is stated against. Fixed, because
#: one column of this report is a COUNTDOWN: read against the real clock, "14d"
#: becomes "today" and then "past" as the calendar moves, and the test would start
#: failing on a date rather than on a change.
_NOW = datetime(2026, 8, 13, 12, tzinfo=MARKET_TZ)


@pytest.fixture
def watched(tmp_path) -> sqlite3.Connection:
    """A journal watching one settled symbol and one thin one.

    Two rows rather than one, because the report's footnotes are per-symbol lists:
    with a single thin row every dash could be explained by "nothing is stored yet",
    which is the case that hides a missing column.

    The settled symbol also carries a recorded earnings date and the thin one does
    not, so the typed column is exercised in both states -- a date beside a dash is
    what a real watchlist looks like, since that column's only source is the reader.
    """
    c = connect_migrated(tmp_path / "watch.db")
    _watched(c, "NVDA", 200)
    _watched(c, "PLTR", 45)
    c.execute("UPDATE watchlist SET earnings_on = '2026-08-27' WHERE symbol = 'NVDA'")
    c.commit()
    return c


def test_the_watchlist_table_prints_every_derived_figure(watched):
    """Eleven columns, and each derived one actually reaches the terminal.

    The figures are read off the payload and looked for as FORMATTED text, so a
    column silently dropped from the header list (which shifts every cell one place
    left) or a body cell that never made it into the row fails here. Asserted per
    figure rather than by a row count, because a count passes just as happily over
    the wrong eleven columns.
    """
    rows = watchlist_data(watched, now=_NOW)
    settled = next(r for r in rows if r["symbol"] == "NVDA")
    assert settled["bx_daily"] is not None and settled["rv_rank"] is not None, (
        "the fixture is meant to seed one symbol past every gate"
    )

    out = render_watchlist(rows)
    for header in ("realised vol", "rvr", "bx daily", "bx weekly", "earnings",
                   "options"):
        assert header in out
    assert f"{settled['rv_rank']:.0f}" in out
    assert f"{settled['bx_daily']:+.1f}" in out
    assert "{" not in out, "a payload value reached the table as a dict"

    # The align string has to cover every column: it defaults to right beyond its
    # own length, so a stale one leaves the last column silently right-aligned.
    lines = out.splitlines()
    header, _sep, first_row = lines[0], lines[1], lines[2]
    assert len({len(line) for line in lines[:3]}) == 1, f"ragged table:\n{out}"
    assert first_row.index("NVDA") == header.index("symbol")
    # The options cell is a dash on a symbol nothing is held against, and it sits
    # under the LEFT edge of its header only while the align string reaches that far.
    assert first_row[header.index("options")] == "-", (
        f"the options column is not left-aligned, so the align string is shorter "
        f"than the header list:\n{out}"
    )


def test_a_thin_symbol_gets_a_dash_and_the_count_it_is_waiting_on(watched):
    """A dash, never a zero -- and a footnote naming the count, per gate.

    45 sessions is the honest state of five of the six real watched symbols, so this
    is the everyday reading rather than an edge. Each gate is named separately
    because a symbol can be past one and short of another: the realised vol answers
    at 21 sessions, the rank wants 120 windows, and the weekly arm wants 120 ISO
    weeks, which is about three years of them.
    """
    rows = watchlist_data(watched, now=_NOW)
    thin = next(r for r in rows if r["symbol"] == "PLTR")
    assert thin["bx_daily"] is None and thin["rv_rank"] is None

    # The footnotes are wrapped to a terminal width, so they are read as prose
    # rather than as lines: the sentences come from constants in `vol` and `trend`,
    # and asserting on line breaks would be asserting on their length.
    prose = _prose(render_watchlist(rows))
    assert f"PLTR ({thin['closes']} of {MIN_SETTLED} sessions)" in prose
    assert f"PLTR ({thin['weeks']} of {MIN_SETTLED} weeks)" in prose
    assert f"PLTR ({thin['rv_rank_windows']} of {RANK_MIN_WINDOWS} windows)" in prose

    # And the row itself dashes rather than zeroing: five of its eleven cells are a
    # bare dash, which is what the reader sees instead of a neutral-looking figure.
    row_line = next(
        line for line in render_watchlist(rows).splitlines()
        if line.strip().startswith("PLTR")
    )
    assert row_line.split().count("-") == 5, (
        f"expected a dash for the rank, both arms, the earnings date nobody has "
        f"recorded and the options cell:\n{row_line}"
    )


def test_the_earnings_column_prints_the_typed_date_and_a_dash_for_none(watched):
    """One typed date with its derived countdown, one dash, and the provenance.

    The two states side by side are the point: this column's only source is the
    reader, so a dash is its NORMAL state and it has to be distinguishable from a
    date. A blank in a table of measured figures reads as "nothing scheduled", which
    is why the footnote says what the column is -- the same treatment `rvr` and `bx`
    get for numbers a reader would otherwise assume came from somewhere.

    The countdown is asserted as text against a fixed `_NOW`, so this fails on a
    change to the derivation rather than on the date the suite happens to run.
    """
    rows = watchlist_data(watched, now=_NOW)
    out = render_watchlist(rows)

    nvda = next(line for line in out.splitlines() if line.strip().startswith("NVDA"))
    assert "2026-08-27 14d" in nvda, (
        f"the recorded date and the days to it belong in one cell: the date alone "
        f"makes the reader do the arithmetic\n{nvda}"
    )
    pltr = next(line for line in out.splitlines() if line.strip().startswith("PLTR"))
    header = out.splitlines()[0]
    assert pltr[header.index("earnings")] == "-", (
        f"a symbol with no recorded date must dash in that column\n{pltr}"
    )

    prose = _prose(out)
    assert "earnings is a date YOU recorded, not a fetched one" in prose
    assert "none recorded rather than none due" in prose, (
        "a blank earnings cell means nothing recorded, never nothing due, and the "
        "report is the only place that distinction can be stated"
    )


def test_a_past_earnings_date_says_so_rather_than_counting_backwards(watched):
    """A recorded date that has gone by, in the cell and in a footnote.

    Read a week AFTER the recorded date, so `earnings_in_days` is negative. The cell
    says "past" instead of printing -7d, which in a column of countdowns reads as one
    running backwards, and the footnote names the symbol -- the date stands until the
    reader replaces it, and only the surface showing it can say so.
    """
    later = datetime(2026, 9, 3, 12, tzinfo=MARKET_TZ)
    rows = watchlist_data(watched, now=later)
    assert next(r for r in rows if r["symbol"] == "NVDA")["earnings_in_days"] == -7

    out = render_watchlist(rows)
    nvda = next(line for line in out.splitlines() if line.strip().startswith("NVDA"))
    assert "2026-08-27 past" in nvda, f"a past date rendered as a countdown:\n{nvda}"
    assert "-7" not in nvda
    assert "recorded and now past: NVDA (2026-08-27)" in _prose(out)


def test_the_table_says_what_its_two_ranked_numbers_mean(watched):
    """The provenance sentences, generated from the constants rather than typed.

    A 0-to-100 column beside an options journal reads as IV rank to every reader who
    has one in another tool, and a BXTRENDER column at unstated periods is a figure
    nobody can reproduce. Both footnotes are interpolated from `vol` and `trend`, so
    a retune cannot leave the terminal claiming the old numbers -- the same rule the
    calendar's `impact_source` follows.
    """
    prose = _prose(render_watchlist(watchlist_data(watched, now=_NOW)))
    assert _prose(RANK_BANDS_SOURCE) in prose
    assert _prose(PARAMS_CAPTION) in prose
    assert f"{RANK_MIDPOINT:.0f} is" in prose
    assert "realised vol is what the stock DID" in prose, (
        "the attribution that stops a reader importing an IV rank's meaning onto "
        "this gauge"
    )


# ------------------------------------------------------------- column sizing
#
# `table` is the primitive under every report in this module, and its sizing
# rule was unguarded: nothing failed when the header stopped counting toward
# column width. That defect does not raise -- it produces a table whose header
# row runs past its separator and whose columns no longer line up under their
# labels, which is exactly the failure a reader is least likely to report as a
# bug and most likely to work around.


def test_a_header_wider_than_its_content_still_fits():
    """Width is max(header, cells), and the header is the half that was dropped.

    'commission' over a cell of '1.0' is the everyday case: the reports in this
    module label narrow numeric columns with long words. Sizing to the cells
    alone gives a 3-wide column holding a 10-character label.
    """
    out = table(["commission", "q"], [["1.0", "2"]])
    lines = out.splitlines()
    assert len({len(line) for line in lines}) == 1, (
        f"rows disagree on width:\n{out}"
    )
    # The separator is built from the same widths, so it is the honest witness.
    header, sep, row = lines
    assert len(header) == len(sep) == len(row)
    assert "commission" in header


def test_content_wider_than_its_header_widens_the_column():
    """The other direction, so the max() cannot be replaced by the header alone."""
    out = table(["q", "n"], [["1234567890", "2"]])
    lines = out.splitlines()
    assert len({len(line) for line in lines}) == 1, f"ragged:\n{out}"
    assert "1234567890" in lines[2]


def test_alignment_defaults_to_label_then_numbers():
    """First column left, the rest right -- the convention the docstring states."""
    out = table(["asset", "qty"], [["OPT", "5"]])
    label, _sep, row = out.splitlines()
    assert row.index("OPT") == label.index("asset"), "first column is not left-aligned"
    assert row.rstrip().endswith("5"), "a numeric column is not right-aligned"


def test_an_empty_table_says_none_rather_than_raising():
    """No rows means no cells to take a max() over, which would be a ValueError."""
    assert table(["a", "b"], []) == "  (none)"
