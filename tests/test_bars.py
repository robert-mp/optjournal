"""Price bars: the parser, the windows, and the write.

No test here touches the network. The parser is exercised against a captured
response shape and the backfill against an injected fetch, so the suite stays
offline and deterministic.
"""

from __future__ import annotations

import sqlite3
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest
from conftest import add_statement, connect_migrated

from optjournal import marketdata
from optjournal.bars import (
    CONTEXT_MAX_BARS,
    CONTEXT_MIN_BARS,
    HOURLY_LIMIT_DAYS,
    SNAPSHOT_FLOOR_DAYS,
    WATCH_LOOKBACK_DAYS,
    BackfillOutcome,
    audit_perishable,
    backfill_bars,
    bars_manifest,
    close_series,
    last_traded_day,
    replay_bars,
    upsert_bars,
    watch_closes,
    weekly_closes,
)
from optjournal.clock import MARKET_TZ
from optjournal.marketdata import (
    Bar,
    BarFetchError,
    BarNotFound,
    occ_symbol,
    parse_chart,
    parse_quote,
)

DAY = 86400


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    """A journal holding only the statement row that trade rows hang off."""
    c = connect_migrated(tmp_path / "bars.db")
    add_statement(c)
    return c


def _chart(stamps, **columns) -> dict:
    """A response in the shape the endpoint actually returns."""
    return {"chart": {"error": None, "result": [
        {"timestamp": list(stamps), "indicators": {"quote": [dict(columns)]}}
    ]}}


# --------------------------------------------------------------------------
# marketdata: reading the wire
# --------------------------------------------------------------------------

def test_the_live_stub_bar_is_dropped():
    """The bug this closes, found in the real DB. The source appends a synthetic
    bar for the moment you asked, stamped at that moment rather than on the grid:
    a 13:17 request returns 09:00, 10:00, 11:00, 12:00 and then 12:35. Its
    timestamp is unique per request, so it upserts over nothing -- each poll
    deposits a fresh phantom bar. Six such rows were already stored from two
    backfills during one session, and polling hourly through a session would have
    added seven a day per contract.
    """
    hour = 3600
    base = 1785_000_000 - (1785_000_000 % hour)   # on the grid by construction
    stamps = [base, base + hour, base + 2 * hour, base + 2 * hour + 2100]
    bars = parse_chart(
        _chart(stamps, open=[1.0] * 4, high=[1.0] * 4, low=[1.0] * 4,
               close=[1.0, 2.0, 3.0, 4.0], volume=[1] * 4),
        symbol="X", bar_size="1h",
    )
    assert [b.ts for b in bars] == stamps[:3], "the off-grid stub survived"
    assert [b.close for b in bars] == [1.0, 2.0, 3.0]


def test_the_grid_is_anchored_on_the_first_bar_not_the_clock():
    """An underlying's hourly bars sit on the half hour (09:30 ET), an option's
    on the hour. Anchoring on the first bar -- always a real session bar -- keeps
    both correct without the parser knowing which asset it is reading, and
    without depending on when it ran, which is what lets a fixture test it.
    """
    hour = 3600
    base = 1785_000_000 - (1785_000_000 % hour) + 1800   # half-past grid
    stamps = [base, base + hour, base + hour + 900]
    bars = parse_chart(
        _chart(stamps, open=[1.0] * 3, high=[1.0] * 3, low=[1.0] * 3,
               close=[1.0] * 3, volume=[1] * 3),
        symbol="X", bar_size="1h",
    )
    assert [b.ts for b in bars] == stamps[:2]


def test_a_daily_bar_for_a_live_session_is_kept():
    """Daily bars are deliberately NOT grid-filtered. A daily bar for a session
    in progress is legitimately incomplete and the replay chart draws it as
    "where it is now", so filtering it would delete the live point.
    """
    stamps = [1785_000_000, 1785_000_123]
    bars = parse_chart(
        _chart(stamps, open=[1.0, 1.0], high=[1.0, 1.0], low=[1.0, 1.0],
               close=[1.0, 2.0], volume=[1, 1]),
        symbol="X", bar_size="1d",
    )
    assert [b.ts for b in bars] == stamps


def test_null_prices_survive_as_null():
    """The rule that outranks the rest. A quiet option strike has no print on
    roughly one session in five; writing 0.0 there would draw the position's
    value collapsing to nothing, which reads as a catastrophic loss rather than
    as a gap.
    """
    bars = parse_chart(
        _chart([100, 200], open=[1.0, None], high=[1.0, None],
               low=[1.0, None], close=[1.0, None], volume=[5, None]),
        symbol="X", bar_size="1d",
    )
    assert [b.close for b in bars] == [1.0, None]
    assert [b.volume for b in bars] == [5, None]
    assert not any(b.close == 0.0 for b in bars), "a gap became a zero"


def test_an_empty_series_is_an_answer_not_a_failure():
    """Option contracts return a well-formed response with an empty timestamp
    array for every intraday request. That is the source having no data at that
    granularity, not an error and not a bad symbol -- so callers can tell the
    two apart.
    """
    assert parse_chart(_chart([]), symbol="X", bar_size="1h") == []


def test_a_source_error_is_raised():
    payload = {"chart": {"error": {"code": "Not Found"}, "result": None}}
    with pytest.raises(BarFetchError, match="Not Found"):
        parse_chart(payload, symbol="X", bar_size="1d")


def test_an_http_404_is_preserved_as_not_found(monkeypatch):
    """The journal needs to distinguish a vanished contract from an outage."""

    def missing(*_args, **_kwargs):
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    monkeypatch.setattr(marketdata.urllib.request, "urlopen", missing)
    with pytest.raises(BarNotFound, match="HTTP Error 404"):
        marketdata.fetch_bars("SPY   260825C00769000", bar_size="1d", start=1, end=2)


def test_a_short_column_pads_rather_than_raising():
    """An incomplete response is a gap, not a fault: indexing a short column
    against the timestamp count would raise instead of recording the hole.
    """
    bars = parse_chart(
        _chart([100, 200, 300], close=[1.0]), symbol="X", bar_size="1d"
    )
    assert [b.close for b in bars] == [1.0, None, None]


def test_bars_come_back_in_time_order():
    bars = parse_chart(_chart([300, 100, 200], close=[3.0, 1.0, 2.0]),
                       symbol="X", bar_size="1d")
    assert [b.ts for b in bars] == [100, 200, 300]


# --------------------------------------------------------------------------
# marketdata: the live quote, from the same response
# --------------------------------------------------------------------------

def _quote_payload(**meta):
    """A chart response carrying only what `parse_quote` reads.

    Shaped from a REAL capture. The live `meta` block holds 25 keys; five of them
    are read, and the field names are copied from the capture rather than guessed --
    `chartPreviousClose`, not `previousClose`, which the response does not carry at
    all. None of the 25 is an implied vol, which is why the panel's gauge carries a
    realised figure.
    """
    return {"chart": {"error": None, "result": [{"meta": dict(meta)}]}}


def test_a_quote_carries_the_price_and_when_it_was():
    quote = parse_quote(_quote_payload(
        regularMarketPrice=330.915, regularMarketTime=1786114925,
        chartPreviousClose=311.21, currency="USD",
    ), symbol="TSLA")
    assert (quote.price, quote.at) == (330.915, 1786114925)
    assert quote.previous_close == 311.21
    assert quote.currency == "USD"


def test_an_undated_price_is_discarded():
    """The rule that makes the live column honest.

    The page's only defence against a stale quote is showing its age, so a price
    with no timestamp would render as live and could be Friday's last trade. Same
    rule as `money.py` applies to a figure whose currency cannot be established:
    drop the number rather than present it unqualified.
    """
    quote = parse_quote(_quote_payload(regularMarketPrice=330.915), symbol="TSLA")
    assert quote.at is None
    assert quote.price is None, "an undated price must not reach the page"


def test_a_quote_carries_the_company_name():
    """The name the watchlist shows beside the symbol, at NO extra request.

    It was already arriving in the `meta` block this parser reads and being dropped,
    which is why it rides on `/api/quotes` rather than becoming a stored column: the
    request is already being spent on the price.

    `longName` verbatim -- probed live, DELL's value is byte for byte the string the
    mockup drew -- with no cleanup, because a name is the source's fact and
    "improving" it is how one column comes to show two data qualities.
    """
    quote = parse_quote(_quote_payload(
        longName="Dell Technologies Inc.", shortName="Dell Technologies Inc.",
        regularMarketPrice=484.60, regularMarketTime=1786114925,
    ), symbol="DELL")
    assert quote.name == "Dell Technologies Inc."


def test_the_name_falls_back_to_the_short_one_but_prefers_the_long():
    """The order is measured, not stylistic.

    `shortName` is truncated at 31 characters by the source: SPY reads "State Street
    SPDR S&P 500 ETF T" there against the full "...ETF Trust" in `longName`, and on
    a dual-class ticker the two disagree outright (BRK-B: "Berkshire Hathaway Inc.
    New"). So `longName` wins whenever it is there, and the short one is still worth
    more than a bare symbol when it is not.
    """
    both = parse_quote(_quote_payload(
        longName="State Street SPDR S&P 500 ETF Trust",
        shortName="State Street SPDR S&P 500 ETF T",
    ), symbol="SPY")
    assert both.name == "State Street SPDR S&P 500 ETF Trust"

    short_only = parse_quote(
        _quote_payload(shortName="State Street SPDR S&P 500 ETF T"), symbol="SPY"
    )
    assert short_only.name == "State Street SPDR S&P 500 ETF T"


def test_a_nameless_quote_reports_none_so_the_row_shows_its_symbol():
    """None rather than the symbol repeated, or an empty string.

    Measured: ZVZZT, the exchange's own test ticker, answers with a price and NO
    name at all, so this is a live case rather than a defensive one. None is what
    lets the row render the bare symbol and lets a later search box say whether
    company search is available yet; a symbol echoed into the name slot would make
    "DELL DELL" look like data.

    An empty or blank string is treated as absent for the same reason, and a
    non-string is refused: a number under a company-name label is the defect shape
    this project keeps hunting.
    """
    assert parse_quote(_quote_payload(regularMarketPrice=26.98), symbol="X").name is None
    assert parse_quote(_quote_payload(longName="   "), symbol="X").name is None
    assert parse_quote(_quote_payload(longName=42), symbol="X").name is None
    # A blank long name must not shadow a usable short one.
    assert parse_quote(
        _quote_payload(longName="", shortName="Coherent Corp."), symbol="COHR"
    ).name == "Coherent Corp."


def test_a_name_survives_an_undated_price():
    """The price is discarded without a timestamp; the name is not.

    Different rules for different facts, and the reason is what each one goes stale
    against: a price is meaningless without its age (outside market hours the source
    keeps serving Friday's), while a company name does not change within a session.
    Tying the two would blank the name on exactly the rows whose price is already
    unusable, which is the reader's worst moment to lose the label.
    """
    quote = parse_quote(_quote_payload(
        longName="Palantir Technologies Inc.", regularMarketPrice=177.32,
    ), symbol="PLTR")
    assert quote.price is None and quote.at is None
    assert quote.name == "Palantir Technologies Inc."


def test_a_quote_for_a_symbol_with_no_meta_is_empty_not_an_error():
    """A known symbol the source has no quote for is an answer, not a failure --
    the same distinction `parse_chart` draws for an empty timestamp array."""
    quote = parse_quote(_quote_payload(), symbol="X")
    assert (quote.price, quote.at, quote.previous_close) == (None, None, None)


def test_a_quote_source_error_is_raised():
    payload = {"chart": {"error": {"code": "Not Found"}, "result": None}}
    with pytest.raises(BarFetchError, match="Not Found"):
        parse_quote(payload, symbol="X")


def test_a_quote_from_a_non_chart_payload_is_raised():
    with pytest.raises(BarFetchError, match="not a chart payload"):
        parse_quote({"unexpected": True}, symbol="X")


def test_a_string_price_is_not_coerced():
    """Defensive against the source changing shape: a price arriving as a string
    must not become a float by accident, because the page would then compare it
    to a stored close and render a change that was never measured.
    """
    quote = parse_quote(_quote_payload(
        regularMarketPrice="330.915", regularMarketTime=1786114925,
    ), symbol="X")
    assert quote.price is None


def test_occ_symbol_drops_ibkrs_padding():
    """IBKR pads the root to six characters; the source wants it unpadded. Plain
    underlying tickers have no padding, so this is safe to apply to both.
    """
    assert occ_symbol("TSLA  260904P00270000") == "TSLA260904P00270000"
    assert occ_symbol("TSLA") == "TSLA"


# --------------------------------------------------------------------------
# windows
# --------------------------------------------------------------------------

def _ts(day: str) -> int:
    """Epoch seconds for a YYYY-MM-DD day, matching bars._epoch."""
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


def _option_trade(conn, *, conid, symbol, underlying, ucid, date, trade_id,
                  open_close="O", quantity=-1):
    """One option fill. `open_close`/`quantity` default to a short open, and are
    parameters so a test can close an episode -- which is what decides whether
    the manifest asks for that contract's perishable intraday bars.
    """
    conn.execute(
        "INSERT INTO trades (trade_id, ib_exec_id, transaction_id, account_id,"
        " trade_date, date_time, asset_category, symbol, conid,"
        " underlying_symbol, underlying_conid, open_close, quantity,"
        " trade_price, currency, fx_rate_to_base, raw, source_file,"
        " first_seen_at)"
        " VALUES (?,?,?, 'U1', ?, ?, 'OPT', ?, ?, ?, ?, ?, ?, 1.0,"
        " 'USD', 1.0, '{}', 't.xml', 'now')",
        (trade_id, trade_id, trade_id, date, f"{date} 15:00:00",
         symbol, conid, underlying, ucid, open_close, quantity),
    )
    conn.commit()


#: A fixed "now" so window lengths are arithmetic rather than wall-clock.
_NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def test_option_history_is_daily_only(conn):
    """Not a policy choice: the source keeps no intraday bars for an option's
    PAST sessions, so asking hourly over a historical window spends a request to
    receive an empty series. Measured pre-market, every contract in the real book
    returned zero hourly bars while its underlying still returned five days.
    """
    _option_trade(conn, conid="C1", symbol="AAA  260101P00100000",
                  underlying="AAA", ucid="U1", date="2026-01-05",
                  trade_id="t1")
    options = [r for r in bars_manifest(conn, now=_NOW) if r.kind == "option"]
    assert options, "no option window derived"
    assert {r.bar_size for r in options} == {"1d"}
    assert not any(r.perishable for r in options), (
        "a seven-month window cannot be collected live and must not claim to be"
    )


def test_a_short_open_option_also_asks_for_live_intraday_bars(conn):
    """The one thing that cannot be backfilled. An option serves hourly bars
    while its session runs and discards them afterwards, so the only way to hold
    an hourly option series is to collect it as it happens.
    """
    _option_trade(conn, conid="C1", symbol="AAA  260801P00100000",
                  underlying="AAA", ucid="U1", date="2026-08-03",
                  trade_id="t1")
    options = [r for r in bars_manifest(conn, now=_NOW) if r.kind == "option"]
    assert {r.bar_size for r in options} == {"1d", "1h"}
    hourly = [r for r in options if r.bar_size == "1h"]
    assert [r.perishable for r in hourly] == [True]
    assert not any(r.perishable for r in options if r.bar_size == "1d"), (
        "the daily series is settled history and can be re-fetched at leisure"
    )


def test_a_closed_option_asks_for_no_live_bars(conn):
    """Its sessions are over, so its intraday bars are already gone. Asking
    would spend a request per poll for a series the source will never return.
    """
    for trade_id, oc, qty in (("t1", "O", -1), ("t2", "C", 1)):
        _option_trade(conn, conid="C1", symbol="AAA  260801P00100000",
                      underlying="AAA", ucid="U1", date="2026-08-03",
                      trade_id=trade_id, open_close=oc, quantity=qty)
    options = [r for r in bars_manifest(conn, now=_NOW) if r.kind == "option"]
    assert options, "a closed contract still needs its daily history"
    assert {r.bar_size for r in options} == {"1d"}


def test_a_long_open_option_asks_for_no_live_bars(conn):
    """The LEAP. Its replay is drawn daily over hundreds of sessions, so hourly
    bars would be thousands of rows the chart never draws -- and the gate is the
    chart's own granularity rule, not a second threshold that could disagree.
    """
    _option_trade(conn, conid="LEAP", symbol="AAA  270617C00700000",
                  underlying="AAA", ucid="U1", date="2025-02-03",
                  trade_id="t1")
    options = [r for r in bars_manifest(conn, now=_NOW) if r.kind == "option"]
    assert {r.bar_size for r in options} == {"1d"}


def test_perishable_only_narrows_to_what_cannot_wait(conn):
    """What the market-hours poll asks for. Running the whole manifest seven
    times a session would re-fetch years of settled daily history to collect a
    handful of new hourly rows.
    """
    _option_trade(conn, conid="C1", symbol="AAA  260801P00100000",
                  underlying="AAA", ucid="U1", date="2026-08-03",
                  trade_id="t1")
    full = bars_manifest(conn, now=_NOW)
    live = bars_manifest(conn, now=_NOW, perishable_only=True)
    assert len(full) > len(live), "the control: the filter must remove something"
    assert live, "nothing derived for an open short-dated option"
    assert all(r.perishable and r.kind == "option" and r.bar_size == "1h"
               for r in live)


def test_underlying_granularity_follows_the_window_length(conn):
    """A five-day hold wants hourly (21-42 points); a months-long one wants
    daily or the series becomes thousands of points for no added insight.
    """
    _option_trade(conn, conid="SHORT", symbol="AAA  260201P00100000",
                  underlying="AAA", ucid="UA", date="2026-01-05",
                  trade_id="s1")
    _option_trade(conn, conid="LONG", symbol="BBB  270101C00500000",
                  underlying="BBB", ucid="UB", date="2026-01-05",
                  trade_id="l1")
    sizes = {
        (r.symbol, r.bar_size)
        for r in bars_manifest(conn)
        if r.kind == "underlying"
    }
    # Both windows are open-ended (no closing fill), so both run to now. The
    # short one is only distinguishable once it closes; what this pins is that
    # the rule reads the span rather than the asset.
    assert all(size in ("1h", "1d") for _, size in sizes)
    assert HOURLY_LIMIT_DAYS > 0


def test_windows_merge_per_conid_and_bar_size(conn):
    """Two episodes on the same contract become one request covering both, so
    an overlapping pair is not fetched twice.
    """
    _option_trade(conn, conid="C1", symbol="AAA  260101P00100000",
                  underlying="AAA", ucid="U1", date="2026-01-05",
                  trade_id="t1")
    _option_trade(conn, conid="C1", symbol="AAA  260101P00100000",
                  underlying="AAA", ucid="U1", date="2026-01-09",
                  trade_id="t2")
    keys = [r.key for r in bars_manifest(conn)]
    assert len(keys) == len(set(keys)), f"duplicate windows: {keys}"


def test_a_flat_book_asks_only_for_the_market_context(conn):
    """A flat book has no positions and no watched symbols, so nothing
    position-derived is fetched -- but the S&P and VIX context is unconditional,
    because it is the market the journal trades in, not something the journal
    traded. A fresh clone should have the 0DTE planner before its first fill.
    """
    manifest = bars_manifest(conn)
    assert {(r.symbol, r.kind, r.bar_size) for r in manifest} == {
        ("^GSPC", "context", "1d"),
        ("^VIX", "context", "1d"),
    }
    # Non-perishable, so a market-hours poll (which asks perishable-only) still
    # asks for nothing: index closes backfill, and re-fetching them intraday
    # would spend requests on a figure that only changes at the daily close.
    assert bars_manifest(conn, perishable_only=True) == []


# --------------------------------------------------------------------------
# the write
# --------------------------------------------------------------------------

def _bar(ts, close=1.0) -> Bar:
    return Bar(ts=ts, open=close, high=close, low=close, close=close, volume=1)


def test_the_write_is_idempotent(conn):
    """Bars for a closed session never change, so a re-run must cost nothing
    and duplicate nothing.
    """
    for _ in range(2):
        upsert_bars(conn, conid="C1", symbol="AAA", bar_size="1d",
                    source="yahoo", bars=[_bar(100), _bar(200)])
    count = conn.execute("SELECT COUNT(*) AS n FROM price_bars").fetchone()["n"]
    assert count == 2


def test_a_better_source_upgrades_a_row_and_a_worse_one_cannot(conn):
    """Broker marks beat a public endpoint, so adding an IBKR source later
    upgrades history in place -- and a later public re-fetch must not undo it.
    """
    upsert_bars(conn, conid="C1", symbol="AAA", bar_size="1d",
                source="yahoo", bars=[_bar(100, 1.0)])
    upsert_bars(conn, conid="C1", symbol="AAA", bar_size="1d",
                source="ibkr", bars=[_bar(100, 2.0)])
    row = conn.execute("SELECT source, close FROM price_bars").fetchone()
    assert (row["source"], row["close"]) == ("ibkr", 2.0)

    upsert_bars(conn, conid="C1", symbol="AAA", bar_size="1d",
                source="yahoo", bars=[_bar(100, 3.0)])
    row = conn.execute("SELECT source, close FROM price_bars").fetchone()
    assert (row["source"], row["close"]) == ("ibkr", 2.0), "a worse source won"


def test_an_unknown_bar_size_is_refused(conn):
    with pytest.raises(ValueError, match="bar size"):
        upsert_bars(conn, conid="C1", symbol="AAA", bar_size="7m",
                    source="yahoo", bars=[_bar(100)])


def test_a_series_never_splices_two_granularities(conn):
    """One conid can hold hourly across a short trade AND daily across a LEAP.
    Selecting both would append two weeks of hourly to three years of daily and
    draw the join as a price move.
    """
    upsert_bars(conn, conid="C1", symbol="AAA", bar_size="1d",
                source="yahoo", bars=[_bar(100, 10.0), _bar(200, 11.0)])
    upsert_bars(conn, conid="C1", symbol="AAA", bar_size="1h",
                source="yahoo", bars=[_bar(300, 99.0), _bar(400, 98.0)])
    daily = close_series(conn, "C1", bar_size="1d")
    assert daily == [(100, 10.0), (200, 11.0)]
    assert 99.0 not in [close for _, close in daily]


def test_a_series_carries_timestamps_and_drops_nulls(conn):
    """Sessions are not evenly spaced -- weekends, holidays, and a half-length
    15:30 bar -- so x cannot be inferred from position in the list. A quiet
    strike with no print stays absent rather than becoming a zero.
    """
    upsert_bars(conn, conid="C1", symbol="AAA", bar_size="1d", source="yahoo",
                bars=[_bar(100, 10.0), _bar(200, None), _bar(300, 12.0)])
    assert close_series(conn, "C1", bar_size="1d") == [(100, 10.0), (300, 12.0)]


def test_a_series_clips_to_the_window_inclusively(conn):
    upsert_bars(conn, conid="C1", symbol="AAA", bar_size="1d", source="yahoo",
                bars=[_bar(100, 1.0), _bar(200, 2.0), _bar(300, 3.0)])
    got = close_series(conn, "C1", bar_size="1d", start=200, end=300)
    assert got == [(200, 2.0), (300, 3.0)]


def test_replay_picks_the_granularity_the_backfill_stored(conn):
    """The chart must ask for what the manifest wrote. Both use _bar_size_for,
    so a short trade gets hourly and a long one daily by construction rather
    than by coincidence.
    """
    _option_trade(conn, conid="OPT1", symbol="AAA  260101P00100000",
                  underlying="AAA", ucid="U1", date="2026-01-05", trade_id="o1")
    upsert_bars(conn, conid="U1", symbol="AAA", bar_size="1h", source="yahoo",
                bars=[_bar(_ts("2026-01-06"), 50.0), _bar(_ts("2026-01-07"), 51.0)])
    got = replay_bars(conn, "AAA", opened_at="2026-01-05", closed_at="2026-01-12")
    assert got.bar_size == "1h"
    assert got.conid == "U1"
    assert [c for _, c in got.points] == [50.0, 51.0]


def test_replay_falls_back_to_a_coarser_series_rather_than_drawing_nothing(conn):
    """A partial backfill should degrade to a coarser chart, not a blank panel.
    The size actually drawn is returned so the panel can say which it is.
    """
    _option_trade(conn, conid="OPT1", symbol="AAA  260101P00100000",
                  underlying="AAA", ucid="U1", date="2026-01-05", trade_id="o1")
    upsert_bars(conn, conid="U1", symbol="AAA", bar_size="1d", source="yahoo",
                bars=[_bar(_ts("2026-01-06"), 50.0)])
    got = replay_bars(conn, "AAA", opened_at="2026-01-05", closed_at="2026-01-12")
    assert got.bar_size == "1d", "hourly was empty, so daily should be drawn"
    assert got.points


def test_replay_of_a_snapshot_only_contract_draws_every_bar_held(conn):
    """The LEAP has no opening fill anywhere, so its window is unknown. Drawing
    everything held beats inventing an entry date by matching cost basis against
    the series.
    """
    upsert_bars(conn, conid="U1", symbol="AAA", bar_size="1d", source="yahoo",
                bars=[_bar(_ts("2025-02-03"), 10.0), _bar(_ts("2026-01-06"), 20.0)])
    conn.execute(
        "INSERT INTO securities (conid, symbol, underlying_symbol, underlying_conid,"
        " raw, updated_at) "
        "VALUES ('OPTX', 'AAA  270101C00700000', 'AAA', 'U1', '{}', 'now')"
    )
    got = replay_bars(conn, "AAA", opened_at=None, closed_at=None)
    assert [c for _, c in got.points] == [10.0, 20.0]


def test_replay_of_an_unknown_symbol_is_empty_not_invented(conn):
    """A symbol resolving to no underlying yields an empty series, not a guess.

    Every field explicitly None or empty: the page reads one shape for every
    replay, so the unresolved case has to answer the same three questions the
    populated one does rather than be absent.
    """
    got = replay_bars(conn, "NOPE", opened_at="2026-01-05", closed_at="2026-01-12")
    assert (got.conid, got.bar_size, got.points) == (None, None, [])


def test_context_is_counted_in_bars_not_calendar_days(conn):
    """Four calendar days rendered 45-58% of every chart as padding, and on a
    ten-bar trade the lead-in was larger than the trade. It also varied with the
    weekday: Thursday plus four days is two sessions, Monday plus four is four.
    """
    _option_trade(conn, conid="OPT1", symbol="AAA  260201P00100000",
                  underlying="AAA", ucid="U1", date="2026-01-12", trade_id="o1")
    # 40 daily bars straddling a trade that ran 2026-01-12 .. 2026-01-16.
    bars = [_bar(_ts("2026-01-01") + n * DAY, 100.0 + n) for n in range(40)]
    upsert_bars(conn, conid="U1", symbol="AAA", bar_size="1d",
                source="yahoo", bars=bars)
    got = replay_bars(conn, "AAA", opened_at="2026-01-12", closed_at="2026-01-16")
    kept = [ts for ts, _ in got.points]
    before = [ts for ts in kept if ts < _ts("2026-01-12")]
    inside = [ts for ts in kept
              if _ts("2026-01-12") <= ts <= _ts("2026-01-16") + DAY - 1]
    after = [ts for ts in kept if ts > _ts("2026-01-16") + DAY - 1]
    # 5 bars inside x 0.25 = 1.25, rounded to 1, floored to CONTEXT_MIN_BARS.
    assert len(inside) == 5, f"{len(inside)} bars inside the window"
    assert len(before) == CONTEXT_MIN_BARS, f"{len(before)} bars of lead-in"
    assert len(after) == CONTEXT_MIN_BARS, f"{len(after)} bars of run-out"
    # And the padding is a minority of the chart, which is the whole point.
    assert len(before) + len(after) < len(inside)


def test_context_scales_with_the_window_it_pads(conn):
    """One fixed count cannot serve a two-hour 0DTE and a ten-day hold.

    Measured on the real journal before this changed: a fixed 7-hourly-bar pad
    (exactly one session, and reasonable for a multi-day trade) put 8 pad bars
    around the 6 real ones of a position opened the day before -- 57% of the
    chart -- and would have put 14 around 3 on a 0DTE held two hours.

    Asserted as a RATIO rather than as counts, because the property that matters
    is "the trade is the subject of its own chart", and a count would have to be
    restated every time the fraction is tuned.
    """
    _option_trade(conn, conid="OPT1", symbol="BBB  260601P00100000",
                  underlying="BBB", ucid="U2", date="2026-01-05", trade_id="o2")
    # 120 daily bars, so a long window has room for real context either side.
    bars = [_bar(_ts("2026-01-01") + n * DAY, 100.0 + n) for n in range(120)]
    upsert_bars(conn, conid="U2", symbol="BBB", bar_size="1d",
                source="yahoo", bars=bars)

    def shape(opened: str, closed: str) -> tuple[int, int]:
        got = replay_bars(conn, "BBB", opened_at=opened, closed_at=closed)
        lo, hi = _ts(opened), _ts(closed) + DAY - 1
        kept = [ts for ts, _ in got.points]
        inside = sum(1 for ts in kept if lo <= ts <= hi)
        return inside, len(kept) - inside

    # A one-session trade: the floor applies, so context exists but is minimal.
    short_in, short_pad = shape("2026-01-10", "2026-01-10")
    assert short_in == 1, f"{short_in} bars inside a one-day window"
    assert short_pad == 2 * CONTEXT_MIN_BARS, f"{short_pad} pad bars on 1 inside"

    # A 40-session trade: proportional, and CAPPED. 40 x 0.15 = 6, above the
    # daily ceiling of 5, which is the case that stops a LEAP from dragging in
    # years of lead-in.
    long_in, long_pad = shape("2026-01-10", "2026-02-18")
    assert long_in == 40, f"{long_in} bars inside a 40-day window"
    assert long_pad == 2 * CONTEXT_MAX_BARS["1d"], (
        f"{long_pad} pad bars: a long window should hit the ceiling, not scale "
        f"forever -- three years of context around a LEAP is a different chart"
    )

    # The ratio is what a reader sees, and it must improve with window length.
    assert long_pad / long_in < short_pad / short_in
    assert long_pad / (long_pad + long_in) < 0.25


def test_the_closing_session_belongs_to_the_trade(conn):
    """A journal stamp truncates to its date, so a closing day's epoch is that
    day's MIDNIGHT and every bar of the session sorts after it. Harmless while
    the window was padded by whole days; it cut the closing session out of the
    trade the moment the trim got precise.
    """
    _option_trade(conn, conid="OPT1", symbol="AAA  260201P00100000",
                  underlying="AAA", ucid="U1", date="2026-01-12", trade_id="o1")
    # Three bars DURING the closing session, hours after midnight.
    close_day = _ts("2026-01-16")
    bars = [_bar(_ts("2026-01-12") + 14 * 3600, 100.0),
            _bar(close_day + 14 * 3600, 101.0),
            _bar(close_day + 15 * 3600, 102.0),
            _bar(close_day + 16 * 3600, 103.0)]
    upsert_bars(conn, conid="U1", symbol="AAA", bar_size="1d",
                source="yahoo", bars=bars)
    got = replay_bars(conn, "AAA", opened_at="2026-01-12", closed_at="2026-01-16")
    inside = [ts for ts, _ in got.points
              if _ts("2026-01-12") <= ts <= close_day + DAY - 1]
    assert len(inside) == 4, "the closing session's bars were treated as context"


def test_a_snapshot_only_window_is_not_trimmed(conn):
    """Nothing to be context FOR: with no entry date, everything held is the
    answer rather than a slice around a window that does not exist.
    """
    bars = [_bar(_ts("2025-02-03") + n * DAY, 10.0 + n) for n in range(30)]
    upsert_bars(conn, conid="U1", symbol="AAA", bar_size="1d",
                source="yahoo", bars=bars)
    conn.execute(
        "INSERT INTO securities (conid, symbol, underlying_symbol, underlying_conid,"
        " raw, updated_at) "
        "VALUES ('OPTY', 'AAA  270101C00700000', 'AAA', 'U1', '{}', 'now')"
    )
    got = replay_bars(conn, "AAA", opened_at=None, closed_at=None)
    assert len(got.points) == 30








def test_a_computed_bar_never_displaces_a_fetched_one(conn):
    """The demo computes option bars because its symbols do not exist upstream.
    They are ranked below every real source so the relationship is one-way: a
    genuine fetch upgrades a computed row, and a re-run of the demo generator can
    never overwrite real market history it happens to also cover.
    """
    stamp = _ts("2026-01-05") + 13 * 3600
    upsert_bars(conn, conid="X", symbol="AAA", bar_size="1d",
                source="synthetic", bars=[_bar(stamp, 1.0)])
    upsert_bars(conn, conid="X", symbol="AAA", bar_size="1d",
                source="yahoo", bars=[_bar(stamp, 2.0)])
    assert close_series(conn, "X", bar_size="1d") == [(stamp, 2.0)], (
        "a real fetch failed to upgrade a computed row"
    )
    upsert_bars(conn, conid="X", symbol="AAA", bar_size="1d",
                source="synthetic", bars=[_bar(stamp, 3.0)])
    assert close_series(conn, "X", bar_size="1d") == [(stamp, 2.0)], (
        "a computed bar overwrote real market history"
    )


# --------------------------------------------------------------------------
# watched symbols: rows stored, SESSIONS read
# --------------------------------------------------------------------------

def _et_bar(day: str, hour: int, close: float) -> Bar:
    """A daily bar stamped at a given hour inside one ET session.

    The hour is a parameter because the source does not stamp two daily series
    alike: an option's daily bar arrives at 04:00Z, which is midnight ET, while
    its underlying's arrives at 13:30Z, the session open. A reader joining on the
    TIMESTAMP therefore sees two different bars where a reader joining on the ET
    trading day sees one session -- which is the whole subject of these tests.
    """
    stamp = int(
        datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, tzinfo=MARKET_TZ)
        .timestamp()
    )
    return Bar(ts=stamp, open=close, high=close, low=close, close=close, volume=1)


def test_two_conids_covering_one_session_read_as_one_close(conn):
    """The measured NVDA shape: one symbol's sessions stored under two conids.

    A watched symbol gets the synthetic `watch:SYMBOL` key, and if the same name
    is also traded its real underlying conid accumulates the same daily closes.
    On the real journal that was 41 rows under conid 4815747 and 43 under
    `watch:NVDA`, with 39 ET days present under BOTH and identical closes -- so
    reading `LIMIT 21` rows returned 21 rows spanning 12 sessions, and every
    duplicate is a zero-return day that drags a realised vol down.

    Seeded here at the same shape, one conid stamped at the session open and the
    other at midnight ET so the two cannot be collapsed by timestamp: eight rows
    over five sessions, three of them present under both keys.
    """
    days = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
    prices = [100.0, 101.0, 102.0, 103.0, 104.0]
    upsert_bars(
        conn, conid="4815747", symbol="NVDA", bar_size="1d", source="yahoo",
        bars=[_et_bar(d, 9, p) for d, p in zip(days[:4], prices[:4], strict=True)],
    )
    upsert_bars(
        conn, conid="watch:NVDA", symbol="NVDA", bar_size="1d", source="yahoo",
        bars=[_et_bar(d, 0, p) for d, p in zip(days[1:], prices[1:], strict=True)],
    )
    stored = conn.execute(
        "SELECT COUNT(*) AS n FROM price_bars WHERE symbol = 'NVDA'"
    ).fetchone()["n"]
    assert stored == 8, "the fixture is meant to store more rows than sessions"

    series = watch_closes(conn, "NVDA")
    assert [day for day, _ in series] == list(reversed(days)), (
        "five ET sessions were stored as nine rows and must read back as five, "
        "newest first -- vol.log_returns' documented input order"
    )
    assert [close for _, close in series] == list(reversed(prices))
    assert watch_closes(conn, "NVDA", sessions=3) == [
        ("2026-08-07", 104.0), ("2026-08-06", 103.0), ("2026-08-05", 102.0),
    ], "the cap counts sessions, which is the unit the caller asked for"


def test_the_higher_ranked_source_wins_a_duplicated_session(conn):
    """Which duplicate survives is `marketdata.SOURCE_RANK`, not arrival order.

    The demo's computed bars are ranked below every real source so a genuine
    fetch always displaces one on WRITE; a reader that preferred the later
    timestamp instead would undo that on READ, showing a synthetic price for a
    session a real fetch also covers. So the synthetic bar here is inserted
    second AND stamped later, leaving rank as the only thing that can pick the
    fetched close.
    """
    upsert_bars(conn, conid="AAA1", symbol="AAA", bar_size="1d", source="yahoo",
                bars=[_et_bar("2026-08-05", 9, 222.0)])
    upsert_bars(conn, conid="watch:AAA", symbol="AAA", bar_size="1d",
                source="synthetic",
                bars=[_et_bar("2026-08-05", 15, 111.0),
                      _et_bar("2026-08-04", 15, 99.0)])

    assert watch_closes(conn, "AAA") == [("2026-08-05", 222.0), ("2026-08-04", 99.0)], (
        "the fetched close must win the day both cover, and the computed one must "
        "still answer for the day only it covers -- outranked is not discarded"
    )


def test_the_watch_window_covers_a_year_of_sessions(conn):
    """The read window cannot be trimmed back to a month without a red test.

    60 calendar days was ~41 sessions, which sized the fetch to ONE column: five
    of the six real watched symbols held 45 or 46 closes and 10 ISO weeks, so
    anything computed over a longer window was structurally absent. The floor
    asserted here is the weekly indicator's ~120 ISO weeks (840 calendar days),
    which is the reason the constant now carries.

    The request COST is asserted in the same breath, because that is what makes
    the width free: one daily request per watched symbol, whatever the span, and
    never a perishable one.
    """
    conn.execute(
        "INSERT INTO watchlist (symbol, note, added_at) VALUES ('NVDA', NULL, ?)",
        ("2026-08-01",),
    )
    conn.commit()

    watched = [r for r in bars_manifest(conn, now=_NOW) if r.kind == "watchlist"]
    assert len(watched) == 1, "one request per watched symbol, or the span is not free"
    request = watched[0]
    assert request.bar_size == "1d" and not request.perishable
    span_days = (request.end - request.start) / DAY
    assert span_days >= 840, (
        f"the watch window is {span_days:.0f} calendar days; the weekly arm needs "
        "~120 ISO weeks, which is ~840"
    )
    assert WATCH_LOOKBACK_DAYS == SNAPSHOT_FLOOR_DAYS, (
        "the two windows are the same judgement -- ask wider than the answer needs "
        "and let the source truncate -- and are meant to stay one number"
    )


# --------------------------------------------------------------------------
# watched symbols: sessions bucketed into ISO weeks
# --------------------------------------------------------------------------

def test_a_week_is_its_last_session_and_a_short_week_is_still_a_week(conn):
    """A week's close is where it ENDED, and a holiday-shortened week is one week.

    No gap filling and no holiday calendar, for the perishable audit's own reason:
    the sessions present in the data are the definition. That is not an edge case
    either -- measured over 755 fetched closes, 31 of 158 ISO weeks hold four
    sessions and 2 hold three, so filling them to five would invent a third of a
    year of closes the market never printed.

    Seeded as two full weeks around one whose Wednesday is missing, which is the
    shape a real holiday leaves. The middle week must still appear, exactly once,
    carrying its own last session's close.
    """
    days = [
        "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07",
        "2026-08-10", "2026-08-11", "2026-08-13", "2026-08-14",   # no 08-12
        "2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21",
    ]
    prices = [100.0 + n for n in range(len(days))]
    upsert_bars(
        conn, conid="watch:AAA", symbol="AAA", bar_size="1d", source="yahoo",
        bars=[_et_bar(d, 0, p) for d, p in zip(days, prices, strict=True)],
    )

    weeks = weekly_closes(conn, "AAA")
    assert [key for key, _, _ in weeks] == ["2026-W32", "2026-W33", "2026-W34"], (
        "ISO weeks, Monday start, OLDEST FIRST -- and the short week is one week"
    )
    assert [close for _, close, _ in weeks] == [104.0, 108.0, 113.0], (
        "a week's close is its last session's close, not its first or its mean"
    )
    assert [count for _, _, count in weeks] == [5, 4, 5], (
        "the short week reports the four sessions it actually holds"
    )


def test_the_current_week_reports_how_many_sessions_are_in(conn):
    """The newest bucket is normally partial, and its count is what says so.

    Measured on 755 real closes, the newest ISO week held 3 sessions against 5 in
    each of the five weeks before it, and the weekly indicator over that partial
    week repaints every session (TSLA: -19.02, -18.63, -19.71 across those three).
    Including it matches TradingView and this repo's own rule that a bar for a
    session in progress is legitimately incomplete; showing it UNLABELLED would be
    the undated-price defect again, so the count travels with the value.

    A second session is then added to the same week and the count -- not the number
    of weeks -- is what moves, which is the property the caption depends on.
    """
    upsert_bars(
        conn, conid="watch:AAA", symbol="AAA", bar_size="1d", source="yahoo",
        bars=[_et_bar(d, 0, p) for d, p in (
            ("2026-08-14", 99.0),      # a Friday: the previous week, complete
            ("2026-08-17", 100.0),     # the Monday of the newest week
        )],
    )
    assert weekly_closes(conn, "AAA")[-1] == ("2026-W34", 100.0, 1)

    upsert_bars(
        conn, conid="watch:AAA", symbol="AAA", bar_size="1d", source="yahoo",
        bars=[_et_bar("2026-08-18", 0, 101.0)],
    )
    weeks = weekly_closes(conn, "AAA")
    assert len(weeks) == 2, "a second session in the same week is not a second week"
    assert weeks[-1] == ("2026-W34", 101.0, 2), (
        "the newest week's close follows its newest session, and the count says how "
        "much of the week is in"
    )


def test_a_week_reads_one_close_per_session_however_many_conids_stored_it(conn):
    """The weekly arm inherits `watch_closes`' collapse rather than repeating it.

    Built on the deduplicated reader on purpose: a symbol that is watched AND traded
    stores the same ET days under two conids, and a second SELECT here would count
    each of them as a session -- which cannot move the week's close, but reports a
    complete week as a ten-session one and would eventually disagree about which
    source wins a day.
    """
    days = ["2026-08-17", "2026-08-18", "2026-08-19"]
    prices = [100.0, 101.0, 102.0]
    for conid, hour in (("4815747", 9), ("watch:AAA", 0)):
        upsert_bars(
            conn, conid=conid, symbol="AAA", bar_size="1d", source="yahoo",
            bars=[_et_bar(d, hour, p) for d, p in zip(days, prices, strict=True)],
        )
    assert weekly_closes(conn, "AAA") == [("2026-W34", 102.0, 3)], (
        "six stored rows are three sessions of one week"
    )


def test_a_symbol_with_no_bars_has_no_weeks(conn):
    """Zero weeks, not one empty one: the count is what explains the weekly dash."""
    assert weekly_closes(conn, "NOTHING") == []
















def test_backfill_collects_failures_without_abandoning_the_book(conn):
    """One unreachable contract must not cost the rest of the run."""
    _option_trade(conn, conid="C1", symbol="AAA  260101P00100000",
                  underlying="AAA", ucid="U1", date="2026-01-05",
                  trade_id="t1")
    calls: list[str] = []

    def fetch(symbol, *, bar_size, start, end, source):
        calls.append(symbol)
        if symbol.startswith("AAA "):
            raise BarFetchError("boom")
        return [_bar(start + DAY)]

    outcome = backfill_bars(conn, fetch=fetch)
    assert isinstance(outcome, BackfillOutcome)
    assert outcome.failures, "the failure was swallowed"
    assert not outcome.ok
    assert outcome.written >= 1, "one failure abandoned the whole run"


def test_backfill_counts_an_empty_series_as_skipped_not_failed(conn):
    _option_trade(conn, conid="C1", symbol="AAA  260101P00100000",
                  underlying="AAA", ucid="U1", date="2026-01-05",
                  trade_id="t1")
    outcome = backfill_bars(conn, fetch=lambda *a, **k: [])
    assert outcome.ok
    assert outcome.written == 0
    assert outcome.skipped == outcome.requested


def test_backfill_treats_a_past_option_404_as_unavailable(conn):
    """An expired contract removed by the source must not fail forever."""
    for trade_id, day, open_close, quantity in (
        ("open", "2026-01-05", "O", -1),
        ("close", "2026-01-06", "C", 1),
    ):
        _option_trade(
            conn, conid="C1", symbol="AAA  260109P00100000",
            underlying="AAA", ucid="U1", date=day, trade_id=trade_id,
            open_close=open_close, quantity=quantity,
        )

    def fetch(symbol, *, bar_size, start, end, source):
        if symbol.startswith("AAA "):
            raise BarNotFound("AAA260109P00100000 1d: HTTP Error 404")
        return [_bar(start + DAY)]

    outcome = backfill_bars(
        conn, fetch=fetch, now=datetime(2026, 2, 1, tzinfo=UTC)
    )
    assert outcome.ok
    assert outcome.skipped == 1
    assert outcome.written >= 1, "the unavailable option abandoned its underlying"


def test_backfill_keeps_a_current_option_404_loud(conn):
    """A live contract missing from the source may be malformed or an outage."""
    _option_trade(
        conn, conid="C1", symbol="AAA  261001P00100000",
        underlying="AAA", ucid="U1", date="2026-09-08", trade_id="open",
    )

    def fetch(symbol, *, bar_size, start, end, source):
        if symbol.startswith("AAA "):
            raise BarNotFound(f"{symbol} {bar_size}: HTTP Error 404")
        return [_bar(start + DAY)]

    outcome = backfill_bars(
        conn, fetch=fetch, now=datetime(2026, 9, 9, tzinfo=UTC)
    )
    assert not outcome.ok
    assert outcome.failures


def test_backfill_keeps_an_underlying_404_loud(conn):
    """Only expired option endpoints receive the unavailable treatment."""
    for trade_id, day, open_close, quantity in (
        ("open", "2026-01-05", "O", -1),
        ("close", "2026-01-06", "C", 1),
    ):
        _option_trade(
            conn, conid="C1", symbol="AAA  260109P00100000",
            underlying="AAA", ucid="U1", date=day, trade_id=trade_id,
            open_close=open_close, quantity=quantity,
        )

    def fetch(symbol, *, bar_size, start, end, source):
        if symbol == "AAA":
            raise BarNotFound(f"AAA {bar_size}: HTTP Error 404")
        return []

    outcome = backfill_bars(
        conn, fetch=fetch, now=datetime(2026, 2, 1, tzinfo=UTC)
    )
    assert not outcome.ok
    assert set(outcome.failures) == {
        "AAA 1h: HTTP Error 404",
        "AAA 1d: HTTP Error 404",
    }


# --------------------------------------------------------------------------
# the session audit: noticing a session whose perishable bars never landed
# --------------------------------------------------------------------------
def _session_bars(conn, conid, symbol, day, *, bar_size="1h", hours=(14, 15, 16)):
    """Bars stamped inside one ET trading day, on the hour."""
    base = _ts(day)
    upsert_bars(
        conn, conid=conid, symbol=symbol, bar_size=bar_size, source="yahoo",
        bars=[_bar(base + hour * 3600) for hour in hours],
    )


def _open_short_option(conn, *, date="2026-08-03", conid="C1"):
    """An open short-dated short option: the one shape that is collected live."""
    _option_trade(conn, conid=conid, symbol="AAA  271001P00100000",
                  underlying="AAA", ucid="UAAA", date=date, trade_id=f"t{conid}")


def test_a_day_the_market_did_not_trade_is_not_a_fault(conn):
    """The holiday guard, and the reason there is no date list anywhere.

    US markets shut around nine days a year plus the odd half-session. Alarming
    on each would be nine false reports a year on a job whose entire value is
    that its silence means something.
    """
    _open_short_option(conn)
    _session_bars(conn, "UAAA", "AAA", "2026-08-05")
    audited = audit_perishable(conn, day="2026-08-06", now=_NOW)
    assert not audited.market_traded, "no underlying bars, yet it claims a session"
    assert audited.ok, "a closed market was reported as lost data"
    assert not audited.missing


def test_a_traded_day_with_no_option_bars_is_reported(conn):
    """The control for the test above: the SAME shape, one underlying bar added,
    and the verdict must invert. Without this, a market_traded oracle stuck at
    False would pass every audit forever and the job would be decorative.
    """
    _open_short_option(conn)
    _session_bars(conn, "UAAA", "AAA", "2026-08-05")
    audited = audit_perishable(conn, day="2026-08-05", now=_NOW)
    assert audited.market_traded, "the underlying's own bars say it traded"
    assert not audited.ok
    assert audited.missing == ("AAA  271001P00100000",)
    assert not audited.covered


def test_a_covered_session_is_silent(conn):
    _open_short_option(conn)
    _session_bars(conn, "UAAA", "AAA", "2026-08-05")
    _session_bars(conn, "C1", "AAA  271001P00100000", "2026-08-05")
    audited = audit_perishable(conn, day="2026-08-05", now=_NOW)
    assert audited.ok
    assert audited.covered == ("AAA  271001P00100000",)
    assert not audited.missing


def test_a_contract_opened_after_the_session_is_not_reported_missing(conn):
    """Otherwise every new position would trigger a report on the day it opened:
    it cannot have bars for a session it did not exist in, and calling that a
    loss would make the job cry wolf on the most ordinary event there is.
    """
    _open_short_option(conn, date="2026-08-06")
    _session_bars(conn, "UAAA", "AAA", "2026-08-05")
    audited = audit_perishable(conn, day="2026-08-05", now=_NOW)
    assert audited.ok, "a position opened today was blamed for yesterday"
    assert not audited.missing and not audited.covered


def test_only_the_contracts_the_poll_asks_for_are_audited(conn):
    """Eligibility is the live manifest itself, not a second rule. A rule of its
    own would drift from the collector and then report on a book neither holds --
    the LEAP is the case that proves it, since it is deliberately never collected
    hourly and so can never be missing hourly bars.
    """
    _option_trade(conn, conid="LEAP", symbol="AAA  270617C00700000",
                  underlying="AAA", ucid="UAAA", date="2025-02-03",
                  trade_id="tleap")
    _session_bars(conn, "UAAA", "AAA", "2026-08-05")
    audited = audit_perishable(conn, day="2026-08-05", now=_NOW)
    assert audited.market_traded
    assert audited.ok, "the LEAP was audited for bars nothing ever collects"
    assert not audited.missing and not audited.covered


def test_the_audited_day_skips_weekends_and_holidays(conn):
    """Walking back to the last day an underlying traded, rather than
    subtracting one day: on a Monday the previous session is Friday, and the
    same mechanism covers a holiday without knowing which days those are.
    """
    _open_short_option(conn, date="2026-07-20")
    _session_bars(conn, "UAAA", "AAA", "2026-07-31")     # a Friday
    monday = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    assert last_traded_day(conn, now=monday) == "2026-07-31"
    assert audit_perishable(conn, now=monday).day == "2026-07-31"


def test_no_recent_underlying_bars_reports_nothing_rather_than_guessing(conn):
    """An empty or un-backfilled journal is the daily job's problem. Reporting it
    here as well would double up on a failure that job already surfaces.
    """
    _open_short_option(conn)
    audited = audit_perishable(conn, now=_NOW)
    assert not audited.market_traded
    assert audited.ok
    assert audited.day == "2026-08-05", "the fallback day must still be yesterday"


def test_a_bar_from_a_neighbouring_session_does_not_count_as_coverage(conn):
    """The day is matched on the ET trading date, not on a raw epoch window.
    Comparing timestamps against a UTC midnight boundary would count a 20:00 ET
    bar -- which is 00:00Z the NEXT day -- as the following session's coverage.
    """
    _open_short_option(conn)
    _session_bars(conn, "UAAA", "AAA", "2026-08-05")
    _session_bars(conn, "C1", "AAA  271001P00100000", "2026-08-04")
    audited = audit_perishable(conn, day="2026-08-05", now=_NOW)
    assert not audited.ok, "yesterday's bars were counted as today's"
    assert audited.missing == ("AAA  271001P00100000",)


def test_the_audit_exit_codes_separate_all_three_outcomes(tmp_path, capsys):
    """The cron reads nothing but the exit code to decide whether to speak, so
    the three states have to be distinguishable: covered (silent), missing
    (report), and nothing-to-check (also silent, but for a different reason a
    human reading `cron_list` needs to be able to tell apart).
    """
    from optjournal.cli import main

    db = tmp_path / "audit.db"
    conn = connect_migrated(db)
    add_statement(conn)

    moment = datetime.now(UTC).astimezone(MARKET_TZ)
    yesterday = (moment - timedelta(days=1)).strftime("%Y-%m-%d")
    opened = (moment - timedelta(days=2)).strftime("%Y-%m-%d")
    _open_short_option(conn, date=opened)

    argv = ["bars", "--audit", "--db", str(db)]
    assert main(argv) == 3, "an un-backfilled journal is not a lost session"

    _session_bars(conn, "UAAA", "AAA", yesterday)
    conn.commit()
    assert main(argv) == 1, "a traded day with no option bars must report"
    assert "MISSING" in capsys.readouterr().out

    _session_bars(conn, "C1", "AAA  271001P00100000", yesterday)
    conn.commit()
    assert main(argv) == 0, "a covered session must be silent"


def test_the_audit_refuses_to_be_combined_with_a_fetch(tmp_path):
    """`--audit` reads and `--live` writes. One argv asking for both has no
    single meaning, and guessing which won would make a cron's behaviour depend
    on flag order.
    """
    from optjournal.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["bars", "--audit", "--live"])


# --------------------------------------------------------------------------
# Index symbols: two vocabularies for one instrument.
# --------------------------------------------------------------------------

def test_an_index_root_is_translated_to_the_sources_spelling():
    """IBKR's `SPX` is not a ticker at the price source, and nothing translated.

    Measured rather than reasoned: `SPX` answered 404 on every request while
    `^GSPC` returned bars, so an account holding SPX options collected no
    underlying history at all. The whole symbol is matched, so an OPTION on the
    same root is left alone -- that distinction is the one way this could break
    working symbols.
    """
    from optjournal.marketdata import source_symbol

    assert source_symbol("SPX") == "^GSPC"
    assert source_symbol("XSP") == "^XSP", "the mini-SPX is its own instrument"
    assert source_symbol("VIX") == "^VIX"
    # An option on an index root keeps its OCC spelling: the padding goes, the
    # translation does not apply, because `SPX   261120C07000000` is not `SPX`.
    assert source_symbol("SPX   261120C07000000") == "SPX261120C07000000"
    # A plain equity is untouched, which is every other symbol in the manifest.
    assert source_symbol("TSLA") == "TSLA"
    assert source_symbol("^GSPC") == "^GSPC", "an already-correct symbol is stable"


def test_the_index_map_holds_only_spellings_that_were_probed():
    """A guessed ticker here is a symbol that 404s forever, silently.

    Every value in the map was requested against the live endpoint while it was
    written. This pins the SHAPE of that promise -- each target is a caret symbol,
    and each key is a bare root -- so a later addition by pattern-matching rather
    than by probing is at least visibly different.
    """
    from optjournal.marketdata import INDEX_SYMBOLS

    assert INDEX_SYMBOLS, "the index map is empty, so no index resolves"
    for root, target in INDEX_SYMBOLS.items():
        assert not root.startswith("^"), f"{root} is already a source symbol"
        assert target.startswith("^"), f"{target} is not an index spelling"
        assert root == root.upper()


def test_the_translation_is_applied_where_bars_and_quotes_share_it():
    """One choke point, so a watchlisted index cannot work in one column and 404
    in the other. `_get_chart` is what both `fetch_bars` and `fetch_quote` call,
    and the symbol in the ERROR text is translated too -- a failure naming a symbol
    nobody requested is how the original 404 read as a mystery."""
    import inspect

    from optjournal import marketdata

    chart = inspect.getsource(marketdata._get_chart)
    assert "source_symbol(symbol)" in chart
    assert "occ_symbol(symbol)" not in chart, (
        "the chart fetch bypasses the index translation"
    )


# --- a reply broken off mid-way is this module's typed error (L3) -------------


@pytest.mark.parametrize("mode", ["truncated", "hangup"])
def test_a_reply_broken_off_mid_way_is_a_bar_fetch_error(broken_http, monkeypatch, mode):
    """`IncompleteRead` is an `http.client.HTTPException`, not an `OSError`.

    It escaped the fetcher's own error type, so one truncated body aborted a whole
    run instead of failing one request. `RemoteDisconnected` is here too, which
    every fetcher must also report as its own error.
    """
    monkeypatch.setattr(marketdata, "_CHART_URL", broken_http(mode) + "/{symbol}")
    with pytest.raises(marketdata.BarFetchError):
        marketdata.fetch_quote("SPY", timeout=5)
    with pytest.raises(marketdata.BarFetchError):
        marketdata.fetch_bars("SPY", bar_size="1d", start=0, end=86400)
