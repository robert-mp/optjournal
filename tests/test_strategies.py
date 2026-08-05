"""Tests for grouping orders into strategies.

The motivating case is real: the account's first strangle was sold as two
orders (call and put) filled within the same second, each with its own
ib_order_id -- so the old order-per-card Trades view showed one strategy as
two unrelated positions.
"""

from __future__ import annotations

import pytest

from optjournal.strategies import (
    WINDOW_S,
    classify,
    position_groups,
    strategy_groups,
)


def _leg(**kw):
    leg = {
        "underlying_symbol": "META",
        "put_call": "P",
        "strike": 520.0,
        "expiry": "20260918",
        "buy_sell": "SELL",
        "open_close": "O",
        "quantity": -1,
        # The trade_legs view carries every money figure three ways -- native,
        # base and the currency it was billed in -- and every level above
        # re-aggregates from exactly these keys. A double that omitted them
        # would hide the aggregation it is meant to exercise.
        "currency": "USD",
        "proceeds": 115.0,
        "proceeds_base": 100.0,
        "commission": -0.71,
        "commission_base": -0.62,
        "realized_pnl": 0.0,
        "realized_pnl_base": 0.0,
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
    assert g["proceeds"]["base"] == pytest.approx(
        put["proceeds_base"] + call["proceeds_base"]
    )


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
    assert g["label"] == "Short put"
    assert g["orders"] == [o]
    # `_order` is a hand-built row, not one `orders_data` produced, so it has
    # no Money -- the group's figures come from the LEGS either way, which is
    # exactly the property under test.
    (leg,) = o["legs"]
    assert g["proceeds"]["base"] == pytest.approx(leg["proceeds_base"])
    assert g["proceeds"]["native"] == pytest.approx(leg["proceeds"])
    assert g["commission"]["base"] == pytest.approx(leg["commission_base"])


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


# ---------------------------------------------------------------- lifecycles


class _Ep:
    """Just enough Episode surface for position_groups."""

    def __init__(self, conid, trade_ids, *, closed=False, closed_at=None,
                 pnl=0.0, comm=0.0):
        self.conid = conid
        self.trade_ids = trade_ids
        self.is_closed = closed
        self.closed_at = closed_at
        self.realized_pnl_base = pnl
        self.commission_base = comm
        # Real episodes carry the native amount and the currency it settled in;
        # the lifecycle gates on them, so the double must too.
        self.realized_pnl = pnl
        self.commission = comm
        self.currency = "USD"


def test_open_and_close_events_link_into_one_closed_lifecycle():
    """The naked-put case: sold in July, bought back in August -- one
    position across its lifecycle, linked by the shared episode, with the
    episode's own P&L (already net of commission) on the card."""
    opening = _order("10", "2026-07-24 10:35:01",
                     [_leg(underlying_symbol="TSLA", strike=270.0)])
    closing = _order("11", "2026-08-03 09:55:23",
                     [_leg(underlying_symbol="TSLA", strike=270.0,
                           buy_sell="BUY", open_close="C")])
    ep = _Ep("C1", ["t1", "t2"], closed=True,
             closed_at="2026-08-03 09:55:23", pnl=684.59, comm=-3.62)
    lifecycles = position_groups(
        [opening, closing], episodes=[ep],
        trade_to_order={"t1": "10", "t2": "11"},
    )
    assert len(lifecycles) == 1
    lc = lifecycles[0]
    assert lc["status"] == "closed"
    assert lc["label"] == "Short put", "named by the shape it was OPENED as"
    assert (lc["opened_at"], lc["closed_at"]) == (
        "2026-07-24 10:35:01", "2026-08-03 09:55:23")
    assert lc["realized_pnl"]["base"] == 684.59, "episode-sourced, not fill-summed"
    assert len(lc["events"]) == 2, "both events stay visible beneath"


def test_an_open_lifecycle_reports_no_realised_pnl():
    """Same rule as the Dashboard: nothing counts until the position is flat."""
    opening = _order("10", "2026-08-03 11:11:19", [_leg()])
    ep = _Ep("C1", ["t1"], closed=False)
    (lc,) = position_groups([opening], episodes=[ep],
                            trade_to_order={"t1": "10"})
    assert lc["status"] == "open"
    assert lc["realized_pnl"] is None
    assert lc["commission"] is None


def test_unrelated_contracts_never_share_a_lifecycle():
    a = _order("10", "2026-07-24 10:35:01", [_leg(underlying_symbol="TSLA")])
    b = _order("11", "2026-08-03 11:11:19", [_leg(underlying_symbol="META")])
    eps = [_Ep("C1", ["t1"]), _Ep("C2", ["t2"])]
    got = position_groups([a, b], episodes=eps,
                          trade_to_order={"t1": "10", "t2": "11"})
    assert len(got) == 2


def test_a_roll_event_chains_lifecycles_into_one_campaign():
    """A roll closes episode A and opens episode B in one event; sharing an
    episode with each side links the whole chain into one card."""
    opening = _order("10", "2026-07-24 10:00:00", [_leg()])
    roll = _order("11", "2026-08-20 10:00:00", [
        _leg(buy_sell="BUY", open_close="C"),
        _leg(expiry="20261016", open_close="O"),
    ])
    eps = [
        _Ep("C1", ["t1", "t2"], closed=True, closed_at="2026-08-20 10:00:00",
            pnl=100.0),
        _Ep("C2", ["t3"], closed=False),
    ]
    got = position_groups([opening, roll], episodes=eps,
                          trade_to_order={"t1": "10", "t2": "11", "t3": "11"})
    assert len(got) == 1, "the campaign is one lifecycle"
    lc = got[0]
    assert lc["status"] == "open", "the rolled-into leg is still open"
    assert lc["realized_pnl"] is None, "campaign not decided yet"
    assert {e["label"] for e in lc["events"]} == {"Short put", "Roll"}


def test_grouping_layers_do_not_mutate_the_orders_they_receive():
    """build_state now fetches orders_data() ONCE and hands the same list to
    the flat view, strategy_groups and position_groups. That dedup is only
    sound while both grouping layers are read-only lenses over their input --
    if either ever annotated an order dict in place, the three panels would
    stop being independent views of one truth. This is the invariant the
    single fetch in web.py cites.
    """
    import copy

    put = _order("1", "2026-08-03 11:11:19", [_leg()])
    call = _order("2", "2026-08-03 11:11:19", [_leg(put_call="C", strike=675.0)])
    orders = [put, call]
    before = copy.deepcopy(orders)

    strategy_groups(orders)
    position_groups(
        orders,
        episodes=[_Ep("C1", ["t1"], closed=False)],
        trade_to_order={"t1": "1"},
    )

    assert orders == before, "a grouping layer mutated its input"


def test_single_legs_are_named_by_right_and_position_direction():
    """A sold put is a "Short put", not a generic "Single leg" -- and the
    direction is the POSITION'S: buying back a put CLOSES a short, so the
    buyback reads "Short put close", never "Long put". A leg the naming
    cannot be honest about (a stock leg with no right, or a missing
    open/close marker) keeps the generic label rather than guessing.
    """
    cases = [
        (dict(buy_sell="SELL", open_close="O", put_call="P"), "Short put"),
        (dict(buy_sell="BUY", open_close="O", put_call="C"), "Long call"),
        (dict(buy_sell="BUY", open_close="O", put_call="P"), "Long put"),
        (dict(buy_sell="SELL", open_close="O", put_call="C"), "Short call"),
        # closes: the fill's side inverts to name the position it closes
        (dict(buy_sell="BUY", open_close="C", put_call="P"), "Short put close"),
        (dict(buy_sell="SELL", open_close="C", put_call="C"), "Long call close"),
        # honest fallbacks
        (dict(buy_sell="SELL", open_close="O", put_call=None), "Single leg"),
        (dict(buy_sell="SELL", open_close="", put_call="P"), "Single leg"),
    ]
    for kw, expected in cases:
        assert classify([_leg(**kw)]) == expected, expected


def test_a_right_less_closing_leg_is_still_named_a_close():
    """A stock leg has no put/call, and the single-leg branch bailed out before
    applying the closing suffix -- so an equities lifecycle gave its opening
    AND its closing event the identical label "Single leg". The Trades tab
    captions an event by comparing its label to the lifecycle's ("X" -> Opened,
    "X close" -> Closed), so both matched the first case and a share SALE that
    closed the position was captioned "Opened" while its own chip read STC.

    The direction is what a missing right makes unknowable; whether the order
    closed is known from open_close alone, so the suffix still applies.
    """
    opening = [{"put_call": None, "buy_sell": "BUY", "open_close": "O", "symbol": "SIVE"}]
    closing = [{"put_call": None, "buy_sell": "SELL", "open_close": "C", "symbol": "SIVE"}]
    assert classify(opening) == "Single leg"
    assert classify(closing) == "Single leg close"
    # The caption rule the page applies, reproduced here so the pin covers the
    # behaviour the user sees rather than just the string.
    lifecycle_label = classify(opening)
    assert classify(closing) == lifecycle_label + " close"
    # Options keep the direction naming they already had.
    assert classify([{"put_call": "P", "buy_sell": "SELL", "open_close": "O"}]) == "Short put"
    assert classify([{"put_call": "P", "buy_sell": "BUY", "open_close": "C"}]) == "Short put close"


def test_every_level_aggregates_the_leaf_rows_not_the_level_below():
    """Leg, order, strategy group and lifecycle all derive from the same fill
    rows, and each asks the gate against the union of THOSE legs' currencies.

    Summing the level below would give an identical base -- sums are
    associative -- but it cannot gate correctly. An order spanning currencies
    has `native: null`, and a group summing that order could not tell a native
    withheld for being mixed from one that was never there, so it would gate as
    though that order had contributed nothing.
    """
    usd = _leg(proceeds=100.0, proceeds_base=90.0, currency="USD")
    sek = _leg(proceeds=1000.0, proceeds_base=95.0, currency="SEK",
               put_call="C", strike=675.0)
    (mixed,) = strategy_groups([_order("1", "2026-08-03 11:11:19", [usd, sek])])

    # The base is complete regardless -- it is the addable reading.
    assert mixed["proceeds"]["base"] == pytest.approx(185.0)
    # ...and the exact figure is withheld, because no single currency accounts
    # for it. An exact-looking 100.0 covering half a total would be worse.
    assert mixed["proceeds"]["native"] is None
    assert mixed["proceeds"]["ccy"] is None

    # A single-currency group answers, through the very same code path.
    (uniform,) = strategy_groups([
        _order("2", "2026-08-03 11:11:19",
               [_leg(proceeds=100.0, proceeds_base=90.0, currency="USD"),
                _leg(proceeds=40.0, proceeds_base=36.0, currency="USD",
                     put_call="C", strike=675.0)])
    ])
    assert uniform["proceeds"]["native"] == pytest.approx(140.0)
    assert uniform["proceeds"]["ccy"] == "USD"
    assert uniform["proceeds"]["base"] == pytest.approx(126.0)
