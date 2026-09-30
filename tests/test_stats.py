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
    CONTRACT_SCORING,
    POSITION_SCORING,
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
    # no decided unit, no average outcome; nothing lost, no profit factor. An
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
    """A strangle sold and bought back: one decision, two contracts, split
    outcomes.

    Two conids on separate order ids filled in the SAME SECOND, which is how
    every real multi-leg event in this journal arrives -- see `campaigns.py`. The
    put wins 400 and the call loses 100, so the position nets +300 while its legs
    disagree, and that is exactly the case the two scorings read differently.
    """
    _leg(conn, conid="1", order_id="10", at="2026-03-02 15:00:00",
         qty=-1, proceeds=500.0, pnl=None, put_call="P")
    _leg(conn, conid="2", order_id="11", at="2026-03-02 15:00:00",
         qty=-1, proceeds=300.0, pnl=None, put_call="C")
    _leg(conn, conid="1", order_id="12", at="2026-03-09 15:00:00",
         qty=1, proceeds=-100.0, pnl=400.0, put_call="P")
    _leg(conn, conid="2", order_id="13", at="2026-03-09 15:00:00",
         qty=1, proceeds=-400.0, pnl=-100.0, put_call="C")


def test_the_two_scorings_divide_the_same_money_into_different_outcomes(conn):
    """The toggle's whole contract, on the case that motivates it.

    A strangle is ONE decision made of two contracts whose outcomes disagree.
    Scored by position it is a single win; scored by contract it is a win and a
    loss on the same trade. Both readings are defensible -- the second is what a
    broker trade log shows -- so the journal offers both, and this pins the
    invariant that makes offering both safe: the MONEY does not move. Net P&L,
    commission and the fill count are identical, and only the number of outcomes
    that cash is divided into changes.

    Measured on the real journal, the same 29 closed round trips read 14W/1L by
    position and 24W/5L by contract.
    """
    _strangle(conn)
    by_position = month_stats(conn, "2026-03", base_currency="EUR")
    by_contract = month_stats(
        conn, "2026-03", base_currency="EUR", scoring=CONTRACT_SCORING
    )

    assert (by_position.wins, by_position.losses) == (1, 0), (
        "a hedge leg cannot be a loss inside a winning position"
    )
    assert (by_contract.wins, by_contract.losses) == (1, 1), (
        "scored per contract, the call leg is its own loss"
    )
    assert by_position.decided_campaigns == 1
    assert by_contract.decided_campaigns == 2

    # The invariant. Anything here moving would mean the toggle had become a
    # second opinion about the account rather than a second way of counting it.
    assert by_position.net_pnl.base == by_contract.net_pnl.base == 300.0
    assert by_position.commissions.base == by_contract.commissions.base
    assert by_position.total_trades == by_contract.total_trades == 4
    assert by_position.closed_episodes == by_contract.closed_episodes == 2


def test_contract_scoring_reports_no_in_flight_cash_because_it_groups_nothing(conn):
    """`inflight_realized` explains a gap that only grouping can open.

    It is the cash settled inside a position still running -- a roll's near leg.
    Under contract scoring every closed round trip is its own finished outcome,
    so the gap cannot exist and the figure is structurally zero. Asserted rather
    than assumed, because a stale non-zero here would feed the Dashboard a note
    claiming "of this figure, X closed inside a position still running" beside a
    scoreboard where no position is still running.
    """
    # One leg closed, its partner still open: a position mid-flight.
    _leg(conn, conid="1", order_id="10", at="2026-03-02 15:00:00",
         qty=-1, proceeds=500.0, pnl=None, put_call="P")
    _leg(conn, conid="2", order_id="11", at="2026-03-02 15:00:00",
         qty=-1, proceeds=300.0, pnl=None, put_call="C")
    _leg(conn, conid="1", order_id="12", at="2026-03-09 15:00:00",
         qty=1, proceeds=-100.0, pnl=400.0, put_call="P")

    by_position = month_stats(conn, "2026-03", base_currency="EUR")
    by_contract = month_stats(
        conn, "2026-03", base_currency="EUR", scoring=CONTRACT_SCORING
    )

    assert by_position.inflight_realized.base == 400.0, (
        "the closed leg's cash sits inside a position that has not finished"
    )
    assert by_position.decided_campaigns == 0, "the position is not decided yet"
    assert by_contract.inflight_realized.base == 0.0
    assert (by_contract.wins, by_contract.decided_campaigns) == (1, 1), (
        "the closed round trip is a finished outcome on its own terms"
    )


@pytest.mark.parametrize("given", ["positon", "", "POSITION", "leg", None])
def test_an_unrecognised_scoring_heals_to_the_default(conn, given):
    """A query string is user input, so an unknown unit must not reach the branch.

    Healing rather than raising, because the value arrives from a URL a reader
    can hand-edit and a 500 on a typo is a worse answer than the default view.
    The healed value is CARRIED on the stats, which is what lets the page label
    the figures with the unit they were actually counted in rather than the one
    that was asked for.

    'POSITION' heals too: the vocabulary is exact, and accepting a case variant
    here would make the page's own comparisons against `SCORINGS` disagree with
    the server about which chip is active.
    """
    _strangle(conn)
    stats = month_stats(conn, "2026-03", base_currency="EUR", scoring=given)
    assert stats.scoring == POSITION_SCORING
    assert (stats.wins, stats.losses) == (1, 0), (
        "an unknown unit must be counted as the default, not as the other one"
    )
    assert stats_data(stats)["scoring"] == POSITION_SCORING


def test_the_scoring_travels_into_the_payload_for_the_page_to_label_with(conn):
    """`stats_data` carries the unit beside the counts it governs.

    The page reads this for its labels, never to recompute: 93% by position and
    83% by contract are the same account, so a payload whose unit the reader has
    to infer from a control's state is one a stale fetch can mislabel.
    """
    _strangle(conn)
    view = stats_data(month_stats(
        conn, "2026-03", base_currency="EUR", scoring=CONTRACT_SCORING
    ))
    assert view["scoring"] == CONTRACT_SCORING
    assert (view["wins"], view["losses"], view["decided_campaigns"]) == (1, 1, 2)


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
    assert (september.net_pnl.base, september.wins, september.decided_campaigns) == (
        198.0, 1, 1)
    assert september.inflight_realized.base == 0.0
    assert (october.net_pnl.base, october.wins, october.decided_campaigns) == (
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
    assert (s.decided_campaigns, s.wins, s.losses) == (2, 1, 1)
    assert s.net_pnl.base == pytest.approx(-102.0), "the money never moved"


def test_under_the_0dte_scope_a_running_roll_is_in_flight_not_decided(conn):
    """`pnl/s_scope_inflight.py`: a 0DTE short put rolled at 15:55 into the next
    day's put, which is still open.

    The scoreboard decided a unit by its IN-SCOPE episodes (only the 0DTE leg,
    closed) while the in-flight figure read the whole campaign (still running),
    so the same -302 was shown as a decided loss AND as cash inside a position
    still running. Both now read the campaign the Trades tab draws: open until
    its last leg closes, and its in-scope cash in flight until then.
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
    assert (s.decided_campaigns, s.losses) == (0, 0)
    assert s.inflight_realized.base == -302.0
    assert s.avg_pnl is None

    # The roll's far leg closes the next day: the decision is now finished, and
    # under the scope its outcome is the in-scope cash, counted once.
    _leg(conn, conid="2", order_id="4", at="2026-09-11 15:00:00", qty=1,
         proceeds=-100.0, pnl=498.0, expiry="2026-09-11")
    s = month_stats(conn, "2026-09", scope=odte_scope(conn))
    assert (s.decided_campaigns, s.losses, s.inflight_realized.base) == (1, 1, 0.0)
    assert s.avg_pnl.base == -302.0
