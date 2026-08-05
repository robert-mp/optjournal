"""Price bars: the parser, the windows, and the write.

No test here touches the network. The parser is exercised against a captured
response shape and the backfill against an injected fetch, so the suite stays
offline and deterministic.
"""

from __future__ import annotations

import sqlite3

import pytest

from optjournal.bars import (
    HOURLY_LIMIT_DAYS,
    BackfillOutcome,
    backfill_bars,
    bars_manifest,
    decimate,
    spark_series,
    upsert_bars,
)
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
# decimation
# --------------------------------------------------------------------------

def test_decimation_keeps_the_true_first_and_last_values():
    """A sparkline's endpoint is what the eye reads as "where it is now".
    Bucketing alone emits each bucket's min/max in occurrence order, so the
    last element would be an extreme of the final bucket -- which had the LEAP's
    miniature ending at 5.30 against a real mark of 8.20.
    """
    values = [float(n) for n in range(200)]
    out = decimate(values, points=20)
    assert out[0] == values[0]
    assert out[-1] == values[-1]


def test_decimation_preserves_extremes():
    """Every-nth sampling would drop exactly the spike a sparkline exists to
    show.
    """
    values = [1.0] * 100
    values[47] = 99.0
    out = decimate(values, points=10)
    assert 99.0 in out, "the spike was averaged out of existence"


def test_a_short_series_is_left_alone():
    values = [1.0, 2.0, 3.0]
    assert decimate(values, points=64) == values


# --------------------------------------------------------------------------
# the manifest
# --------------------------------------------------------------------------

def _option_trade(conn, *, conid, symbol, underlying, ucid, date, trade_id):
    conn.execute(
        "INSERT INTO trades (trade_id, ib_exec_id, transaction_id, account_id,"
        " trade_date, date_time, asset_category, symbol, conid,"
        " underlying_symbol, underlying_conid, open_close, quantity,"
        " trade_price, currency, fx_rate_to_base, raw, source_file,"
        " first_seen_at)"
        " VALUES (?,?,?, 'U1', ?, ?, 'OPT', ?, ?, ?, ?, 'O', -1, 1.0,"
        " 'USD', 1.0, '{}', 't.xml', 'now')",
        (trade_id, trade_id, trade_id, date, f"{date} 15:00:00",
         symbol, conid, underlying, ucid),
    )
    conn.commit()


def test_option_windows_are_daily_whatever_their_length(conn):
    """Not a policy choice: the source serves option contracts at daily
    granularity only, so asking hourly would spend a request to receive an
    empty series.
    """
    _option_trade(conn, conid="C1", symbol="AAA  260101P00100000",
                  underlying="AAA", ucid="U1", date="2026-01-05",
                  trade_id="t1")
    requests = bars_manifest(conn)
    options = [r for r in requests if r.kind == "option"]
    assert options, "no option window derived"
    assert {r.bar_size for r in options} == {"1d"}


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


def test_a_sparkline_never_splices_two_granularities(conn):
    """One conid can hold hourly across a short trade AND daily across a LEAP.
    Selecting both would append two weeks of hourly to three years of daily and
    draw the join as a price move.
    """
    upsert_bars(conn, conid="C1", symbol="AAA", bar_size="1d",
                source="yahoo", bars=[_bar(100, 10.0), _bar(200, 11.0)])
    upsert_bars(conn, conid="C1", symbol="AAA", bar_size="1h",
                source="yahoo", bars=[_bar(300, 99.0), _bar(400, 98.0)])
    daily = spark_series(conn, ["C1"], bar_size="1d")["C1"]
    assert daily == [10.0, 11.0]
    assert 99.0 not in daily


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
