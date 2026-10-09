"""`stats.py` at its own interface, for the rules the payload does not pin.

WHY THIS FILE IS SMALL, and why that is the finding. An architecture review
counted name references across `tests/` and reported four functions -- `fx_quotes`,
`available_years`, `cohort_data`, `stats_data` -- as having zero tests. The count
was right and the conclusion was not: all four run on every `build_state`, so the
payload suite exercises them transitively, and a reference count cannot see that.

So the gap was measured instead of inferred. Twelve documented rules across the
four functions were broken one at a time against the whole suite. EIGHT were
caught -- the option-currency restriction, the rate inversion, the newest-snapshot
preference, the year ordering, the category filter, `win_rate` in both views, and
the day list's realised figures. Four were not, and those four are what this file
holds. A test for any of the other eight would be a second assertion of something
already guarded, which is how a suite gets large without getting stronger.

Every case here is a `None`-versus-zero or a filter boundary: the shapes that
survive a refactor because a payload still parses and a page still renders, while
the number quietly stops meaning what it says.
"""

from __future__ import annotations

import sqlite3

import pytest
from conftest import add_statement, connect_migrated

from optjournal.money import Money
from optjournal.stats import (
    Cohort,
    MonthStats,
    cohort_data,
    fx_quotes,
    month_stats,
    stats_data,
)


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    """An empty migrated journal with the statement row other rows hang off."""
    c = connect_migrated(tmp_path / "stats.db")
    add_statement(c)
    return c


def _option_trade(conn: sqlite3.Connection, *, trade_id: str, currency: str) -> None:
    """One OPT fill, which is what puts a currency in the offerable set."""
    conn.execute(
        "INSERT INTO trades (broker, trade_id, ib_exec_id, transaction_id,"
        " account_id, trade_date, asset_category, symbol, quantity, currency,"
        " fx_rate_to_base, raw, source_file, first_seen_at)"
        " VALUES ('IBKR',?,?,?,'U1','2026-03-02','OPT','SPY',1,?,1.0,'{}',"
        "'t.xml','2026-03-02T00:00:00Z')",
        (trade_id, trade_id, trade_id, currency),
    )
    conn.commit()


def _snapshot(
    conn: sqlite3.Connection, *, currency: str, rate: float, day: str
) -> None:
    """One position-snapshot row, the only dated FX rate the statement gives.

    `fx_rate_to_base` is `NOT NULL` in the schema, so the query's
    `IS NOT NULL` half of the guard is unreachable from real data and only the
    `> 0` half is testable -- which is the half that matters, since it is the
    one standing in front of a division.
    """
    conn.execute(
        "INSERT INTO position_snapshots (account_id, conid, symbol,"
        " asset_category, currency, fx_rate_to_base, report_date, position,"
        " raw, source_file, ingested_at)"
        " VALUES ('U1',?,?,'OPT',?,?,?,1,'{}','t.xml','2026-03-02T00:00:00Z')",
        (f"c{currency}{day}", f"S{currency}", currency, rate, day),
    )
    conn.commit()


def test_a_snapshot_rate_of_zero_is_refused_rather_than_dividing(conn):
    """The one arithmetic trap in `fx_quotes`, and nothing else pinned it.

    The stored rate is native -> base and the quote is its RECIPROCAL, so a rate
    of zero is not merely a bad datum -- it is a ZeroDivisionError on a page load.
    The query filters `> 0` for exactly that reason, and the filter is invisible
    in every payload test because no real snapshot has ever carried one.

    A negative rate is refused on the same clause. It would not raise; it would
    quote a currency at a negative rate and render every restated total with the
    sign flipped, which is the worse outcome of the two because it looks like a
    number.
    """
    _option_trade(conn, trade_id="t1", currency="USD")
    _snapshot(conn, currency="USD", rate=0.0, day="2026-03-02")

    assert fx_quotes(conn, "EUR") == [], (
        "a zero rate reached the reciprocal; the > 0 clause is what stops a "
        "page load dividing by it"
    )

    _snapshot(conn, currency="USD", rate=-0.88, day="2026-03-03")
    assert fx_quotes(conn, "EUR") == [], "a negative rate would flip every total"

    # And the same currency quotes normally once a real rate arrives, so the
    # filter is rejecting the ROW rather than blacklisting the currency.
    _snapshot(conn, currency="USD", rate=0.88, day="2026-03-04")
    assert [q["code"] for q in fx_quotes(conn, "EUR")] == ["USD"]


def test_the_base_currency_is_never_offered_as_an_alternative(conn):
    """A quote converts base into something ELSE; base into base is 1.0.

    Offering it puts a no-op entry in the page's currency toggle, which reads as
    a restatement the reader can choose and produces the identical figures. The
    guard is one clause in a loop, and no payload test covers it because the real
    journal's base currency has no snapshot row of its own.
    """
    _option_trade(conn, trade_id="t1", currency="EUR")
    _option_trade(conn, trade_id="t2", currency="USD")
    _snapshot(conn, currency="EUR", rate=1.0, day="2026-03-02")
    _snapshot(conn, currency="USD", rate=0.88, day="2026-03-02")

    codes = [q["code"] for q in fx_quotes(conn, "EUR")]
    assert codes == ["USD"], "the base currency offered to convert into itself"

    # Case-insensitively, because the column is not normalised on the way in.
    assert [q["code"] for q in fx_quotes(conn, "eur")] == ["USD"]


def test_a_cohort_with_no_episodes_reports_no_average_not_a_zero(conn):
    """`avg_pnl` is None for an empty cohort, and the view must carry that.

    `Money.per` returns None on a zero denominator rather than dividing, so the
    honest reading of "no round trips yet" is "no average". Flattening it to a
    zero payload states that the average outcome was break-even, which is a
    measurement, and the 0DTE cohort is empty on this account for months at a
    time -- so the wrong shape is the common one, not the edge case.
    """
    empty = Cohort(label="0DTE")
    assert empty.avg_pnl is None, "an empty cohort cannot have an average"
    assert cohort_data(empty)["avg_pnl"] is None, (
        "a zero payload here claims the average outcome was break-even"
    )

    # Non-empty still carries the figure, so the None is about the denominator
    # and not about the key being dropped.
    decided = Cohort(label="0DTE", episodes=2, wins=1, losses=1,
                     net_pnl=Money.restated(100.0))
    assert cohort_data(decided)["avg_pnl"] == {
        "base": 50.0, "native": None, "ccy": None
    }


def test_a_period_with_no_wins_reports_no_average_win_not_a_zero(conn):
    """The same rule one level up, in `stats_data`.

    `avg_win` and `avg_loss` are None until a period has a win or a loss to
    average, and a month with no closed round trip is ordinary in this journal.
    A zero payload there reads as "the average win was $0.00", which invites the
    conclusion that trades closed flat rather than that none closed at all.
    """
    stats = MonthStats(month="2026-03", base_currency="EUR", asset_category="OPT")
    assert (stats.avg_win, stats.avg_loss) == (None, None)

    view = stats_data(stats)
    assert view["avg_win"] is None and view["avg_loss"] is None, (
        "a zero payload claims an average over trades that do not exist"
    )
    # The keys are PRESENT and null rather than absent: the page tests for a null
    # value, never for a missing property. Same contract as `Money.payload`.
    assert "avg_win" in view and "avg_loss" in view

    stats.avg_win = Money.restated(250.0)
    assert stats_data(stats)["avg_win"] == {
        "base": 250.0, "native": None, "ccy": None
    }

    # The two figures derived from the same populations follow the same rule:
    # nothing closed, no average outcome; nothing lost, no profit factor. An
    # all-wins month is the case that matters for the second -- the ratio is
    # infinite there, and a big finite number would read as a measurement.
    assert view["avg_pnl"] is None and view["profit_factor"] is None
    assert "avg_pnl" in view and "profit_factor" in view


def _leg(
    conn: sqlite3.Connection,
    *,
    conid: str,
    order_id: str,
    at: str,
    qty: int,
    proceeds: float,
    pnl: float | None,
    put_call: str = "P",
    open_close: str | None = None,
    notes: str | None = None,
    expiry: str = "2026-04-17",
) -> None:
    """One OPT fill on `conid`, enough for `build_history` to fold into episodes.

    `fifo_pnl_realized` is IBKR's own figure and only the closing fill carries
    one, which is the shape the real statement has: an opening sale realises
    nothing.
    """
    trade_id = f"{conid}-{at}-{qty}"
    conn.execute(
        "INSERT INTO trades (broker, trade_id, ib_exec_id, transaction_id,"
        " ib_order_id, account_id, trade_date, date_time, asset_category,"
        " symbol, conid, underlying_symbol, put_call, strike, expiry,"
        " multiplier, buy_sell, open_close, notes, quantity, trade_price, currency,"
        " fx_rate_to_base, proceeds, proceeds_base, ib_commission,"
        " ib_commission_base, fifo_pnl_realized, fifo_pnl_realized_base,"
        " raw, source_file, first_seen_at)"
        " VALUES ('IBKR',?,?,?,?,'U1',?,?,'OPT',?,?,'SPY',?,500,?,"
        "100,?,?,?,?,?,'USD',1.0,?,?,-1.0,-1.0,?,?,'{}','t.xml',"
        "'2026-03-02T00:00:00Z')",
        (trade_id, trade_id, trade_id, order_id, at[:10], at,
         f"SPY  {put_call}{conid}", conid, put_call, expiry,
         "SELL" if qty < 0 else "BUY",
         open_close or ("O" if pnl is None else "C"), notes,
         qty, abs(proceeds) / (abs(qty) * 100),
         proceeds, proceeds, pnl, pnl),
    )
    conn.commit()


def _strangle(conn: sqlite3.Connection) -> None:
    """A strangle sold and bought back: one position, two contracts, split
    outcomes.

    Two conids on separate order ids filled in the SAME SECOND, which is how
    every real multi-leg event in this journal arrives (see `campaigns.py`). The
    put wins 400 and the call loses 100, so the position nets +300 while its legs
    disagree.
    """
    _leg(conn, conid="1", order_id="10", at="2026-03-02 15:00:00",
         qty=-1, proceeds=500.0, pnl=None, put_call="P")
    _leg(conn, conid="2", order_id="11", at="2026-03-02 15:00:00",
         qty=-1, proceeds=300.0, pnl=None, put_call="C")
    _leg(conn, conid="1", order_id="12", at="2026-03-09 15:00:00",
         qty=1, proceeds=-100.0, pnl=400.0, put_call="P")
    _leg(conn, conid="2", order_id="13", at="2026-03-09 15:00:00",
         qty=1, proceeds=-400.0, pnl=-100.0, put_call="C")


def test_a_strangle_scores_each_leg_as_its_own_outcome(conn):
    """The scoreboard counts closes, the money's own unit.

    A strangle is one position made of two contracts whose outcomes disagree, so
    it scores a win and a loss, which is what a broker trade log shows. The
    count and the money read the same two closes.
    """
    _strangle(conn)
    s = month_stats(conn, "2026-03", base_currency="EUR")

    assert (s.wins, s.losses) == (1, 1), "the call leg is its own loss"
    assert s.closes == 2
    assert s.net_pnl.base == 300.0
    assert s.commissions.base == -4.0
    assert s.total_trades == 4


def test_a_closed_leg_is_decided_while_its_partner_is_still_open(conn):
    """A close is an outcome the day it fills, whatever its position does.

    The put is bought back and the call stays open, so the put's +400 is both in
    Net P&L and a win, and nothing about the open call holds either back.
    """
    _leg(conn, conid="1", order_id="10", at="2026-03-02 15:00:00",
         qty=-1, proceeds=500.0, pnl=None, put_call="P")
    _leg(conn, conid="2", order_id="11", at="2026-03-02 15:00:00",
         qty=-1, proceeds=300.0, pnl=None, put_call="C")
    _leg(conn, conid="1", order_id="12", at="2026-03-09 15:00:00",
         qty=1, proceeds=-100.0, pnl=400.0, put_call="P")

    s = month_stats(conn, "2026-03", base_currency="EUR")
    assert s.net_pnl.base == 400.0
    assert (s.wins, s.losses, s.closes) == (1, 0, 1), (
        "the close is a finished outcome on its own terms"
    )
    assert s.open_episodes == 1


def test_buying_back_part_of_a_contract_is_one_loss_on_its_day(conn):
    """The QCOM buyback of 2026-10-08. Four puts were sold, then 2 bought back in
    one order that filled as two executions, at a loss. IBKR booked the loss that
    day and it was in October's P&L, while October showed no losing trade, because
    the contract was still open. One order on one contract is one close, so it is one
    loss, on its day, and the other 2 puts stay open."""
    _leg(conn, conid="1", order_id="10", at="2026-09-14 12:51:29", qty=-4,
         proceeds=2000.0, pnl=None)
    _leg(conn, conid="1", order_id="11", at="2026-10-08 11:56:10", qty=1,
         proceeds=-1030.0, pnl=-530.60)
    _leg(conn, conid="1", order_id="11", at="2026-10-08 11:56:11", qty=1,
         proceeds=-1030.0, pnl=-530.14)

    s = month_stats(conn, "2026-10")
    assert (s.closes, s.wins, s.losses) == (1, 0, 1), "two fills of one order"
    assert s.largest_loss.base == pytest.approx(-1060.74)
    assert s.avg_pnl.base * s.closes == pytest.approx(s.net_pnl.base)
    assert s.open_episodes == 1, "the other 2 puts are still held"
    assert [(d.day, d.trades) for d in s.days] == [("2026-10-08", 2)]


def test_a_contract_closed_by_two_orders_is_two_closes(conn):
    """Two decisions on one contract on one day, each its own win or loss."""
    _leg(conn, conid="1", order_id="10", at="2026-03-02 10:00:00", qty=-2,
         proceeds=600.0, pnl=None)
    _leg(conn, conid="1", order_id="11", at="2026-03-09 10:00:00", qty=1,
         proceeds=-100.0, pnl=200.0)
    _leg(conn, conid="1", order_id="12", at="2026-03-09 15:00:00", qty=1,
         proceeds=-400.0, pnl=-100.0)

    s = month_stats(conn, "2026-03")
    assert (s.closes, s.wins, s.losses) == (2, 1, 1)
    assert (s.largest_win.base, s.largest_loss.base) == (200.0, -100.0)


def test_an_order_working_overnight_closes_once_on_each_day(conn):
    """A close is dated by the day IBKR books it, so one order filling across a
    month end scores in each month the money lands in, and every month's closes
    still sum to its Net P&L."""
    _leg(conn, conid="1", order_id="10", at="2026-09-02 10:00:00", qty=-2,
         proceeds=600.0, pnl=None)
    _leg(conn, conid="1", order_id="11", at="2026-09-30 15:59:00", qty=1,
         proceeds=-100.0, pnl=200.0)
    _leg(conn, conid="1", order_id="11", at="2026-10-01 09:31:00", qty=1,
         proceeds=-350.0, pnl=-50.0)

    for month, expected in (("2026-09", (1, 1, 0)), ("2026-10", (1, 0, 1))):
        s = month_stats(conn, month)
        assert (s.closes, s.wins, s.losses) == expected, month
        assert s.avg_pnl.base * s.closes == pytest.approx(s.net_pnl.base), month


def _lc(label, pnl, *, closed="2026-08-20", status="closed"):
    return {"label": label, "status": status, "closed_at": closed,
            "realized_pnl": None if pnl is None else {"base": pnl, "native": None, "ccy": None}}


def test_the_strategy_ranking_sums_decided_positions_per_opening_shape():
    """Summed, not averaged: two modest strangles outrank one lucky put."""
    from optjournal.stats import strategy_ranking

    lcs = [_lc("Strangle", 300.0), _lc("Strangle", 250.0), _lc("Short put", 500.0),
           _lc("Put vertical", -120.0)]
    r = strategy_ranking(lcs, None)
    assert (r["best"]["label"], r["best"]["pnl"]["base"], r["best"]["decided"]) == \
        ("Strangle", 550.0, 2)
    assert (r["worst"]["label"], r["worst"]["pnl"]["base"]) == ("Put vertical", -120.0)


def test_the_strategy_ranking_counts_only_what_decided_inside_the_period():
    from optjournal.stats import strategy_ranking

    lcs = [_lc("Strangle", 900.0, status="open"),          # not decided
           _lc("Short put", 400.0, closed="2026-07-31"),    # another month
           _lc("Short call", 50.0), _lc("Short put", 10.0)]
    r = strategy_ranking(lcs, "2026-08")
    assert (r["best"]["label"], r["best"]["pnl"]["base"]) == ("Short call", 50.0)
    assert (r["worst"]["label"], r["worst"]["pnl"]["base"]) == ("Short put", 10.0)
    # IBKR's compact dates match a period exactly as ISO ones do.
    assert strategy_ranking([_lc("Strangle", 5.0, closed="20260820")], "2026-08")["best"]


def test_one_strategy_has_no_worst_and_nothing_decided_has_neither():
    """A tile calling the same strategy best AND worst is arithmetic, not news."""
    from optjournal.stats import strategy_ranking

    one = strategy_ranking([_lc("Strangle", 5.0), _lc("Strangle", -9.0)], None)
    assert one["best"]["label"] == "Strangle" and one["worst"] is None
    assert strategy_ranking([], None) == {"best": None, "worst": None}
    assert strategy_ranking([_lc("Strangle", 5.0, status="open")], None) == \
        {"best": None, "worst": None}


def test_account_fees_are_signed_so_a_refund_month_agrees_with_the_costs_tab(conn):
    """September 2026 on the real account was refunded more than it was charged.

    The Dashboard took the magnitude of the signed sum, so a net CREDIT of 0.01
    read as a 0.01 charge while the Costs tab (`costs.build_costs`) reported the
    credit. Both now flip the sign once, on the total.
    """
    from optjournal.costs import build_costs

    for tid, day, amount in (("f1", "2026-09-02 13:33:52", 1.30),
                             ("f2", "2026-09-02 17:34:36", -1.29)):
        conn.execute(
            "INSERT INTO cash_transactions (transaction_id, account_id, date_time,"
            " type, description, amount, currency, fx_rate_to_base, amount_base,"
            " raw, source_file, first_seen_at)"
            " VALUES (?, 'U1', ?, 'Other Fees', 'OPRA NP L1', ?, 'EUR', 1.0, ?,"
            " '{}', 't.xml', 'now')",
            (tid, day, amount, amount),
        )
    s = month_stats(conn, "2026-09")
    assert s.account_friction_base == pytest.approx(-0.01)
    assert s.account_friction_base == pytest.approx(
        build_costs(conn, period="2026-09").unattributable.base)


def test_a_reversal_through_zero_scores_the_long_and_the_short_apart(conn):
    """`pnl/s_cross_zero.py`: long 2 calls, one SELL 3 (`C;O`) realising +198 in
    September, the leftover short bought back in October for +149.

    Read as one opening sale, September showed no P&L and no outcome, and
    October one +347 win. The long finished in September, so September has its
    money and its win; the short is its own decision, decided in October.
    """
    _leg(conn, conid="1", order_id="10", at="2026-09-01 10:00:00",
         qty=2, proceeds=-200.0, pnl=None, put_call="C")
    _leg(conn, conid="1", order_id="11", at="2026-09-10 10:00:00",
         qty=-3, proceeds=600.0, pnl=198.0, put_call="C", open_close="C;O")
    _leg(conn, conid="1", order_id="12", at="2026-10-05 10:00:00",
         qty=1, proceeds=-50.0, pnl=149.0, put_call="C")
    september, october = (month_stats(conn, m) for m in ("2026-09", "2026-10"))
    assert (september.net_pnl.base, september.wins, september.closes) == (
        198.0, 1, 1)
    assert (october.net_pnl.base, october.wins, october.closes) == (
        149.0, 1, 1)


def test_expirations_on_one_day_do_not_merge_unrelated_positions(conn):
    """`pnl/s_expiry_merge.py`: a short put opened 2026-09-01 and a long call
    opened three weeks later expire together. IBKR books both at 16:20:00 under
    orders of its own, inside the 90-second window, so the two decisions scored
    as one -102 loss where they were a +199 win and a -301 loss."""
    _leg(conn, conid="1", order_id="1001", at="2026-09-01 10:00:00", qty=-1,
         proceeds=200.0, pnl=None, put_call="P")
    _leg(conn, conid="2", order_id="1002", at="2026-09-20 11:00:00", qty=1,
         proceeds=-300.0, pnl=None, put_call="C")
    _leg(conn, conid="1", order_id="9001", at="2026-10-16 16:20:00", qty=1,
         proceeds=0.0, pnl=199.0, put_call="P", notes="Ep")
    _leg(conn, conid="2", order_id="9002", at="2026-10-16 16:20:00", qty=-1,
         proceeds=0.0, pnl=-301.0, put_call="C", notes="Ep")
    s = month_stats(conn, None)
    assert (s.closes, s.wins, s.losses) == (2, 1, 1)
    assert s.net_pnl.base == pytest.approx(-102.0), "the money never moved"


def test_expirations_on_one_day_are_not_one_strategy_on_the_calendar(conn):
    """The same two expirations as above, seen through the events the Calendar's
    day detail reads. They were plain `strategy_groups`, which clustered orders
    without knowing which ones IBKR generated, so the two unrelated expirations
    were drawn as one 2-leg strategy after the scoreboard had kept them apart."""
    from optjournal.history import build_history
    from optjournal.serialize import orders_data
    from optjournal.stats import campaigns_for
    from optjournal.strategies import campaign_events

    _leg(conn, conid="1", order_id="1001", at="2026-09-01 10:00:00", qty=-1,
         proceeds=200.0, pnl=None, put_call="P")
    _leg(conn, conid="2", order_id="1002", at="2026-09-20 11:00:00", qty=1,
         proceeds=-300.0, pnl=None, put_call="C")
    _leg(conn, conid="1", order_id="9001", at="2026-10-16 16:20:00", qty=1,
         proceeds=0.0, pnl=199.0, put_call="P", notes="Ep")
    _leg(conn, conid="2", order_id="9002", at="2026-10-16 16:20:00", qty=-1,
         proceeds=0.0, pnl=-301.0, put_call="C", notes="Ep")
    episodes = build_history(conn, asset_category="OPT").episodes
    events = campaign_events(orders_data(conn), campaigns_for(conn, "OPT", episodes))
    assert sorted(e["order_ids"] for e in events) == [
        ["1001"], ["1002"], ["9001"], ["9002"]]


def test_a_spreads_legs_expiring_together_stay_one_event(conn):
    """The other half: a vertical opened as one decision expires as two IBKR
    orders at 16:20:00, and those ARE one event (one campaign), so the day
    detail still names the spread rather than two single legs."""
    from optjournal.history import build_history
    from optjournal.serialize import orders_data
    from optjournal.stats import campaigns_for
    from optjournal.strategies import campaign_events

    _leg(conn, conid="1", order_id="1001", at="2026-09-01 10:00:00", qty=-1,
         proceeds=200.0, pnl=None, put_call="P")
    _leg(conn, conid="2", order_id="1002", at="2026-09-01 10:00:01", qty=1,
         proceeds=-80.0, pnl=None, put_call="P")
    _leg(conn, conid="1", order_id="9001", at="2026-10-16 16:20:00", qty=1,
         proceeds=0.0, pnl=199.0, put_call="P", notes="Ep")
    _leg(conn, conid="2", order_id="9002", at="2026-10-16 16:20:00", qty=-1,
         proceeds=0.0, pnl=-81.0, put_call="P", notes="Ep")
    episodes = build_history(conn, asset_category="OPT").episodes
    events = campaign_events(orders_data(conn), campaigns_for(conn, "OPT", episodes))
    assert sorted(e["order_ids"] for e in events) == [
        ["1001", "1002"], ["9001", "9002"]]


def test_a_reversal_order_is_drawn_in_both_positions_it_filled(conn):
    """Long 2, SELL 3 in one `C;O` fill, buy 1 back: the fill closes the long and
    opens a short, so its order belongs to BOTH campaigns.

    The order-to-campaign map kept one index per order, so the last campaign won
    and the order was drawn in a single card. The short's card then opened on the
    day it was closed, labelled its opening event a close, and read -100 of
    proceeds where the position took 150 in and paid 100 back; the long's card
    took the whole 450 of a sale only two thirds of which was its own. Each card
    now carries its own half of the leg: the quantity it took, that half's own
    open/close marker, and the money divided the way `history._through_zero`
    divides it -- IBKR reports all the realised P&L on the closing half."""
    from optjournal.history import build_history
    from optjournal.serialize import orders_data
    from optjournal.stats import campaigns_for
    from optjournal.strategies import position_groups

    _leg(conn, conid="1", order_id="1001", at="2026-09-10 10:00:00", qty=2,
         proceeds=-200.0, pnl=None, open_close="O")
    _leg(conn, conid="1", order_id="1002", at="2026-09-15 10:00:00", qty=-3,
         proceeds=450.0, pnl=95.0, open_close="C;O")
    _leg(conn, conid="1", order_id="1003", at="2026-09-20 10:00:00", qty=1,
         proceeds=-100.0, pnl=40.0, open_close="C")
    episodes = build_history(conn, asset_category="OPT").episodes
    cards = position_groups(
        orders_data(conn), episodes=episodes,
        campaign_list=campaigns_for(conn, "OPT", episodes),
    )
    got = {
        card["opened_at"]: (
            card["label"],
            [event["order_ids"] for event in card["events"]],
            round(card["proceeds"]["base"], 6),
            card["realized_pnl"]["base"],
        )
        for card in cards
    }
    assert got == {
        "2026-09-10 10:00:00": ("Long put", [["1001"], ["1002"]], 100.0, 95.0),
        "2026-09-15 10:00:00": ("Short put", [["1002"], ["1003"]], 50.0, 40.0),
    }
    # The shared leg, as each card shows it: 2 of the 3 closing the long and the
    # remaining 1 opening the short, each with two thirds and one third of the
    # money it moved.
    shared = {
        card["opened_at"]: [
            (leg["quantity"], leg["open_close"], round(leg["money"]["proceeds"]["base"], 6),
             round(leg["money"]["commission"]["base"], 6),
             leg["money"]["realized_pnl"]["base"])
            for event in card["events"] if event["order_ids"] == ["1002"]
            for order in event["orders"] for leg in order["legs"]
        ]
        for card in cards
    }
    assert shared == {
        "2026-09-10 10:00:00": [(-2, "C", 300.0, round(-2 / 3, 6), 95.0)],
        "2026-09-15 10:00:00": [(-1, "O", 150.0, round(-1 / 3, 6), 0.0)],
    }


#: Two positions meeting inside one order, or inside one 90-second window, each
#: as `(conid, order, at, qty, proceeds, pnl, open_close[, put_call])` fills.
#: The first three are the shapes the reversal fix was written for, which must
#: keep summing; the rest are the ones it broke. B, C and F divide one order's
#: fills between two positions without a single split fill; G and H are two
#: ORDERS on one contract inside the window, one per position.
_MEETINGS = {
    "A: one C;O fill": [
        ("1", "1001", "2026-09-10 10:00:00", 2, -200.0, None, "O"),
        ("1", "1002", "2026-09-15 10:00:00", -3, 450.0, 95.0, "C;O"),
        ("1", "1003", "2026-09-20 10:00:00", 1, -100.0, 40.0, "C"),
    ],
    "D: one leg of a two-leg order reverses": [
        ("1", "1001", "2026-09-10 10:00:00", 2, -200.0, None, "O"),
        ("1", "1002", "2026-09-15 10:00:00", -3, 450.0, 95.0, "C;O"),
        ("2", "1002", "2026-09-15 10:00:00", 1, -80.0, None, "O", "C"),
        ("1", "1003", "2026-09-20 10:00:00", 1, -100.0, 40.0, "C"),
    ],
    "E: flipped twice": [
        ("1", "1001", "2026-09-10 10:00:00", 2, -200.0, None, "O"),
        ("1", "1002", "2026-09-15 10:00:00", -3, 450.0, 95.0, "C;O"),
        ("1", "1003", "2026-09-20 10:00:00", 3, -300.0, 40.0, "C;O"),
        ("1", "1004", "2026-09-25 10:00:00", -2, 260.0, 50.0, "C"),
    ],
    "B: one order filled C, then C;O": [
        ("1", "1001", "2026-09-10 10:00:00", 2, -200.0, None, "O"),
        ("1", "1002", "2026-09-15 10:00:00", -1, 150.0, 48.0, "C"),
        ("1", "1002", "2026-09-15 10:00:01", -2, 300.0, 47.0, "C;O"),
        ("1", "1003", "2026-09-20 10:00:00", 1, -100.0, 40.0, "C"),
    ],
    "C: one order filled C, C, O": [
        ("1", "1001", "2026-09-10 10:00:00", 2, -200.0, None, "O"),
        ("1", "1002", "2026-09-15 10:00:00", -1, 150.0, 48.0, "C"),
        ("1", "1002", "2026-09-15 10:00:01", -1, 150.0, 47.0, "C"),
        ("1", "1002", "2026-09-15 10:00:02", -1, 150.0, None, "O"),
        ("1", "1003", "2026-09-20 10:00:00", 1, -100.0, 40.0, "C"),
    ],
    "F: a close-only run, then the same order opens": [
        ("1", "1002", "2026-09-15 10:00:00", 2, -150.0, 48.0, "C"),
        ("1", "1002", "2026-09-15 10:00:01", 1, -75.0, None, "O"),
        ("1", "1003", "2026-09-20 10:00:00", -1, 100.0, 25.0, "C"),
    ],
    "G: closed, then re-opened by a second order 30s later": [
        ("1", "1001", "2026-09-10 10:00:00", 1, -200.0, None, "O"),
        ("1", "1002", "2026-09-15 10:00:00", -1, 250.0, 48.0, "C"),
        ("1", "1003", "2026-09-15 10:00:30", 1, -255.0, None, "O"),
        ("1", "1004", "2026-09-20 10:00:00", -1, 300.0, 43.0, "C"),
    ],
    "H: flipped through two orders 20s apart": [
        ("1", "1001", "2026-09-10 10:00:00", 1, -200.0, None, "O"),
        ("1", "1002", "2026-09-15 10:00:00", -1, 250.0, 48.0, "C"),
        ("1", "1003", "2026-09-15 10:00:20", -1, 250.0, None, "O"),
        ("1", "1004", "2026-09-20 10:00:00", 1, -100.0, 148.0, "C"),
    ],
}

#: What a leg carries that the page adds up, native and base.
_LEG_SUMS = ("quantity", "proceeds", "proceeds_base", "commission",
             "commission_base", "realized_pnl", "realized_pnl_base")


def _meet(conn, name: str):
    """Ingest one `_MEETINGS` case and read it the way `/api/state` does."""
    return _ingest(conn, _MEETINGS[name])


def _ingest(conn, fills):
    """Ingest fills shaped as `_MEETINGS` and read them the way `/api/state` does."""
    from optjournal.history import build_history
    from optjournal.serialize import orders_data
    from optjournal.stats import campaigns_for
    from optjournal.strategies import campaign_events, position_groups

    for conid, order_id, at, qty, proceeds, pnl, open_close, *right in fills:
        _leg(conn, conid=conid, order_id=order_id, at=at, qty=qty,
             proceeds=proceeds, pnl=pnl, open_close=open_close,
             put_call=right[0] if right else "P")
    report = build_history(conn, asset_category="OPT")
    camps = campaigns_for(conn, "OPT", report.episodes)
    orders = orders_data(conn)
    cards = position_groups(orders, episodes=report.episodes, campaign_list=camps)
    return report, camps, orders, cards, campaign_events(orders, camps)


def _sums(pairs) -> dict[tuple[str, str], tuple[float, ...]]:
    """`_LEG_SUMS` per (order, contract), over `(order, leg)` pairs."""
    out: dict[tuple[str, str], list[float]] = {}
    for order, leg in pairs:
        got = out.setdefault((order["ib_order_id"], leg["conid"]), [0.0] * len(_LEG_SUMS))
        for i, name in enumerate(_LEG_SUMS):
            got[i] += leg[name] or 0.0
    return {k: tuple(round(v, 6) for v in vals) for k, vals in out.items()}


@pytest.mark.parametrize("name", list(_MEETINGS))
def test_every_fill_is_drawn_once_across_the_cards_and_once_on_the_calendar(conn, name):
    """Where two positions meet inside one order, or inside one 90-second window,
    the cards between them draw each fill exactly once, and the Calendar does too.

    An order joined every campaign whose `order_ids` listed it, and those list
    every order of the window's group, so two orders on one contract 30 seconds
    apart were each drawn whole in BOTH cards (G, H); and only a single `C;O` fill
    was ever divided, so one order filled C, C, O drew its whole leg in both (C),
    and one filled C then C;O gave the long only the split fill's half (B)."""
    from optjournal.stats import month_stats

    report, camps, orders, cards, events = _meet(conn, name)
    whole = _sums((o, lg) for o in orders for lg in o["legs"])
    drawn = [(o, lg) for card in cards for ev in card["events"]
             for o in ev["orders"] for lg in o["legs"]]
    assert _sums(drawn) == whole, "a fill drawn twice across the cards, or not at all"
    assert _sums((o, lg) for ev in events for o in ev["orders"]
                 for lg in o["legs"]) == whole
    # Once on the Calendar means one row per leg, not a row per half.
    assert sorted((o["ib_order_id"], lg["conid"]) for ev in events
                  for o in ev["orders"] for lg in o["legs"]) == sorted(whole)
    # Each drawn part reads its own money, not the whole leg's.
    for _order, leg in drawn:
        for field in ("proceeds", "commission", "realized_pnl"):
            assert leg["money"][field]["base"] == pytest.approx(leg[f"{field}_base"] or 0.0)
            assert (leg["money"][field]["native"] or 0.0) == pytest.approx(leg[field] or 0.0)
    # A card counts every execution it draws, a split one in each card drawing a
    # half. The totals across cards count each once: the Dashboard's fills, and
    # the Calendar's, which lists an execution whole.
    took = [len({t for i in c.episode_indices for t in report.episodes[i].trade_ids})
            for c in camps]
    assert sorted(card["fills"] for card in cards) == sorted(took)
    executions = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    assert sum(ev["fills"] for ev in events) == executions
    stats = month_stats(conn, None, asset_category="OPT", report=report)
    assert stats.total_trades == executions
    # The Dashboard scores closes, recounted here straight from the fills.
    closes = [base for (base,) in conn.execute(
        "SELECT SUM(COALESCE(fifo_pnl_realized_base, 0)) FROM trades"
        " WHERE open_close LIKE '%C%'"
        " GROUP BY broker, account_id, conid, ib_order_id, trade_date")]
    assert (stats.closes, stats.wins, stats.losses) == (
        len(closes), sum(c > 0 for c in closes), sum(c < 0 for c in closes))
    # And the money is what the cards show: each card carries what IBKR booked
    # on its fills, whether or not the position is still running.
    shown = [card["realized_pnl"]["base"] for card in cards if card["realized_pnl"]]
    assert stats.net_pnl.base == pytest.approx(sum(shown))


def _cards_read(cards) -> dict[str, tuple]:
    """Each card as a reader sees its header: shape, events, proceeds, outcome, fills."""
    return {
        card["opened_at"]: (
            card["label"],
            [event["order_ids"] for event in card["events"]],
            round(card["proceeds"]["base"], 6),
            card["realized_pnl"] and card["realized_pnl"]["base"],
            card["fills"],
        )
        for card in cards
    }


def test_a_re_entry_order_inside_the_window_is_drawn_in_its_own_card_only(conn):
    """G: sell the long, buy it back 30 seconds later under a second order. The
    two orders are one window group, so each campaign listed both, and each card
    drew both: the re-entry card was labelled "Roll" and read 295 of proceeds
    for a position that paid 255 and took 300 back."""
    *_, cards, _events = _meet(conn, "G: closed, then re-opened by a second order 30s later")
    assert _cards_read(cards) == {
        "2026-09-10 10:00:00": ("Long put", [["1001"], ["1002"]], 50.0, 48.0, 2),
        "2026-09-15 10:00:30": ("Long put", [["1003"], ["1004"]], 45.0, 43.0, 2),
    }


def test_a_flip_through_two_orders_draws_each_order_in_the_card_it_filled(conn):
    """H: close the long, open a short 20 seconds later under another order."""
    *_, cards, _events = _meet(conn, "H: flipped through two orders 20s apart")
    assert _cards_read(cards) == {
        "2026-09-10 10:00:00": ("Long put", [["1001"], ["1002"]], 50.0, 48.0, 2),
        "2026-09-15 10:00:20": ("Short put", [["1003"], ["1004"]], 150.0, 148.0, 2),
    }


def test_one_order_filled_c_c_o_divides_along_its_fills(conn):
    """C: no fill is `C;O`, so nothing was divided and both cards drew the whole
    -3 for 450. The long took the two closing fills and the short the opening
    one, each card with the money of its own fills and its own fill count."""
    *_, cards, _events = _meet(conn, "C: one order filled C, C, O")
    assert _cards_read(cards) == {
        "2026-09-10 10:00:00": ("Long put", [["1001"], ["1002"]], 100.0, 95.0, 3),
        "2026-09-15 10:00:02": ("Short put", [["1002"], ["1003"]], 50.0, 40.0, 2),
    }
    parts = {card["opened_at"]: [
        (leg["quantity"], leg["open_close"], leg["proceeds"], leg["realized_pnl"],
         leg["fills"])
        for event in card["events"] if event["order_ids"] == ["1002"]
        for order in event["orders"] for leg in order["legs"]] for card in cards}
    assert parts == {
        "2026-09-10 10:00:00": [(-2, "C", 300.0, 95.0, 2)],
        "2026-09-15 10:00:02": [(-1, "O", 150.0, 0.0, 1)],
    }


def test_one_order_filled_c_then_c_o_gives_the_long_both_its_closes(conn):
    """B: the closing card got only the split fill's half (-1, 150), dropping the
    bare C fill before it (-1, 150, +48 realised) from every card."""
    *_, cards, _events = _meet(conn, "B: one order filled C, then C;O")
    assert _cards_read(cards) == {
        "2026-09-10 10:00:00": ("Long put", [["1001"], ["1002"]], 100.0, 95.0, 3),
        "2026-09-15 10:00:01": ("Short put", [["1002"], ["1003"]], 50.0, 40.0, 2),
    }


def test_a_close_only_run_and_the_opening_fill_after_it_divide_their_order(conn):
    """F: a short from before the archive bought back (+2, C) and a long opened
    (+1, O) in the same order. Both cards drew the whole +3 for -225."""
    *_, cards, _events = _meet(conn, "F: a close-only run, then the same order opens")
    assert _cards_read(cards) == {
        "2026-09-15 10:00:00": ("Short put close", [["1002"]], -150.0, 48.0, 1),
        "2026-09-15 10:00:01": ("Long put", [["1002"], ["1003"]], 25.0, 25.0, 2),
    }


def test_a_split_execution_is_one_row_on_the_calendar_and_counted_in_each_card(conn):
    """A: one `C;O` execution divided between the long and the short. The
    Calendar's day detail drew its two halves as two rows ("2 fill(s)"); it lists
    the day's executions, so it shows it whole. Each card counts it, since each
    draws a half of it."""
    *_, cards, events = _meet(conn, "A: one C;O fill")
    assert {card["opened_at"]: card["fills"] for card in cards} == {
        "2026-09-10 10:00:00": 2, "2026-09-15 10:00:00": 2}
    day = [(o["ib_order_id"], lg["quantity"], lg["proceeds"]) for ev in events
           for o in ev["orders"] for lg in o["legs"]
           if lg["first_fill_at"].startswith("2026-09-15")]
    assert day == [("1002", -3, 450.0)]


def test_a_position_opened_by_the_far_half_of_a_split_counts_that_fill(conn):
    """Long 2, then SELL 3 as `C;O`, and the short is still open. Counted only
    where it closed, the execution left the short's card reading "0 fill(s)" over
    the STO it drew."""
    from optjournal.history import build_history
    from optjournal.serialize import orders_data
    from optjournal.stats import campaigns_for
    from optjournal.strategies import position_groups

    _leg(conn, conid="1", order_id="1001", at="2026-09-10 10:00:00", qty=2,
         proceeds=-200.0, pnl=None, open_close="O")
    _leg(conn, conid="1", order_id="1002", at="2026-09-15 10:00:00", qty=-3,
         proceeds=450.0, pnl=95.0, open_close="C;O")
    episodes = build_history(conn, asset_category="OPT").episodes
    cards = position_groups(orders_data(conn), episodes=episodes,
                            campaign_list=campaigns_for(conn, "OPT", episodes))
    assert {(card["label"], card["status"]): card["fills"] for card in cards} == {
        ("Long put", "closed"): 2, ("Short put", "open"): 1}


#: A short put rolled out and then partly bought back, as `_MEETINGS` fills. The
#: near contract closes in two fills three seconds apart, the real GOOG roll's
#: shape, and one of the two far puts the roll sold is bought back two days on.
_ROLLED = [
    ("1", "1001", "2026-09-24 10:00:00", -2, 600.0, None, "O"),
    ("1", "1002", "2026-09-28 14:16:20", 1, -100.0, 100.0, "C"),
    ("2", "1002", "2026-09-28 14:16:21", -2, 500.0, None, "O"),
    ("1", "1002", "2026-09-28 14:16:23", 1, -100.0, 95.0, "C"),
    ("2", "1003", "2026-09-30 11:00:00", 1, -150.0, 90.0, "C"),
]


def _realised(money) -> float | None:
    return None if money is None else round(money["base"], 6)


def _shown(event) -> float | None:
    """An event's realised figure as its card header shows it: only where the
    event holds a closing leg."""
    closes = any("C" in str(leg["open_close"] or "").upper()
                 for order in event["orders"] for leg in order["legs"])
    return _realised(event["realized_pnl"]) if closes else None


def test_a_partial_close_counts_on_its_card_its_event_and_its_day(conn):
    """IBKR booked +90 on buying back one of the two far puts, and the open card,
    the event holding that fill and its Calendar day all carry it beside the
    roll's +195. So does the scoreboard. The roll's two fills closing the near
    contract are one close, the buyback of one far put is another, and both won."""
    from optjournal.stats import month_stats

    report, _camps, _orders, (card,), _events = _ingest(conn, _ROLLED)
    assert card["status"] == "open"
    assert _realised(card["realized_pnl"]) == 285.0
    assert [(e["label"], _shown(e)) for e in card["events"]] == [
        ("Short put", None), ("Roll", 195.0), ("Short put close", 90.0)]
    assert "commission" not in card
    september = month_stats(conn, "2026-09", report=report)
    assert {d.day: _realised(d.realized.payload()) for d in september.days} == {
        "2026-09-24": 0.0, "2026-09-28": 195.0, "2026-09-30": 90.0}
    assert _realised(september.net_pnl.payload()) == 285.0
    assert (september.closes, september.wins, september.losses) == (2, 2, 0)
    assert _realised(september.open_premium.payload()) == 260.0, (
        "the far contract's 350 of premium less the 90 already in net P&L")


def test_a_close_that_nets_zero_is_neither_a_win_nor_a_loss(conn):
    from optjournal.stats import month_stats

    report, *_ = _ingest(conn, [
        ("1", "1001", "2026-09-24 10:00:00", -1, 300.0, None, "O"),
        ("1", "1002", "2026-09-25 10:00:00", 1, -300.0, 0.0, "C"),
    ])
    s = month_stats(conn, "2026-09", report=report)
    assert (s.closes, s.wins, s.losses) == (1, 0, 0)


def test_a_decided_cards_realised_is_its_campaigns(conn):
    """Once every contract is closed the card's figure is `Campaign.realized`, the
    one the strategy ranking reads, to the last key. The scoreboard scores the far
    contract's last buyback, +80, as October's one close. The card is a position,
    and a close is what IBKR booked on one order's fills."""
    from optjournal.stats import month_stats

    report, (camp,), _orders, (card,), _events = _ingest(conn, _ROLLED + [
        ("2", "1004", "2026-10-02 11:00:00", 1, -150.0, 80.0, "C")])
    assert card["status"] == "closed"
    assert card["realized_pnl"] == camp.realized.payload()
    october = month_stats(conn, "2026-10", report=report)
    assert _realised(october.net_pnl.payload()) == 80.0
    assert (october.closes, october.wins, october.losses) == (1, 1, 0)
    assert _realised(october.largest_win.payload()) == 80.0


def _at_broker(conn, broker: str, fills) -> None:
    """Fills `(trade, account, order, at, qty, open_close, realised)` on one SPY
    put at `broker`."""
    for tid, account, oid, at, qty, open_close, pnl in fills:
        conn.execute(
            "INSERT INTO trades (broker, trade_id, ib_exec_id, transaction_id,"
            " ib_order_id, account_id, trade_date, date_time, asset_category,"
            " symbol, conid, underlying_symbol, put_call, strike, expiry,"
            " multiplier, buy_sell, open_close, quantity, trade_price, currency,"
            " fx_rate_to_base, proceeds, proceeds_base, ib_commission,"
            " ib_commission_base, fifo_pnl_realized, fifo_pnl_realized_base,"
            " raw, source_file, first_seen_at)"
            " VALUES (?,?,?,?,?,?,?,?,'OPT','SPY P','1','SPY','P',500,'2026-12-18',"
            "100,?,?,?,1.0,'USD',1.0,?,?,-1.0,-1.0,?,?,'{}','t.xml','now')",
            (broker, tid, tid, tid, oid, account, at[:10], at,
             "SELL" if qty < 0 else "BUY", open_close, qty, -qty * 100.0,
             -qty * 100.0, pnl, pnl))
    conn.commit()


def _drawn_by_broker(conn):
    """The cards and the Calendar, each leg as `(broker, order, quantity)`."""
    from optjournal.history import build_history
    from optjournal.serialize import orders_data
    from optjournal.stats import campaigns_for
    from optjournal.strategies import campaign_events, position_groups

    episodes = build_history(conn, asset_category="OPT").episodes
    camps = campaigns_for(conn, "OPT", episodes)
    orders = orders_data(conn)
    cards = position_groups(orders, episodes=episodes, campaign_list=camps)

    def legs(events):
        return sorted((o["broker"], o["ib_order_id"], lg["broker"], lg["quantity"])
                      for ev in events for o in ev["orders"] for lg in o["legs"])

    return (orders, sorted((legs(c["events"]), c["fills"], c["anchor"]) for c in cards),
            legs(campaign_events(orders, camps)))


def test_the_same_order_id_at_two_brokers_is_two_orders(conn):
    """Order ids are each broker's own, so 5000 at IBKR and 5000 at another broker
    are two placements. Each order row took both brokers' legs, since the legs
    were read by order id alone, and the cards and the Calendar then drew each
    broker's 5000 in both positions: 1 and 2 contracts, twice over, in each. The
    later one's card answers to its own first fill, since the page posts an
    anchor alone and the two would otherwise be one journal entry."""
    _at_broker(conn, "ibkr", [
        ("a1", "U1", "5000", "2026-09-01 10:00:00", 1, "O", None),
        ("a2", "U1", "5001", "2026-09-03 10:00:00", -1, "C", 10.0)])
    _at_broker(conn, "b2", [
        ("b1", "X9", "5000", "2026-09-02 10:00:00", 2, "O", None),
        ("b2", "X9", "6001", "2026-09-04 10:00:00", -2, "C", 20.0)])
    orders, cards, calendar = _drawn_by_broker(conn)
    assert sorted((o["broker"], o["ib_order_id"], [lg["broker"] for lg in o["legs"]],
                   o["fills"]) for o in orders) == [
        ("b2", "5000", ["b2"], 1), ("b2", "6001", ["b2"], 1),
        ("ibkr", "5000", ["ibkr"], 1), ("ibkr", "5001", ["ibkr"], 1)]
    assert cards == [
        ([("b2", "5000", "b2", 2), ("b2", "6001", "b2", -2)], 2, "t:b1"),
        ([("ibkr", "5000", "ibkr", 1), ("ibkr", "5001", "ibkr", -1)], 2, "5000")]
    assert calendar == [("b2", "5000", "b2", 2), ("b2", "6001", "b2", -2),
                        ("ibkr", "5000", "ibkr", 1), ("ibkr", "5001", "ibkr", -1)]


def test_the_same_order_id_at_two_brokers_joins_no_positions(conn):
    """The window groups orders by id, and 5000 at both brokers read as one order
    touching two contracts, which is what a spread looks like: the two brokers'
    positions were one decision, one card and one outcome on the scoreboard. A
    decision is placed at one broker, so a group joins only one broker's."""
    from optjournal.history import build_history
    from optjournal.stats import campaigns_for

    _at_broker(conn, "ibkr", [
        ("a1", "U1", "5000", "2026-09-01 10:00:00", 1, "O", None),
        ("a2", "U1", "5001", "2026-09-03 10:00:00", -1, "C", 10.0)])
    _at_broker(conn, "b2", [
        ("b1", "X9", "5000", "2026-09-02 10:00:00", 2, "O", None),
        ("b2", "X9", "6001", "2026-09-04 10:00:00", -2, "C", 20.0)])
    conn.execute("UPDATE trades SET conid = '2' WHERE broker = 'b2'")
    conn.commit()
    episodes = build_history(conn, asset_category="OPT").episodes
    camps = campaigns_for(conn, "OPT", episodes)
    assert sorted(sorted(c.orders) for c in camps) == [
        [("b2", "5000"), ("b2", "6001")], [("ibkr", "5000"), ("ibkr", "5001")]]
    assert sorted(c.realized.base for c in camps) == [10.0, 20.0]


def test_one_journal_at_two_brokers_is_two_sets_of_cards(conn):
    """The same statement read for two brokers: every id the same, trade ids
    included. Each broker's positions are drawn once each, from its own fills.
    The page posts an anchor alone, so one broker's card answers to the order
    and the other's to its own first fill, and the note filed under the order
    at the first broker stays on that broker's card."""
    from optjournal import journal
    from optjournal.serialize import journal_data

    fills = [("t1", "U1", "100", "2026-09-01 10:00:00", 1, "O", None),
             ("t2", "U1", "100", "2026-09-01 10:00:05", 1, "O", None),
             ("t3", "U1", "101", "2026-09-03 10:00:00", -2, "C", 10.0)]
    _at_broker(conn, "ibkr", fills)
    _at_broker(conn, "b2", fills)
    journal.save(conn, "100", account_id="U1", broker="b2", values={"entry_note": "b2"})
    _orders, cards, calendar = _drawn_by_broker(conn)
    assert cards == [
        ([("b2", "100", "b2", 2), ("b2", "101", "b2", -2)], 3, "100"),
        ([("ibkr", "100", "ibkr", 2), ("ibkr", "101", "ibkr", -2)], 3, "t:t1")]
    assert calendar == [("b2", "100", "b2", 2), ("b2", "101", "b2", -2),
                        ("ibkr", "100", "ibkr", 2), ("ibkr", "101", "ibkr", -2)]
    assert journal_data(conn)["orphans"] == []
    assert journal_data(conn)["entries"]["100"]["entry_note"] == "b2"


def test_a_split_execution_is_listed_under_the_position_it_closed(conn):
    """Long 2 (order 1000), then order 1001 sells 3 as `C;O` and 1 more 20 seconds
    later, which adds to the short. The Calendar lists 1001 whole, under the
    position that took its first execution; the split one is taken by both, and
    the tie goes to the long it closed. It went to the short, because the tie was
    read off the fill count of the whole share and the short's later fill made
    that count match the long's, so the day showed "Long put" and 1001 as two
    unrelated events where they were one placement."""
    from optjournal.history import build_history
    from optjournal.serialize import orders_data
    from optjournal.stats import campaigns_for
    from optjournal.strategies import campaign_events

    _leg(conn, conid="1", order_id="1000", at="2026-09-15 09:59:40", qty=2,
         proceeds=-200.0, pnl=None, open_close="O")
    _leg(conn, conid="1", order_id="1001", at="2026-09-15 10:00:00", qty=-3,
         proceeds=300.0, pnl=95.0, open_close="C;O")
    _leg(conn, conid="1", order_id="1001", at="2026-09-15 10:00:20", qty=-1,
         proceeds=100.0, pnl=None, open_close="O")
    _leg(conn, conid="1", order_id="1002", at="2026-09-20 10:00:00", qty=2,
         proceeds=-100.0, pnl=40.0, open_close="C")
    episodes = build_history(conn, asset_category="OPT").episodes
    events = campaign_events(orders_data(conn), campaigns_for(conn, "OPT", episodes))
    assert sorted(e["order_ids"] for e in events) == [["1000", "1001"], ["1002"]]


def test_under_the_0dte_scope_a_rolled_leg_is_decided_the_day_it_closes(conn):
    """`pnl/s_scope_inflight.py`: a 0DTE short put rolled at 15:55 into the next
    day's put, which is still open.

    The 0DTE leg is a closed round trip, so its -302 is a decided loss on the day
    it lands in Net P&L. The far leg is not 0DTE, so closing it moves nothing
    under the scope.
    """
    from optjournal.stats import odte_scope

    _leg(conn, conid="1", order_id="1", at="2026-09-10 10:00:00", qty=-1,
         proceeds=200.0, pnl=None, expiry="2026-09-10")
    _leg(conn, conid="1", order_id="2", at="2026-09-10 15:55:00", qty=1,
         proceeds=-500.0, pnl=-302.0, expiry="2026-09-10")
    _leg(conn, conid="2", order_id="3", at="2026-09-10 15:55:00", qty=-1,
         proceeds=600.0, pnl=None, expiry="2026-09-11")
    scope = odte_scope(conn)
    s = month_stats(conn, "2026-09", scope=scope)
    assert s.net_pnl.base == -302.0
    assert (s.closes, s.losses) == (1, 1)
    assert s.avg_pnl.base == -302.0

    _leg(conn, conid="2", order_id="4", at="2026-09-11 15:00:00", qty=1,
         proceeds=-100.0, pnl=498.0, expiry="2026-09-11")
    s = month_stats(conn, "2026-09", scope=odte_scope(conn))
    assert s.net_pnl.base == -302.0
    assert (s.closes, s.losses) == (1, 1)
    assert s.avg_pnl.base == -302.0


def test_a_stock_outcome_lands_in_the_month_its_pnl_does(conn):
    """`pnl/s_two_clocks.py`: a Korean stock sold at 20:03 ET on 31 August, which
    is 1 September in Seoul, so IBKR's trade date is the 1st.

    Stock P&L follows IBKR's per-fill realisation on the trade date, the month
    the statement books it in, while the outcome followed the fill's ET stamp: the
    win landed in August with no P&L and the P&L in September with no win. The
    outcome now takes the closing fill's trade date, the same clock as its money.
    """
    for tid, at, day, oc, qty, pnl in (
        ("k1", "2026-08-10 21:00:00", "2026-08-11", "O", 10, 0.0),
        ("k2", "2026-08-31 20:03:00", "2026-09-01", "C", -10, 11.88),
    ):
        conn.execute(
            "INSERT INTO trades (trade_id, ib_exec_id, transaction_id, ib_order_id,"
            " account_id, trade_date, date_time, asset_category, symbol, conid,"
            " underlying_symbol, open_close, quantity, trade_price, currency,"
            " fx_rate_to_base, fifo_pnl_realized, fifo_pnl_realized_base, raw,"
            " source_file, first_seen_at) VALUES (?,?,?,?,'U1',?,?,'STK',"
            " '322310.KQ','K1','322310.KQ',?,?,10000,'KRW',0.0006,?,?,'{}','t.xml','now')",
            (tid, tid, tid, tid, day, at, oc, qty, pnl / 0.0006, pnl),
        )
    august, september = (month_stats(conn, m, asset_category="STK")
                         for m in ("2026-08", "2026-09"))
    assert (august.net_pnl.base, august.closes, august.wins) == (0.0, 0, 0)
    assert (september.net_pnl.base, september.closes,
            september.wins) == (11.88, 1, 1)


def test_net_liquidation_is_every_accounts_newest_summary_summed(conn):
    """Two accounts' NAV, one a day behind. The panel read one row, so the gain
    as a share of net liquidation was measured against one account's value."""
    for day, account, total in (("20260923", "U2", 220.0), ("20260924", "U1", 510.0),
                                ("20260922", "U1", 400.0)):
        conn.execute(
            "INSERT INTO equity_summaries (report_date, account_id, currency,"
            " cash_base, total_base, raw, source_file, ingested_at)"
            " VALUES (?, ?, 'EUR', 0, ?, '{}', 't.xml', 'now')", (day, account, total))
    s = month_stats(conn, None)
    assert (s.net_liq_base, s.net_liq_date) == (730.0, "2026-09-24")
    # A period ending before every summary has none, rather than a zero.
    assert month_stats(conn, "2026-08").net_liq_base is None
