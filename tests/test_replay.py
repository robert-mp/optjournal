"""The modelled layer: the vol solve, the band, the marks and the delta.

Beside `replay.py`, where these tests followed the code. Every figure here is
MODELLED rather than broker-stated -- the one place in this journal that is true
-- so these are the tests that hold the numbers nobody was charged.

A `conn` is still needed, and that is not a wart: a vol solve reads an option's
own daily closes and the underlying's, so the layer under test genuinely depends
on storage. What the tests do NOT need is a statement corpus or a payload, which
is why they state their own bars.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from conftest import add_statement, connect_migrated

from optjournal.bars import upsert_bars
from optjournal.blackscholes import bs_price
from optjournal.clock import epoch_et, expiry_epoch
from optjournal.marketdata import Bar
from optjournal.replay import (
    ReplayLeg,
    delta_around,
    expected_move_band,
    modelled_marks,
)


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    """A journal holding only the statement row that trade rows hang off."""
    c = connect_migrated(tmp_path / "replay.db")
    add_statement(c)
    return c


def _ts(day: str) -> int:
    """Epoch seconds for a YYYY-MM-DD day, matching bars._epoch."""
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


def _bar(ts, close=1.0) -> Bar:
    return Bar(ts=ts, open=close, high=close, low=close, close=close, volume=1)


def test_the_band_solves_vol_from_the_options_own_closes(conn):
    """The whole point: the reference implementation applies an INDEX's vol to a
    single name because the underlying is all it has. We hold the contract's own
    prices, so the band reports what the market charged for this contract.
    """
    spot, strike, vol = 100.0, 90.0, 0.40
    expiry = "2026-03-20"
    expiry_ts = epoch_et("2026-03-20 16:00:00")
    # Stamps built through epoch_et rather than by adding hours to a UTC
    # midnight, because the source's "midnight ET" is 04:00Z in summer and
    # 05:00Z in winter. Hand-adding 4h to a JANUARY date lands at 23:00 ET the
    # previous day, which is a different trading day and so a different join key.
    # Each is PRICED at 16:00, though: a daily bar's price is its close.
    days = ["2026-01-05", "2026-01-06"]
    opt = []
    for day in days:
        years = (expiry_ts - epoch_et(f"{day} 16:00:00")) / (365.0 * 86400)
        opt.append(_bar(epoch_et(f"{day} 00:00:00"),
                        bs_price(spot, strike, years, vol, "P")))
    upsert_bars(conn, conid="OPT1", symbol="AAA  260320P00090000", bar_size="1d",
                source="yahoo", bars=opt)
    # Underlying dailies stamped at the session open, as the source really does.
    opens = [epoch_et(f"{day} 09:30:00") for day in days]
    upsert_bars(conn, conid="U1", symbol="AAA", bar_size="1d", source="yahoo",
                bars=[_bar(s, spot) for s in opens])

    points = [(s, spot) for s in opens]
    band = expected_move_band(
        conn,
        [ReplayLeg(conid="OPT1", strike=strike, right="P", expiry=expiry)],
        points,
        underlying_conid="U1",
        bar_size="1d",
    )
    assert len(band) == 2, "no band -- the daily series failed to join"
    stamp, low, high = band[0]
    years = (expiry_ts - epoch_et(f"{days[0]} 16:00:00")) / (365.0 * 86400)
    want = spot * vol * (years ** 0.5)
    assert (high - low) / 2 == pytest.approx(want, abs=1e-4)
    assert low < spot < high


def test_the_band_is_absent_rather_than_narrow_without_a_vol(conn):
    """A point before the first solvable close gets NO band. A zero-width
    envelope would read as "the market expected nothing to happen".
    """
    upsert_bars(conn, conid="U1", symbol="AAA", bar_size="1d", source="yahoo",
                bars=[_bar(_ts("2026-01-05") + 13 * 3600, 100.0)])
    band = expected_move_band(
        conn,
        [ReplayLeg(conid="OPT1", strike=90.0, right="P", expiry="2026-03-20")],
        [(_ts("2026-01-05") + 13 * 3600, 100.0)],
        underlying_conid="U1",
        bar_size="1d",
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
    expiry, expiry_ts = "2026-01-16", epoch_et("2026-01-16 16:00:00")
    # A daily series that runs a fortnight PAST expiry.
    days = [f"2026-01-{n:02d}" for n in (12, 13, 14, 15, 16, 20, 21, 22, 23)]
    opens = [epoch_et(f"{day} 09:30:00") for day in days]
    upsert_bars(conn, conid="U1", symbol="AAA", bar_size="1d", source="yahoo",
                bars=[_bar(stamp, spot) for stamp in opens])
    option = []
    for day in days:
        years = (expiry_ts - epoch_et(f"{day} 16:00:00")) / (365.0 * 86400)
        if years <= 0:
            continue      # the source stops too: an expired contract has no close
        option.append(_bar(epoch_et(f"{day} 00:00:00"),
                           bs_price(spot, strike, years, vol, "P")))
    upsert_bars(conn, conid="OPT1", symbol="AAA  260116P00090000", bar_size="1d",
                source="yahoo", bars=option)

    points = [(stamp, spot) for stamp in opens]
    leg = ReplayLeg(
        conid="OPT1", strike=strike, right="P", expiry=expiry,
        fills=((opens[0], -1.0, 3.0),),
    )
    marks = modelled_marks(conn, [leg], points, underlying_conid="U1", bar_size="1d")
    band = expected_move_band(
        conn, [leg], points, underlying_conid="U1", bar_size="1d",
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


def test_a_fill_anchors_vol_where_the_source_has_no_history(conn):
    """The defect this closes, measured on the real journal: the TSLA 270P was
    sold on 2026-07-24 and the price source's first bar for that contract is
    2026-07-27, so the band, the delta and the P&L were all absent across the
    entry session -- the part of a replay a reader most wants. Asking the source
    for an earlier window returns nothing; the data does not exist. The fill does.
    """
    spot, strike, vol = 100.0, 90.0, 0.40
    expiry, expiry_ts = "2026-03-20", epoch_et("2026-03-20 16:00:00")
    # An hourly chart over one session, with NO option bar anywhere.
    opens = [epoch_et("2026-01-05 09:30:00") + i * 3600 for i in range(4)]
    upsert_bars(conn, conid="U1", symbol="AAA", bar_size="1h", source="yahoo",
                bars=[_bar(s, spot) for s in opens])
    points = [(s, spot) for s in opens]
    leg = ReplayLeg(conid="OPT1", strike=strike, right="P", expiry=expiry)

    assert expected_move_band(
        conn, [leg], points, underlying_conid="U1", bar_size="1h"
    ) == [], "the control: with no option price at all there is nothing to solve"

    # The same contract, sold halfway through the second bar: the fill is an
    # option price the market really charged.
    fill_at = opens[1] + 1800
    years = (expiry_ts - fill_at) / (365.0 * 86400)
    sold = replace(
        leg, fills=((fill_at, -1.0, bs_price(spot, strike, years, vol, "P")),)
    )
    band = expected_move_band(
        conn, [sold], points, underlying_conid="U1", bar_size="1h"
    )
    assert [row[0] for row in band] == opens[1:], (
        "the band should start at the bar the fill falls in and not before it: "
        "a vol held backwards would price a position that did not exist yet"
    )
    stamp, low, high = band[0]
    # Read at that bar's close, an hour after its stamp.
    closes = (expiry_ts - (opens[1] + 3600)) / (365.0 * 86400)
    assert (high - low) / 2 == pytest.approx(spot * vol * (closes ** 0.5), abs=1e-4)


def test_delta_around_reports_none_before_a_position_existed(conn):
    """An opening event has no delta "before". Reporting 0.0 there would read as
    "we were delta-neutral" rather than "we were not in the trade" -- and for a
    roll, whose whole point is the exposure it removed, the pair is the number.
    """
    bars = [epoch_et(f"2026-01-05 {hour}:30:00") for hour in (9, 10, 11)]
    marks = [[bars[0], 0.0, 0.60], [bars[1], 5.0, 0.40], [bars[2], 9.0, 0.0]]

    def around(at: str):
        return delta_around(marks, epoch_et(f"2026-01-05 {at}"), bar_size="1h")

    assert around("10:30:00") == (None, 0.60), "an event at the first bar's close"
    assert around("11:30:00") == (0.60, 0.40), "a roll mid-series"
    assert around("09:00:00") == (None, 0.60), "before every mark"
    assert around("13:00:00") == (0.0, None), "after every mark"
    assert delta_around([], bars[0], bar_size="1h") == (None, None)


def test_delta_around_reads_a_mark_at_its_bars_close(conn):
    """A bar's mark is read at its close, so a fill inside the bar is AFTER it.

    The 10:30 bar closes at 11:30, already holding a 10:35 fill. Compared by its
    stamp, that mark counted as "before" the fill it contains, and an opening
    card read the new position's delta on both sides of its arrow.
    """
    bars = [epoch_et(f"2026-01-05 {hour}:30:00") for hour in (9, 10, 11)]
    marks = [[bars[0], 0.0, None], [bars[1], 5.0, 0.32], [bars[2], 9.0, 0.30]]
    filled = epoch_et("2026-01-05 10:35:00")
    assert delta_around(marks, filled, bar_size="1h") == (None, 0.32)


def test_delta_around_carries_an_absent_delta_rather_than_flattening_it(conn):
    """A None in the marks must reach the card as None.

    `modelled_marks` now reports delta as None on a bar holding nothing, and a
    closing event's "after" is exactly such a bar. Flattening it to 0.0 here would
    put the fabrication back one layer down: the card would read "0.32 -> 0.00",
    which on a symmetric axis claims the position ended delta-neutral rather than
    ended.
    """
    bars = [epoch_et(f"2026-01-05 {hour}:30:00") for hour in (9, 10, 11)]
    marks = [[bars[0], 0.0, None], [bars[1], 5.0, 0.32], [bars[2], 9.0, None]]
    closing, opening = epoch_et("2026-01-05 12:00:00"), epoch_et("2026-01-05 11:00:00")
    assert delta_around(marks, closing, bar_size="1h") == (0.32, None), "a CLOSING event"
    assert delta_around(marks, opening, bar_size="1h") == (None, 0.32), "an OPENING event"


def test_delta_is_absent_off_position_while_pnl_keeps_reporting(conn):
    """Delta is None before the entry and after the close; P&L is not.

    The bug: `modelled_marks` used one flag for two questions -- "can this bar be
    priced" and "is anything held here". They diverge exactly when quantity hits
    zero, so `delta += 0 * bs_delta(...)` wrote an exact 0.0 and, on an axis that
    is symmetric BECAUSE delta-neutral is a real state, that drew the centre line
    for a position nobody held. Measured on this journal before the fix: a closed
    short put reported +0.3171 and then 0.0000 for twelve further bars.

    P&L keeps reporting through the same bars, and that asymmetry is the point:
    cash flow to date with nothing left to mark IS the trade's result, so the
    figure is true and frozen. Exposure has no such post-close value.
    """
    spot, strike, vol = 100.0, 90.0, 0.40
    expiry = "2026-03-20"
    days = [f"2026-01-{n:02d}" for n in (12, 13, 14, 15, 16, 19, 20)]
    opens = [epoch_et(f"{day} 09:30:00") for day in days]
    upsert_bars(conn, conid="U1", symbol="AAA", bar_size="1d", source="yahoo",
                bars=[_bar(stamp, spot) for stamp in opens])
    expiry_ts = _ts(expiry) + 16 * 3600
    upsert_bars(
        conn, conid="OPT1", symbol="AAA  260320P00090000", bar_size="1d",
        source="yahoo",
        bars=[
            _bar(
                epoch_et(f"{day} 00:00:00"),
                bs_price(
                    spot, strike,
                    (expiry_ts - epoch_et(f"{day} 00:00:00")) / (365.0 * 86400),
                    vol, "P",
                ),
            )
            for day in days
        ],
    )
    points = [(stamp, spot) for stamp in opens]
    # Sold on the third bar, bought back on the fifth: two flat stretches, one at
    # each end, which are the two real shapes (context before entry, and after a
    # close) in a single series.
    leg = ReplayLeg(
        conid="OPT1", strike=strike, right="P", expiry=expiry,
        fills=((opens[2], -1.0, 3.0), (opens[4], 1.0, 1.0)),
    )
    marks = modelled_marks(conn, [leg], points, underlying_conid="U1", bar_size="1d")
    by_ts = {row[0]: row for row in marks}

    for stamp in opens[:2]:
        assert by_ts[stamp][2] is None, "delta before the opening fill"
    for stamp in opens[2:4]:
        assert by_ts[stamp][2], "delta must be reported while the position is held"
    for stamp in opens[4:]:
        assert by_ts[stamp][2] is None, "delta after the position went flat"

    # P&L is present on EVERY bar, including the flat ones, and frozen after the
    # close at what the trade made.
    assert all(row[1] is not None for row in marks), "P&L went missing"
    closed = [by_ts[stamp][1] for stamp in opens[4:]]
    assert len(set(closed)) == 1, "the realised figure moved after the close"
    assert closed[0] == pytest.approx((3.0 - 1.0) * 100.0), (
        "the frozen figure is not the credit received less the cost to close"
    )


def test_the_band_accepts_both_expiry_formats_the_payload_carries(conn):
    """A leg states 2026-09-04 while a snapshot row keeps IBKR's 20260918.
    Handling one and rejecting the other produced a band for the LEAP and none
    for any traded lifecycle.
    """
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
    from optjournal import replay as replay_mod

    calls = []
    original = replay_mod._vol_series
    monkeypatch.setattr(
        replay_mod, "_vol_series",
        lambda *a, **k: (calls.append(1), original(*a, **k))[1],
    )

    leg = ReplayLeg(
        conid="C1", strike=270.0, right="P", expiry="2026-09-04",
        fills=((_ts("2026-07-27"), -3.0, 5.24),),
    )
    points = [(_ts("2026-07-27") + h * 3600, 320.0 + h) for h in range(6)]
    replay_mod.replay_model(conn, [leg], points, underlying_conid="U1", bar_size="1h")

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
    from optjournal import replay as replay_mod

    calls = []
    monkeypatch.setattr(
        replay_mod, "_vol_series", lambda *a, **k: (calls.append(1), {})[1]
    )
    points = [(_ts("2026-07-27") + h * 3600, 320.0) for h in range(3)]
    leg = ReplayLeg(conid="C1", strike=270.0, right="P", expiry="2026-09-04")

    assert replay_mod.expected_move_band(
        conn, [], points, underlying_conid="U1", bar_size="1h", vols={}) == []
    assert replay_mod.modelled_marks(
        conn, [leg], points, underlying_conid="U1", bar_size="1h", vols={}) == []
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
    from optjournal.replay import band_contracts

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


# --------------------------------------------------------------------------
# Through `attach`, the interface `build_state` calls: when each price was
# observed, and which legs a bar is modelled from.
# --------------------------------------------------------------------------

_YEAR = 365.0 * 86400


def _hourly(conn, conid: str, closes: dict[str, float]) -> None:
    """Hourly underlying bars, stamped at their OPEN as the source stamps them."""
    upsert_bars(conn, conid=conid, symbol="AAA", bar_size="1h", source="yahoo",
                bars=[_bar(epoch_et(stamp), close) for stamp, close in closes.items()])


def _session(day: str, closes: list[float]) -> dict[str, float]:
    """One regular session of hourly bars from 09:30, one close each."""
    hours = ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    return {f"{day} {hour}:00": close for hour, close in zip(hours, closes, strict=False)}


def _option_close(conn, conid: str, day: str, price: float) -> None:
    """A contract's daily close, stamped at midnight ET as the source stamps it."""
    upsert_bars(conn, conid=conid, symbol=f"AAA {conid}", bar_size="1d",
                source="yahoo", bars=[_bar(epoch_et(f"{day} 00:00:00"), price)])


def _leg(conid, strike, right, expiry, at, quantity, price, marker="O") -> dict:
    return {
        "conid": conid, "strike": strike, "put_call": right, "expiry": expiry,
        "first_fill_at": at, "quantity": quantity, "avg_price": price,
        "multiplier": 100, "open_close": marker,
    }


def _replay(conn, legs: list[dict], *, closed: str | None = None) -> dict:
    """The replay `attach` builds for one lifecycle of hand-stated legs."""
    from optjournal.replay import attach

    for leg in legs:
        conn.execute(
            "INSERT OR IGNORE INTO securities (conid, symbol, underlying_conid,"
            " underlying_symbol, raw, updated_at) VALUES (?, ?, 'U1', 'AAA', '{}', 'now')",
            (leg["conid"], f"AAA {leg['conid']}"),
        )
    state = {
        "lifecycles": [{
            "conids": sorted({leg["conid"] for leg in legs}),
            "opened_at": legs[0]["first_fill_at"], "closed_at": closed,
            "underlying": "AAA", "label": "test",
            "status": "closed" if closed else "open",
            "events": [{"first_fill_at": leg["first_fill_at"], "label": "e",
                        "orders": [{"legs": [leg]}]} for leg in legs],
        }],
        "positions": [],
    }
    attach(conn, state)
    return next(iter(state["replays"].values()))


def _half(band: list[list[float]]) -> dict[int, float]:
    """Each band row's half-width by stamp. The band is rounded to four places,
    which is why the comparisons below allow 1e-4."""
    return {row[0]: (row[2] - row[1]) / 2 for row in band}


def test_each_bar_is_priced_at_its_close_and_a_close_is_known_only_after_it(conn):
    """A bar is stamped at its OPEN and carries its CLOSE, so that is when it is priced.

    Two defects with one cause. Pricing the 10:30 bar at 10:30 gave it an hour
    more to expiry than its price had. And an option's daily bar, stamped at
    midnight ET, was read as known from midnight: the whole session was banded
    with the vol of a close that had not happened yet, measured on the real 0DTE
    vertical of 2026-09-03 as a band a third of its true width at the open.
    """
    spot, strike = 100.0, 95.0
    expiry = epoch_et("2026-01-07 16:00:00")
    _hourly(conn, "U1",
            _session("2026-01-05", [spot] * 7) | _session("2026-01-06", [spot] * 7))
    upsert_bars(conn, conid="U1", symbol="AAA", bar_size="1d", source="yahoo",
                bars=[_bar(epoch_et(f"{day} 09:30:00"), spot)
                      for day in ("2026-01-05", "2026-01-06")])
    # Monday's close at 30% vol and Tuesday's at 60%, each priced at 16:00.
    for day, vol in (("2026-01-05", 0.30), ("2026-01-06", 0.60)):
        years = (expiry - epoch_et(f"{day} 16:00:00")) / _YEAR
        _option_close(conn, "OPT1", day, bs_price(spot, strike, years, vol, "P"))
    sold = epoch_et("2026-01-06 09:45:00")
    price = bs_price(spot, strike, (expiry - sold) / _YEAR, 0.30, "P")
    # Closed the same session, so the window is short enough to draw hourly.
    replay = _replay(conn, [_leg("OPT1", strike, "P", "2026-01-07",
                                 "2026-01-06 09:45:00", -1, price)],
                     closed="2026-01-06 15:50:00")

    half = _half(replay["band"])
    tuesday = [epoch_et(f"2026-01-06 {h}:30:00") for h in range(9, 16)]
    for stamp in tuesday[:-1]:
        closes = stamp + 3600
        assert half[stamp] == pytest.approx(
            spot * 0.30 * ((expiry - closes) / _YEAR) ** 0.5, abs=1e-4
        ), "a bar before 16:00 used Tuesday's close, or its stamp rather than its close"
    # The 15:30 bar closes at 16:00: the first moment Tuesday's close exists.
    assert half[tuesday[-1]] == pytest.approx(
        spot * 0.60 * ((expiry - epoch_et("2026-01-06 16:00:00")) / _YEAR) ** 0.5,
        abs=1e-4,
    )


def test_the_settlement_bar_has_a_band_of_no_width_and_a_mark_at_intrinsic(conn):
    """The last bar of an expiry session closes AT the expiry: nothing is left to move.

    Stamped 15:30 and priced as if it were, it kept half an hour of expected move
    and a time value the contract no longer had.
    """
    spot, strike = 100.0, 95.0
    expiry = epoch_et("2026-01-06 16:00:00")
    _hourly(conn, "U1",
            _session("2026-01-05", [spot] * 7) | _session("2026-01-06", [spot] * 7))
    upsert_bars(conn, conid="U1", symbol="AAA", bar_size="1d", source="yahoo",
                bars=[_bar(epoch_et("2026-01-05 09:30:00"), spot)])
    years = (expiry - epoch_et("2026-01-05 16:00:00")) / _YEAR
    _option_close(conn, "OPT1", "2026-01-05", bs_price(spot, strike, years, 0.30, "P"))
    # Held to expiry: the lifecycle ends that session with no closing fill yet.
    replay = _replay(conn, [_leg("OPT1", strike, "P", "2026-01-06",
                                 "2026-01-06 09:45:00", -1, 0.50)],
                     closed="2026-01-06 16:20:00")

    last = epoch_et("2026-01-06 15:30:00")
    band = {row[0]: row for row in replay["band"]}
    marks = {row[0]: row for row in replay["marks"]}
    assert band[last][1] == band[last][2] == spot, "a band at settlement"
    assert marks[last][1] == pytest.approx(50.0), (
        "the put expired out of the money, so the trade kept its whole credit"
    )


@pytest.mark.parametrize(("filled", "spot_then"), [
    # Inside a session: between the 10:30 close and the 11:30 close.
    ("2026-01-06 10:45:00", 105.0),
    # Fifteen minutes after the open: between the 102 the session opened at and
    # the first bar's 104 close. Not across the night from Monday's 100 close.
    ("2026-01-06 09:45:00", 102.5),
])
def test_a_fill_is_paired_with_the_spot_either_side_of_it(conn, filled, spot_then):
    """A fill's vol is solved against the underlying AT the fill, not an hour later.

    The chart's points are closes stamped at their bars' opens, and a fill was
    interpolated between those stamps, so it was paired with a spot an hour after
    it: measured on the real 7755C at 09:34, 7706.80 against 7688.02, which solved
    14.1% for an 18.4% contract.
    """
    strike, vol = 100.0, 0.40
    expiry = epoch_et("2026-01-09 16:00:00")
    _hourly(conn, "U1", {"2026-01-05 15:30:00": 100.0}
            | _session("2026-01-06", [104.0, 108.0, 112.0, 112.0]))
    # Tuesday opened 2 above Monday's close, as a session often does.
    opening = epoch_et("2026-01-06 09:30:00")
    upsert_bars(conn, conid="U1", symbol="AAA", bar_size="1h", source="yahoo",
                bars=[Bar(ts=opening, open=102.0, high=104.0, low=102.0, close=104.0,
                          volume=1)])
    at = epoch_et(filled)
    price = bs_price(spot_then, strike, (expiry - at) / _YEAR, vol, "C")
    replay = _replay(conn, [_leg("OPT1", strike, "C", "2026-01-09", filled, -1, price)])

    # The first bar to close after the fill is the first one its vol reaches.
    row = next(row for row in replay["band"] if row[0] + 3600 >= at)
    bar_spot = {ts: close for ts, close in replay["points"]}[row[0]]
    assert (row[2] - row[1]) / 2 == pytest.approx(
        bar_spot * vol * ((expiry - (row[0] + 3600)) / _YEAR) ** 0.5, abs=1e-4
    ), "the fill was paired with a spot other than the one around it"


def _daily(conn, days: list[str], spot: float) -> None:
    """Daily underlying bars, stamped at the session open as the source stamps them."""
    upsert_bars(conn, conid="U1", symbol="AAA", bar_size="1d", source="yahoo",
                bars=[_bar(epoch_et(f"{day} 09:30:00"), spot) for day in days])


def test_the_band_measures_to_the_legs_held_at_each_bar(conn):
    """After a roll the envelope is the NEW leg's: its expiry and its vol.

    The horizon was the nearest expiry over every leg the lifecycle ever held,
    and the vol their average, so a roll outward kept measuring to the contract
    it had just closed. Measured on the real open QCOM position, a short 10/16
    call rolled into an 11/20 put: the band was 43% too narrow, and it vanished on
    10/16 while the put was still open. On a bar holding nothing (the context
    before entry) every leg still alive is the basis, as before.
    """
    spot = 100.0
    days = ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08",
            "2026-01-09", "2026-01-12", "2026-01-13"]
    _daily(conn, days, spot)
    near, far = epoch_et("2026-01-09 16:00:00"), epoch_et("2026-01-23 16:00:00")
    for conid, strike, right, expiry, vol in (("CALL", 110.0, "C", near, 0.30),
                                              ("PUT", 90.0, "P", far, 0.50)):
        for day in days:
            closes = epoch_et(f"{day} 16:00:00")
            if closes < expiry:
                years = (expiry - closes) / _YEAR
                _option_close(conn, conid, day, bs_price(spot, strike, years, vol, right))

    def price(strike, right, expiry, vol, at):
        return bs_price(spot, strike, (expiry - epoch_et(at)) / _YEAR, vol, right)

    rolled = "2026-01-07 10:15:00"
    replay = _replay(conn, [
        _leg("CALL", 110.0, "C", "2026-01-09", "2026-01-05 10:15:00", -1,
             price(110.0, "C", near, 0.30, "2026-01-05 10:15:00")),
        _leg("CALL", 110.0, "C", "2026-01-09", rolled, 1,
             price(110.0, "C", near, 0.30, rolled), marker="C"),
        _leg("PUT", 90.0, "P", "2026-01-23", rolled, -1,
             price(90.0, "P", far, 0.50, rolled)),
    ])

    half = _half(replay["band"])

    def want(day, vol, horizon):
        return spot * vol * ((horizon - epoch_et(f"{day} 16:00:00")) / _YEAR) ** 0.5

    # Before entry nothing is held: both legs are the basis, as they always were.
    assert half[epoch_et("2026-01-02 09:30:00")] == pytest.approx(
        want("2026-01-02", 0.40, near), abs=1e-4)
    for day in ("2026-01-05", "2026-01-06"):
        assert half[epoch_et(f"{day} 09:30:00")] == pytest.approx(
            want(day, 0.30, near), abs=1e-4), f"{day}: the call alone was held"
    for day in ("2026-01-07", "2026-01-08", "2026-01-09", "2026-01-12", "2026-01-13"):
        assert epoch_et(f"{day} 09:30:00") in half, f"{day}: the band vanished with the put held"
        assert half[epoch_et(f"{day} 09:30:00")] == pytest.approx(
            want(day, 0.50, far), abs=1e-4), f"{day}: measured to a leg rolled away"


def _closes(conn, conid, strike, right, expiry, vol, spots: dict[str, float]) -> None:
    """A contract's daily closes at one vol, each priced at its own 16:00 close."""
    for day, spot in spots.items():
        at = epoch_et(f"{day} 16:00:00")
        if at < expiry:
            _option_close(conn, conid, day,
                          bs_price(spot, strike, (expiry - at) / _YEAR, vol, right))


def test_a_held_leg_the_model_cannot_price_yet_books_neither_its_cash_nor_its_value(conn):
    """P&L is cash plus value, so a leg must bring both or neither.

    A leg with no vol yet had its cash booked and its value dropped, so the P&L
    carried the whole premium as if it had been lost. A LEAP bought at 100.00 for
    a strike 100 in the money is below the European floor at 4%, so no vol
    reprices it, and until its first close the covered call it pays for read
    -$10,000 on the day it was opened.
    """
    spots = {"2026-01-05": 250.0, "2026-01-06": 250.0, "2026-01-07": 250.0}
    _daily(conn, list(spots), 250.0)
    leap, short = epoch_et("2028-01-21 16:00:00"), epoch_et("2026-02-20 16:00:00")
    _closes(conn, "SHORT", 270.0, "C", short, 0.30, spots)
    # The LEAP's first close is Tuesday's: nothing prices it on Monday.
    _closes(conn, "LEAP", 150.0, "C", leap, 0.30,
            {day: spot for day, spot in spots.items() if day != "2026-01-05"})
    opened = "2026-01-05 10:15:00"
    credit = bs_price(250.0, 270.0, (short - epoch_et(opened)) / _YEAR, 0.30, "C")
    replay = _replay(conn, [
        _leg("LEAP", 150.0, "C", "2028-01-21", opened, 1, 100.0),
        _leg("SHORT", 270.0, "C", "2026-02-20", opened, -1, credit),
    ])
    marks = {row[0]: row for row in replay["marks"]}

    monday = epoch_et("2026-01-05 09:30:00")
    worth = bs_price(250.0, 270.0, (short - epoch_et("2026-01-05 16:00:00")) / _YEAR,
                     0.30, "C")
    assert marks[monday][1] == pytest.approx((credit - worth) * 100, abs=0.01), (
        "the LEAP's debit was booked without the LEAP"
    )
    assert marks[monday][2] == pytest.approx(
        -_delta(250.0, 270.0, short, "2026-01-05", 0.30, "C"), abs=1e-4)
    # From its first close the LEAP is priced, cash and value together.
    at = epoch_et("2026-01-06 16:00:00")
    both = (bs_price(250.0, 150.0, (leap - at) / _YEAR, 0.30, "C") - 100.0
            + credit - bs_price(250.0, 270.0, (short - at) / _YEAR, 0.30, "C")) * 100
    assert marks[epoch_et("2026-01-06 09:30:00")][1] == pytest.approx(both, abs=0.01)


def _delta(spot, strike, expiry, day, vol, right):
    from optjournal.blackscholes import bs_delta

    at = epoch_et(f"{day} 16:00:00")
    return bs_delta(spot, strike, (expiry - at) / _YEAR, vol, right)


def test_a_leg_that_never_prices_still_books_its_cash_once_flat(conn):
    """Flat, a leg's value is zero whatever the model can say, so its cash counts.

    A leg with no solvable vol was skipped whole, even after it was closed, so a
    lifecycle holding one never ended at what it made.
    """
    spots = {"2026-01-05": 250.0, "2026-01-06": 250.0, "2026-01-07": 250.0}
    _daily(conn, list(spots), 250.0)
    short = epoch_et("2026-02-20 16:00:00")
    _closes(conn, "SHORT", 270.0, "C", short, 0.30, spots)
    opened, closed = "2026-01-05 10:15:00", "2026-01-06 10:15:00"

    def price(at):
        return bs_price(250.0, 270.0, (short - epoch_et(at)) / _YEAR, 0.30, "C")

    replay = _replay(conn, [
        _leg("LEAP", 150.0, "C", "2028-01-21", opened, 1, 100.0),
        _leg("SHORT", 270.0, "C", "2026-02-20", opened, -1, price(opened)),
        _leg("LEAP", 150.0, "C", "2028-01-21", closed, -1, 101.0, marker="C"),
        _leg("SHORT", 270.0, "C", "2026-02-20", closed, 1, price(closed), marker="C"),
    ], closed=closed)
    gross = (101.0 - 100.0 + price(opened) - price(closed)) * 100
    assert replay["marks"][-1][1] == pytest.approx(gross, abs=0.01), (
        "the flat LEAP's $100 of realised cash never reached the modelled P&L"
    )


def test_an_expired_leg_still_held_is_valued_at_its_settlement(conn):
    """A contract past expiry with no closing fill yet is worth what it settled at.

    A calendar's near short call expired $10 in the money while the far call kept
    the series going. Dropped from the value but not from the cash, the near
    leg's credit stayed in the P&L and its $1,000 settlement never did.
    """
    spots = {"2026-01-05": 100.0, "2026-01-06": 100.0, "2026-01-07": 110.0,
             "2026-01-08": 110.0, "2026-01-09": 110.0}
    for day, spot in spots.items():
        _daily(conn, [day], spot)
    near, far = epoch_et("2026-01-07 16:00:00"), epoch_et("2026-02-20 16:00:00")
    _closes(conn, "NEAR", 100.0, "C", near, 0.30, spots)
    _closes(conn, "FAR", 100.0, "C", far, 0.30, spots)
    opened = "2026-01-05 10:15:00"
    credit = bs_price(100.0, 100.0, (near - epoch_et(opened)) / _YEAR, 0.30, "C")
    debit = bs_price(100.0, 100.0, (far - epoch_et(opened)) / _YEAR, 0.30, "C")
    replay = _replay(conn, [
        _leg("NEAR", 100.0, "C", "2026-01-07", opened, -1, credit),
        _leg("FAR", 100.0, "C", "2026-02-20", opened, 1, debit),
    ])
    marks = {row[0]: row for row in replay["marks"]}

    thursday = epoch_et("2026-01-08 09:30:00")
    worth = bs_price(110.0, 100.0, (far - epoch_et("2026-01-08 16:00:00")) / _YEAR,
                     0.30, "C")
    assert marks[thursday][1] == pytest.approx(
        (credit - 10.0 - debit + worth) * 100, abs=0.01
    ), "the expired leg's settlement is missing from the P&L"
    assert marks[thursday][2] == pytest.approx(
        _delta(110.0, 100.0, far, "2026-01-08", 0.30, "C"), abs=1e-4
    ), "an expired contract has no delta left"
