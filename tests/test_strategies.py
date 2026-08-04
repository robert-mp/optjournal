"""Tests for grouping orders into strategies.

The motivating case is real: the account's first strangle was sold as two
orders (call and put) filled within the same second, each with its own
ib_order_id -- so the old order-per-card Trades view showed one strategy as
two unrelated positions.
"""

from __future__ import annotations

from optjournal.strategies import WINDOW_S, classify, strategy_groups


def _leg(**kw):
    leg = {
        "underlying_symbol": "META",
        "put_call": "P",
        "strike": 520.0,
        "expiry": "20260918",
        "buy_sell": "SELL",
        "open_close": "O",
        "quantity": -1,
        "proceeds_base": 100.0,
    }
    leg.update(kw)
    return leg


def _order(oid, at, legs, **totals):
    order = {
        "ib_order_id": oid,
        "first_fill_at": at,
        "fills": len(legs),
        "proceeds_base": sum(leg.get("proceeds_base") or 0 for leg in legs),
        "commission_base": -0.62 * len(legs),
        "realized_pnl_base": 0.0,
        "underlyings": legs[0]["underlying_symbol"] if legs else "?",
        "legs": legs,
    }
    order.update(totals)
    return order


def test_same_second_orders_on_one_underlying_group_as_a_strangle():
    """The real META case: two orders, same second, put + call, same expiry,
    same side, different strikes -- one strategy, both orders kept."""
    put = _order("1", "2026-08-03 11:11:19", [_leg()])
    call = _order("2", "2026-08-03 11:11:19", [_leg(put_call="C", strike=675.0)])
    groups = strategy_groups([put, call])
    assert len(groups) == 1
    g = groups[0]
    assert g["label"] == "Strangle"
    assert sorted(g["order_ids"]) == ["1", "2"]
    assert len(g["orders"]) == 2, "member orders stay visible beneath the group"
    assert g["proceeds_base"] == put["proceeds_base"] + call["proceeds_base"]


def test_orders_outside_the_window_stay_separate():
    a = _order("1", "2026-08-03 11:11:19", [_leg()])
    b = _order(
        "2", "2026-08-03 11:14:00", [_leg(put_call="C", strike=675.0)]
    )  # 161s later
    assert len(strategy_groups([a, b])) == 2


def test_same_time_different_underlyings_stay_separate():
    a = _order("1", "2026-08-03 11:11:19", [_leg()])
    b = _order("2", "2026-08-03 11:11:19", [_leg(underlying_symbol="TSLA")])
    groups = strategy_groups([a, b])
    assert len(groups) == 2


def test_a_group_of_one_is_exactly_its_order():
    o = _order("1", "2026-08-03 11:11:19", [_leg()])
    (g,) = strategy_groups([o])
    assert g["label"] == "Single leg"
    assert g["orders"] == [o]
    assert (g["proceeds_base"], g["commission_base"]) == (
        o["proceeds_base"], o["commission_base"],
    )


def test_classification_is_derived_from_the_combined_legs():
    same = dict(buy_sell="SELL", open_close="O", expiry="20260918")
    cases = [
        ([_leg(**same), _leg(**same, put_call="C", strike=675.0)], "Strangle"),
        ([_leg(**same), _leg(**same, put_call="C")], "Straddle"),
        ([_leg(**same), _leg(**same, strike=500.0)], "Put vertical"),
        ([_leg(**same), _leg(put_call="P", strike=520.0, expiry="20261016",
                             buy_sell="SELL", open_close="O")], "Put calendar"),
        ([_leg(**same), _leg(put_call="P", strike=480.0, expiry="20261016",
                             buy_sell="SELL", open_close="O")], "Put diagonal"),
        ([_leg(open_close="O"), _leg(put_call="C", strike=675.0, open_close="C")],
         "Roll"),
        ([_leg(strike=s, put_call=pc, **{"buy_sell": bs, "open_close": "O",
                                         "expiry": "20260918"})
          for s, pc, bs in [(480, "P", "BUY"), (520, "P", "SELL"),
                            (600, "C", "SELL"), (640, "C", "BUY")]],
         "Iron condor"),
    ]
    for legs, expected in cases:
        assert classify(legs) == expected, expected


def test_closing_shapes_are_named_as_closes():
    same = dict(buy_sell="BUY", open_close="C", expiry="20260918")
    legs = [_leg(**same), _leg(**same, put_call="C", strike=675.0)]
    assert classify(legs) == "Strangle close"


def test_window_constant_is_seconds_and_modest():
    """The window is the heuristic's whole risk surface; pin its scale so a
    casual edit to minutes does not silently merge unrelated trades."""
    assert 1 <= WINDOW_S <= 300
