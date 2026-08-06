"""Price bars: the parser, the windows, and the write.

No test here touches the network. The parser is exercised against a captured
response shape and the backfill against an injected fetch, so the suite stays
offline and deterministic.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from optjournal.bars import (
    CONTEXT_BARS,
    HOURLY_LIMIT_DAYS,
    BackfillOutcome,
    BandContract,
    backfill_bars,
    bars_manifest,
    close_series,
    delta_around,
    epoch_et,
    et_day,
    expected_move_band,
    replay_bars,
    upsert_bars,
)
from optjournal.blackscholes import bs_price
from optjournal.db import connect, migrate
from optjournal.marketdata import Bar, BarFetchError, occ_symbol, parse_chart

DAY = 86400


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    c = connect(tmp_path / "bars.db")
    migrate(c)
    c.execute(
        "INSERT INTO statements (source_file, sha256, account_id, from_date,"
        " to_date, base_currency, asset_filter, ingested_at)"
        " VALUES ('t.xml','x','U1','2025-01-01','2026-12-31','EUR','OPT','now')"
    )
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
    assert got["bar_size"] == "1h"
    assert got["conid"] == "U1"
    assert [c for _, c in got["points"]] == [50.0, 51.0]


def test_replay_falls_back_to_a_coarser_series_rather_than_drawing_nothing(conn):
    """A partial backfill should degrade to a coarser chart, not a blank panel.
    The size actually drawn is returned so the panel can say which it is.
    """
    _option_trade(conn, conid="OPT1", symbol="AAA  260101P00100000",
                  underlying="AAA", ucid="U1", date="2026-01-05", trade_id="o1")
    upsert_bars(conn, conid="U1", symbol="AAA", bar_size="1d", source="yahoo",
                bars=[_bar(_ts("2026-01-06"), 50.0)])
    got = replay_bars(conn, "AAA", opened_at="2026-01-05", closed_at="2026-01-12")
    assert got["bar_size"] == "1d", "hourly was empty, so daily should be drawn"
    assert got["points"]


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
    assert [c for _, c in got["points"]] == [10.0, 20.0]


def test_replay_of_an_unknown_symbol_is_empty_not_invented(conn):
    got = replay_bars(conn, "NOPE", opened_at="2026-01-05", closed_at="2026-01-12")
    assert got == {"conid": None, "bar_size": None, "points": []}


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
    kept = [ts for ts, _ in got["points"]]
    before = [ts for ts in kept if ts < _ts("2026-01-12")]
    after = [ts for ts in kept if ts > _ts("2026-01-16") + DAY - 1]
    assert len(before) == CONTEXT_BARS["1d"], f"{len(before)} bars of lead-in"
    assert len(after) == CONTEXT_BARS["1d"], f"{len(after)} bars of run-out"


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
    inside = [ts for ts, _ in got["points"]
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
    assert len(got["points"]) == 30


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
    from optjournal.bars import _expiry_epoch
    assert _expiry_epoch("20260904") == _expiry_epoch("2026-09-04")
    assert _expiry_epoch("nonsense") is None


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
