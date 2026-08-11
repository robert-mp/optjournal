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
    BandContract,
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


def test_delta_around_carries_an_absent_delta_rather_than_flattening_it(conn):
    """A None in the marks must reach the card as None.

    `modelled_marks` now reports delta as None on a bar holding nothing, and a
    closing event's "after" is exactly such a bar. Flattening it to 0.0 here would
    put the fabrication back one layer down: the card would read "0.32 -> 0.00",
    which on a symmetric axis claims the position ended delta-neutral rather than
    ended.
    """
    marks = [[100, 0.0, None], [200, 5.0, 0.32], [300, 9.0, None]]
    assert delta_around(marks, 250) == (0.32, None), "a CLOSING event"
    assert delta_around(marks, 150) == (None, 0.32), "an OPENING event"


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
    marks = modelled_marks(conn, [leg], points, underlying_conid="U1")
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
    replay_mod.replay_model(conn, [leg], points, underlying_conid="U1")

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
        conn, [], points, underlying_conid="U1", vols={}) == []
    assert replay_mod.modelled_marks(
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
