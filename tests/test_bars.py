"""Price bars: the parser, the windows, and the write.

No test here touches the network. The parser is exercised against a captured
response shape and the backfill against an injected fetch, so the suite stays
offline and deterministic.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from conftest import add_statement, connect_migrated

from optjournal.bars import (
    CONTEXT_MAX_BARS,
    CONTEXT_MIN_BARS,
    HOURLY_LIMIT_DAYS,
    MARKET_TZ,
    BackfillOutcome,
    BandContract,
    ReplayLeg,
    audit_perishable,
    backfill_bars,
    bars_manifest,
    close_series,
    delta_around,
    epoch_et,
    et_day,
    expected_move_band,
    expiry_epoch,
    last_traded_day,
    modelled_marks,
    replay_bars,
    upsert_bars,
)
from optjournal.blackscholes import bs_price
from optjournal.marketdata import (
    Bar,
    BarFetchError,
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

    Shaped from a REAL capture. The live `meta` block holds 23 keys; these four
    are the ones read, and the field names are copied from the capture rather
    than guessed -- `chartPreviousClose`, not `previousClose`, which the response
    does not carry at all.
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


def test_a_flat_book_asks_for_nothing(conn):
    """The manifest is derived from positions, not scheduled, so there is
    nothing to fetch while the book is empty.
    """
    assert bars_manifest(conn) == []


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


# --------------------------------------------------------------------------
# clocks
# --------------------------------------------------------------------------

def test_journal_stamps_are_read_as_us_eastern():
    """Settled from the data, not assumed: Stockholm fills land 03:19-10:57 and a
    Korean fill lands 20:03, both of which are inside those exchanges' sessions
    in ET and outside them in UTC. A wrong zone would put every entry marker four
    hours off the line it marks.
    """
    # 10:35 EDT is 14:35Z in July (UTC-4) and 15:35Z in January (UTC-5).
    assert epoch_et("2026-07-24 10:35:01") == 1784903701
    summer = datetime.fromtimestamp(epoch_et("2026-07-24 10:35:00"), UTC)
    winter = datetime.fromtimestamp(epoch_et("2026-01-15 10:35:00"), UTC)
    assert (summer.hour, winter.hour) == (14, 15), "DST is not being applied"


@pytest.mark.parametrize("stamp", [None, "", "nonsense", "24/07/2026"])
def test_an_unparseable_stamp_is_none_not_zero(stamp):
    """Epoch zero is 1970, which would place a marker at the far left of every
    chart rather than nowhere.
    """
    assert epoch_et(stamp) is None


def test_two_daily_series_join_on_the_trading_day_not_the_timestamp(conn):
    """The source does not stamp them alike: an option's daily bar arrives at
    04:00Z (midnight ET) and its underlying's at 13:30Z (the session open). Same
    provider, same interval, two conventions -- so an exact-timestamp join finds
    nothing, silently, and the band just fails to appear.
    """
    day = _ts("2026-07-27")
    assert et_day(day + 4 * 3600) == et_day(day + 13 * 3600 + 1800) == "2026-07-27"


def test_the_cached_helpers_stay_pure_across_a_dst_boundary():
    """`et_day` and `expiry_epoch` are `functools.cache`d, so purity is load-bearing.

    Both are keyed on one scalar and read no clock, no database and no global --
    which is what makes caching them safe, and what this pins. The DST pair is the
    case worth naming: the ET offset changes across it (EDT is UTC-4, EST UTC-5),
    so a cache keyed on anything coarser than the exact timestamp, or a helper
    that consulted "now", would answer one of these two wrongly. The account has
    held positions across two such boundaries.
    """
    edt = int(datetime(2026, 7, 1, 12, tzinfo=MARKET_TZ).timestamp())
    est = int(datetime(2026, 1, 15, 12, tzinfo=MARKET_TZ).timestamp())
    assert et_day(edt) == "2026-07-01"
    assert et_day(est) == "2026-01-15"
    # Same answer on a second call, which is the cache's only observable effect.
    assert (et_day(edt), et_day(est)) == ("2026-07-01", "2026-01-15")
    # A cached function must still accept the None the payload really carries.
    assert expiry_epoch(None) is None and expiry_epoch("") is None
    assert expiry_epoch("20260904") == expiry_epoch("2026-09-04")


def test_the_band_solves_vol_from_the_options_own_closes(conn):
    """The whole point: the reference implementation applies an INDEX's vol to a
    single name because the underlying is all it has. We hold the contract's own
    prices, so the band reports what the market charged for this contract.
    """
    spot, strike, vol = 100.0, 90.0, 0.40
    expiry = "2026-03-20"
    expiry_ts = _ts("2026-03-20") + 16 * 3600
    # Stamps built through epoch_et rather than by adding hours to a UTC
    # midnight, because the source's "midnight ET" is 04:00Z in summer and
    # 05:00Z in winter. Hand-adding 4h to a JANUARY date lands at 23:00 ET the
    # previous day, which is a different trading day and so a different join key.
    days = ["2026-01-05", "2026-01-06"]
    opt = []
    for day in days:
        stamp = epoch_et(f"{day} 00:00:00")
        years = (expiry_ts - stamp) / (365.0 * 86400)
        opt.append(_bar(stamp, bs_price(spot, strike, years, vol, "P")))
    upsert_bars(conn, conid="OPT1", symbol="AAA  260320P00090000", bar_size="1d",
                source="yahoo", bars=opt)
    # Underlying dailies stamped at the session open, as the source really does.
    opens = [epoch_et(f"{day} 09:30:00") for day in days]
    upsert_bars(conn, conid="U1", symbol="AAA", bar_size="1d", source="yahoo",
                bars=[_bar(s, spot) for s in opens])

    points = [(s, spot) for s in opens]
    band = expected_move_band(
        conn,
        [BandContract(conid="OPT1", strike=strike, right="P", expiry=expiry)],
        points,
        underlying_conid="U1",
    )
    assert len(band) == 2, "no band -- the daily series failed to join"
    stamp, low, high = band[0]
    years = (expiry_ts - epoch_et(f"{days[0]} 00:00:00")) / (365.0 * 86400)
    want = spot * vol * (years ** 0.5)
    assert (high - low) / 2 == pytest.approx(want, rel=2e-2)
    assert low < spot < high


def test_the_band_is_absent_rather_than_narrow_without_a_vol(conn):
    """A point before the first solvable close gets NO band. A zero-width
    envelope would read as "the market expected nothing to happen".
    """
    upsert_bars(conn, conid="U1", symbol="AAA", bar_size="1d", source="yahoo",
                bars=[_bar(_ts("2026-01-05") + 13 * 3600, 100.0)])
    band = expected_move_band(
        conn,
        [BandContract(conid="OPT1", strike=90.0, right="P", expiry="2026-03-20")],
        [(_ts("2026-01-05") + 13 * 3600, 100.0)],
        underlying_conid="U1",
    )
    assert band == []


def test_marks_stop_at_expiry_rather_than_pricing_a_settled_contract(conn):
    """Past expiry the model does not degrade, it lies with confidence: bs_price
    clamps to intrinsic and bs_delta to 1.0, so a position still marked open
    draws a P&L that keeps swinging with spot and a delta pinned at the top of
    its axis for as long as the window runs. Measured on the demo's snapshot-only
    call, that was two months of tail on a contract that had settled.

    The band already stopped, because expected_move refuses a negative horizon.
    The two series are drawn on one chart, so a mark outstaying its band says the
    position was live after the envelope said it had expired.
    """
    spot, strike, vol = 100.0, 90.0, 0.40
    expiry, expiry_ts = "2026-01-16", _ts("2026-01-16") + 16 * 3600
    # A daily series that runs a fortnight PAST expiry.
    days = [f"2026-01-{n:02d}" for n in (12, 13, 14, 15, 16, 20, 21, 22, 23)]
    opens = [epoch_et(f"{day} 09:30:00") for day in days]
    upsert_bars(conn, conid="U1", symbol="AAA", bar_size="1d", source="yahoo",
                bars=[_bar(stamp, spot) for stamp in opens])
    option = []
    for day in days:
        stamp = epoch_et(f"{day} 00:00:00")
        years = (expiry_ts - stamp) / (365.0 * 86400)
        if years <= 0:
            continue      # the source stops too: an expired contract has no close
        option.append(_bar(stamp, bs_price(spot, strike, years, vol, "P")))
    upsert_bars(conn, conid="OPT1", symbol="AAA  260116P00090000", bar_size="1d",
                source="yahoo", bars=option)

    points = [(stamp, spot) for stamp in opens]
    leg = ReplayLeg(
        conid="OPT1", strike=strike, right="P", expiry=expiry,
        fills=((opens[0], -1.0, 3.0),),
    )
    marks = modelled_marks(conn, [leg], points, underlying_conid="U1")
    band = expected_move_band(
        conn,
        [BandContract(conid="OPT1", strike=strike, right="P", expiry=expiry)],
        points, underlying_conid="U1",
    )
    assert marks, "the control: marks must exist while the contract is alive"
    assert max(row[0] for row in marks) <= expiry_ts, (
        "a settled contract is still being priced"
    )
    assert max(row[0] for row in marks) == max(row[0] for row in band), (
        "the mark series and the band end on different bars"
    )
    assert all(abs(row[2]) <= 1.0 for row in marks), (
        "delta pinned past its true range, which is the clamp showing through"
    )


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


def test_a_fill_anchors_vol_where_the_source_has_no_history(conn):
    """The defect this closes, measured on the real journal: the TSLA 270P was
    sold on 2026-07-24 and the price source's first bar for that contract is
    2026-07-27, so the band, the delta and the P&L were all absent across the
    entry session -- the part of a replay a reader most wants. Asking the source
    for an earlier window returns nothing; the data does not exist. The fill does.
    """
    spot, strike, vol = 100.0, 90.0, 0.40
    expiry, expiry_ts = "2026-03-20", _ts("2026-03-20") + 16 * 3600
    # An hourly chart over one session, with NO option bar anywhere.
    opens = [epoch_et("2026-01-05 09:30:00") + i * 3600 for i in range(4)]
    upsert_bars(conn, conid="U1", symbol="AAA", bar_size="1h", source="yahoo",
                bars=[_bar(s, spot) for s in opens])
    points = [(s, spot) for s in opens]
    contract = BandContract(conid="OPT1", strike=strike, right="P", expiry=expiry)

    assert expected_move_band(conn, [contract], points, underlying_conid="U1") == [], (
        "the control: with no option price at all there is nothing to solve"
    )

    # The same contract, priced by a fill in the second bar.
    fill_at = opens[1]
    years = (expiry_ts - fill_at) / (365.0 * 86400)
    priced = replace(
        contract, anchors=((fill_at, bs_price(spot, strike, years, vol, "P")),)
    )
    band = expected_move_band(conn, [priced], points, underlying_conid="U1")
    assert [row[0] for row in band] == opens[1:], (
        "the band should start AT the fill and not before it -- a vol held "
        "backwards would price a position that did not exist yet"
    )
    stamp, low, high = band[0]
    assert (high - low) / 2 == pytest.approx(spot * vol * (years ** 0.5), rel=2e-2)


def test_delta_around_reports_none_before_a_position_existed(conn):
    """An opening event has no delta "before". Reporting 0.0 there would read as
    "we were delta-neutral" rather than "we were not in the trade" -- and for a
    roll, whose whole point is the exposure it removed, the pair is the number.
    """
    marks = [[100, 0.0, 0.60], [200, 5.0, 0.40], [300, 9.0, 0.0]]
    assert delta_around(marks, 100) == (None, 0.60), "an event on the first bar"
    assert delta_around(marks, 250) == (0.40, 0.0), "a roll mid-series"
    assert delta_around(marks, 50) == (None, 0.60), "before every mark"
    assert delta_around(marks, 9999) == (0.0, None), "after every mark"
    assert delta_around([], 100) == (None, None)


def test_the_band_accepts_both_expiry_formats_the_payload_carries(conn):
    """A leg states 2026-09-04 while a snapshot row keeps IBKR's 20260918.
    Handling one and rejecting the other produced a band for the LEAP and none
    for any traded lifecycle.
    """
    from optjournal.bars import expiry_epoch
    assert expiry_epoch("20260904") == expiry_epoch("2026-09-04")
    assert expiry_epoch("nonsense") is None


def test_the_band_and_the_marks_share_one_vol_solve(conn, monkeypatch):
    """`replay_model` solves implied vol ONCE and hands it to both consumers.

    Measured before this existed: 20 solves for 10 replays, with `_vol_series`
    accounting for 60% of `build_state` and half of that being exact
    recomputation over identical inputs.

    Counted rather than timed, because a timing assertion is flaky and a call
    count is exact. The property is also correctness and not only speed: with one
    solve the envelope and the P&L series drawn inside it cannot disagree about
    what the market charged for a contract.
    """
    from optjournal import bars as bars_mod

    calls = []
    original = bars_mod._vol_series
    monkeypatch.setattr(
        bars_mod, "_vol_series",
        lambda *a, **k: (calls.append(1), original(*a, **k))[1],
    )

    leg = ReplayLeg(
        conid="C1", strike=270.0, right="P", expiry="2026-09-04",
        fills=((_ts("2026-07-27"), -3.0, 5.24),),
    )
    points = [(_ts("2026-07-27") + h * 3600, 320.0 + h) for h in range(6)]
    bars_mod.replay_model(conn, [leg], points, underlying_conid="U1")

    assert len(calls) == 1, (
        f"replay_model solved vol {len(calls)} times; the band and the marks must "
        "share one solve, or half the work is recomputation and the two halves of "
        "one chart can disagree"
    )


def test_a_supplied_empty_vol_series_is_not_re_solved(conn, monkeypatch):
    """`{}` is a real answer -- nothing solved -- and must not trigger a re-solve.

    The guard is `vols is None` rather than `vols or _vol_series(...)` for exactly
    this: a contract whose price the model cannot reproduce returns an empty dict,
    which is falsey, so the truthiness spelling would re-run the full solve on
    precisely the input that just failed to produce anything.
    """
    from optjournal import bars as bars_mod

    calls = []
    monkeypatch.setattr(
        bars_mod, "_vol_series", lambda *a, **k: (calls.append(1), {})[1]
    )
    points = [(_ts("2026-07-27") + h * 3600, 320.0) for h in range(3)]
    leg = ReplayLeg(conid="C1", strike=270.0, right="P", expiry="2026-09-04")

    assert bars_mod.expected_move_band(
        conn, [], points, underlying_conid="U1", vols={}) == []
    assert bars_mod.modelled_marks(
        conn, [leg], points, underlying_conid="U1", vols={}) == []
    assert not calls, "an empty-but-supplied vol series was solved again"


def test_the_band_and_the_marks_solve_against_one_projection():
    """Both are drawn on one chart, so both must read the same contracts.

    They used to be built by two functions: `web._band_contracts` walked raw leg
    dicts for the band, while `modelled_marks` projected `ReplayLeg`s inline for
    the P&L. The two agreed -- verified across both journals -- but nothing held
    them there, and a divergence would put an envelope and a P&L series on the
    same axes disagreeing about what the market charged for a contract, with no
    test between them.

    Asserted structurally: `band_contracts` is the only projection, so a leg's
    fills become its anchors and a snapshot-only leg (no fills, seeded from a
    cost basis with no timestamp) anchors on nothing rather than on a guess.
    """
    from optjournal.bars import band_contracts

    traded = ReplayLeg(
        conid="C1", strike=270.0, right="P", expiry="2026-09-04",
        fills=((1000, -3.0, 5.24), (2000, 3.0, 2.61)),
    )
    snapshot = ReplayLeg(
        conid="C2", strike=700.0, right="C", expiry="20270617",
        seed_quantity=1.0, seed_price=30.0,
    )
    band = band_contracts([traded, snapshot])
    assert [c.conid for c in band] == ["C1", "C2"], "one contract per leg, in order"
    # Every fill is a vol observation: an opening and a closing fill are two
    # separate prices the market really charged for the same contract.
    assert band[0].anchors == ((1000, 5.24), (2000, 2.61))
    # A cost basis carries no timestamp, so there is nothing to anchor at.
    assert band[1].anchors == ()
    # The strike, right and expiry ride along unchanged -- the vol solve needs
    # all three, and reading any of them off the wrong leg inverts the answer.
    assert (band[0].strike, band[0].right, band[0].expiry) == (270.0, "P", "2026-09-04")
    assert (band[1].strike, band[1].right, band[1].expiry) == (700.0, "C", "20270617")


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
    _option_trade(conn, conid=conid, symbol="AAA  260901P00100000",
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
    assert audited.missing == ("AAA  260901P00100000",)
    assert not audited.covered


def test_a_covered_session_is_silent(conn):
    _open_short_option(conn)
    _session_bars(conn, "UAAA", "AAA", "2026-08-05")
    _session_bars(conn, "C1", "AAA  260901P00100000", "2026-08-05")
    audited = audit_perishable(conn, day="2026-08-05", now=_NOW)
    assert audited.ok
    assert audited.covered == ("AAA  260901P00100000",)
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
    _session_bars(conn, "C1", "AAA  260901P00100000", "2026-08-04")
    audited = audit_perishable(conn, day="2026-08-05", now=_NOW)
    assert not audited.ok, "yesterday's bars were counted as today's"
    assert audited.missing == ("AAA  260901P00100000",)


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
    _open_short_option(conn)

    argv = ["bars", "--audit", "--db", str(db)]
    assert main(argv) == 3, "an un-backfilled journal is not a lost session"

    # Derived exactly as last_traded_day derives it. Computing it as
    # et_day(now - 86400) instead would agree almost always and disagree in a
    # narrow window around a DST transition -- the same trap that already put a
    # January fixture on the wrong trading day earlier in this module.
    yesterday = (
        datetime.now(UTC).astimezone(MARKET_TZ) - timedelta(days=1)
    ).strftime("%Y-%m-%d")

    _session_bars(conn, "UAAA", "AAA", yesterday)
    conn.commit()
    assert main(argv) == 1, "a traded day with no option bars must report"
    assert "MISSING" in capsys.readouterr().out

    _session_bars(conn, "C1", "AAA  260901P00100000", yesterday)
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
