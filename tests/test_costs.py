"""The DB-backed cost engine: what it attributes, and what it refuses to.

Two halves. The first states costs as literal rows and asserts the arithmetic,
because a report is only as good as its attribution rules and each of those is a
decision worth pinning. The second runs the engine over every archived statement
and reconciles it against `analysis.py`, the statement-reading sibling: the two
read the same broker data by different routes, so a figure they disagree about is
a defect in one of them.
"""

from __future__ import annotations

import sqlite3

import pytest
from conftest import add_statement, connect_migrated

from optjournal.costs import (
    AUTOFX_MARKUP_BPS,
    AUTOFX_MARKUP_MEASURED_BPS,
    CostScope,
    build_costs,
)

# `currency` and `ib_commission_currency` are separate columns on purpose: IBKR
# bills conversion commission in the BASE currency while the row's currency is
# the pair's quote, and a fixture that could not express that could not test it.
_TRADE_SQL = (
    "INSERT INTO trades (trade_id, ib_exec_id, transaction_id, ib_order_id,"
    " account_id, trade_date, date_time, asset_category, symbol, conid, notes,"
    " quantity, trade_price, currency, fx_rate_to_base, proceeds, proceeds_base,"
    " ib_commission, ib_commission_base, ib_commission_currency, taxes,"
    " fifo_pnl_realized, fifo_pnl_realized_base, raw, source_file, first_seen_at)"
    " VALUES (?,?,?,?, 'U1', ?, ?, ?, ?, ?, ?, ?, 1.0, ?, ?, ?, ?, ?, ?, ?, ?,"
    " 0, 0, '{}', 't.xml', 'now')"
)


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    c = connect_migrated(tmp_path / "costs.db")
    add_statement(c, from_date="2025-01-01", to_date="2026-12-31")
    return c


def add_fill(
    conn,
    tid: str,
    *,
    asset: str = "OPT",
    date: str = "2026-03-01",
    symbol: str = "SPY",
    qty: float = 1.0,
    notional: float = 1000.0,
    commission: float = -1.0,
    commission_ccy: str = "USD",
    currency: str = "USD",
    rate: float = 1.0,
    taxes: float = 0.0,
    notes: str | None = None,
    order: str = "",
) -> None:
    """One fill. Amounts are as IBKR states them: a charge is NEGATIVE."""
    conn.execute(
        _TRADE_SQL,
        (tid, f"e{tid}", f"x{tid}", order or f"o{tid}", date, f"{date} 10:00:00",
         asset, symbol, f"c{tid}", notes, qty, currency, rate,
         -notional, -notional * rate, commission, commission * rate,
         commission_ccy, taxes),
    )


def add_cash(
    conn,
    tid: str,
    *,
    kind: str = "Other Fees",
    description: str = "OPRA NP L1",
    symbol: str | None = None,
    amount: float = -1.30,
    currency: str = "EUR",
    rate: float = 1.0,
    date: str = "2026-03-01",
) -> None:
    conn.execute(
        "INSERT INTO cash_transactions (transaction_id, account_id, date_time,"
        " type, description, symbol, amount, currency, fx_rate_to_base,"
        " amount_base, raw, source_file, first_seen_at)"
        " VALUES (?, 'U1', ?, ?, ?, ?, ?, ?, ?, ?, '{}', 't.xml', 'now')",
        (tid, f"{date} 10:00:00", kind, description, symbol, amount, currency,
         rate, amount * rate),
    )


# --- cost is positive --------------------------------------------------------


def test_a_charge_is_reported_as_a_positive_cost(conn):
    """IBKR stores a charge negative; the report presents cost positive.

    Flipped once, at the query, so no breakdown can disagree in sign with the
    total it decomposes.
    """
    add_fill(conn, "1", commission=-2.50)
    r = build_costs(conn)
    assert r.attributable.base == pytest.approx(2.50)
    assert r.attributable.by_ccy == {"USD": pytest.approx(2.50)}


def test_a_commission_credit_is_netted_and_counted(conn):
    """IBKR credits commission when it adjusts a per-order minimum across a
    split order. Netting it off is right; hiding that it happened is not, so the
    fill is counted and the surface can explain a cost that went down."""
    add_fill(conn, "1", commission=-3.00)
    add_fill(conn, "2", commission=+1.00)
    r = build_costs(conn)
    assert r.attributable.base == pytest.approx(2.00)
    assert r.credit_fills == 1


# --- attribution -------------------------------------------------------------


def test_commission_narrows_with_the_scope(conn):
    """Execution commission carries an asset category, so it narrows exactly."""
    add_fill(conn, "1", asset="OPT", commission=-2.00)
    add_fill(conn, "2", asset="STK", commission=-5.00)
    assert build_costs(conn, scope=CostScope.of(["OPT"])).attributable.base == 2.00
    assert build_costs(conn, scope=CostScope.of(["STK"])).attributable.base == 5.00
    both = build_costs(conn, scope=CostScope.of(["OPT", "STK"]))
    assert both.attributable.base == pytest.approx(7.00)


def test_an_empty_scope_means_everything_not_nothing(conn):
    """An untouched filter is 'no narrowing asked for'. Reading it as 'show
    nothing' would make the tab open empty and look broken."""
    add_fill(conn, "1", asset="OPT", commission=-2.00)
    add_fill(conn, "2", asset="STK", commission=-5.00)
    assert CostScope.of(None).is_everything
    assert CostScope.of([]).is_everything
    assert build_costs(conn, scope=CostScope.of([])).attributable.base == 7.00


def test_fees_are_never_narrowed_by_the_scope(conn):
    """The rule that keeps the total honest.

    A market-data subscription carries no asset category -- no fee row in the
    real archive carries a contract or trade id -- so it cannot be attributed,
    and pro-rating it into a fill subset would be inventing a split. It is
    reported whole under EVERY scope, including one it has nothing to do with.
    """
    add_fill(conn, "1", asset="OPT", commission=-2.00)
    add_cash(conn, "f1", amount=-1.30)
    for scope in (None, ["OPT"], ["STK"], ["OPT", "STK", "CASH"]):
        r = build_costs(conn, scope=CostScope.of(scope))
        assert r.unattributable.base == pytest.approx(1.30), scope


def test_a_fee_refund_reduces_the_total_it_reverses(conn):
    """IBKR cancels a charge with a POSITIVE row of the same size.

    The real archive, EUR market data: 1.30 charged for August, the same 1.30
    cancelled (`CANCEL[...]`), 1.29 charged for September. Taking each row's
    magnitude booked the cancellation as a third charge, 3.89 where the account
    paid 1.29.
    """
    add_cash(conn, "f1", amount=-1.30, description="OPRA NP L1 FOR AUG 2026")
    add_cash(conn, "f2", amount=1.30,
             description="CANCEL[OPRA NP L1] FOR AUG 2026")
    add_cash(conn, "f3", amount=-1.29, description="OPRA NP L1 FOR SEP 2026")
    r = build_costs(conn)
    assert r.unattributable.base == pytest.approx(1.29)
    (group,) = r.fees
    assert group.count == 3, "a refund is still a row the reader can see"
    assert dict(group.total.by_ccy) == {"EUR": pytest.approx(1.29)}


def test_a_period_refunded_more_than_it_was_charged_is_a_net_credit(conn):
    """September 2026 on the real account: the August cancellation (+1.30) and
    the September charge (-1.29) both land in it, so the month cost -0.01."""
    add_cash(conn, "f1", amount=-1.30, date="2026-08-04")
    add_cash(conn, "f2", amount=1.30, date="2026-09-02",
             description="CANCEL[OPRA NP L1] FOR AUG 2026")
    add_cash(conn, "f3", amount=-1.29, date="2026-09-02")
    assert build_costs(conn, period="2026-09").unattributable.base == (
        pytest.approx(-0.01))
    assert build_costs(conn, period="2026-08").unattributable.base == (
        pytest.approx(1.30))


def test_fees_are_not_mixed_into_the_attributable_figure(conn):
    """Two properties on the report, because they answer different questions:
    'what did trading this cost' and 'what did the account cost'."""
    add_fill(conn, "1", commission=-2.00)
    add_cash(conn, "f1", amount=-1.30)
    r = build_costs(conn)
    assert r.attributable.base == pytest.approx(2.00)
    assert r.unattributable.base == pytest.approx(1.30)
    assert r.friction.stated.base == pytest.approx(3.30)


# --- currency ----------------------------------------------------------------


def test_commission_is_read_in_the_currency_it_was_billed_in(conn):
    """Not the instrument's currency.

    IBKR bills conversion commission in the base currency while the row's
    currency is the pair's quote -- a real case in this archive, where every
    CASH commission is EUR on SEK, USD and KRW instruments. Reading the
    instrument's column would label a EUR charge as SEK.
    """
    add_fill(conn, "1", asset="CASH", symbol="EUR.SEK", currency="SEK",
             commission=-1.73, commission_ccy="EUR", rate=0.09)
    r = build_costs(conn)
    assert r.attributable.by_ccy == {"EUR": pytest.approx(1.73)}


def test_a_mixed_scope_keeps_every_charge_and_withholds_only_the_single_figure(conn):
    """The reason costs are `Charge` and not `Money`.

    Widening from options to the account should add columns, not delete
    exactness: each charge is still known, and only the claim that one currency
    speaks for the total becomes false.
    """
    add_fill(conn, "1", asset="OPT", commission=-2.00, commission_ccy="USD")
    add_fill(conn, "2", asset="STK", commission=-10.00, commission_ccy="SEK",
             currency="SEK", rate=0.09)
    r = build_costs(conn, scope=CostScope.of(["OPT", "STK"]))
    assert r.attributable.by_ccy == {"USD": pytest.approx(2.00),
                                     "SEK": pytest.approx(10.00)}
    assert not r.attributable.money.is_exact
    # And a single-currency scope still answers exactly.
    opt = build_costs(conn, scope=CostScope.of(["OPT"]))
    assert opt.attributable.money.is_exact
    assert opt.attributable.money.native == pytest.approx(2.00)


def test_taxes_are_read_at_the_instruments_rate(conn):
    """Taxes carry no currency field of their own, so the instrument's is the
    only reading the data supports -- unlike commission, which has its own."""
    add_fill(conn, "1", commission=0.0, taxes=-5.00, currency="SEK", rate=0.10)
    r = build_costs(conn)
    assert r.by_category[0].taxes.base == pytest.approx(0.50)
    assert r.by_category[0].taxes.by_ccy == {"SEK": pytest.approx(5.00)}


# --- per unit ----------------------------------------------------------------


def test_per_unit_divides_both_halves_by_the_same_quantity(conn):
    """So a per-unit figure can never be an as-charged numerator over a restated
    divisor -- the defect `Money.per` exists to prevent, here for a ledger."""
    add_fill(conn, "1", qty=4, commission=-2.00, commission_ccy="USD")
    per = build_costs(conn).by_category[0].per_unit
    assert per.base == pytest.approx(0.50)
    assert per.by_ccy == {"USD": pytest.approx(0.50)}


def test_a_conversion_has_no_per_unit_cost(conn):
    """Its quantity is an amount of money, so a per-unit figure would be
    commission per euro, which is not a rate anyone charges or reads."""
    add_fill(conn, "1", asset="CASH", symbol="EUR.USD", qty=1000, commission=-1.00)
    assert build_costs(conn).by_category[0].per_unit is None


def test_fractional_lots_are_not_truncated(conn):
    """Dividend reinvestment buys fractional shares. Truncating each fill toward
    zero before summing made a thousand of them total nothing."""
    for i in range(4):
        add_fill(conn, str(i), asset="STK", qty=0.25, commission=-1.00)
    cost = build_costs(conn).by_category[0]
    assert cost.quantity == pytest.approx(1.0)
    assert cost.per_unit.base == pytest.approx(4.0)


# --- autofx ------------------------------------------------------------------


def test_the_markup_is_estimated_only_on_flagged_conversions(conn):
    """A manual conversion pays a stated commission; an auto-conversion pays a
    markup embedded in the rate. Charging both to one row double-counts it."""
    add_fill(conn, "auto", asset="CASH", symbol="EUR.USD", notional=10_000,
             commission=0.0, notes="AFx")
    add_fill(conn, "manual", asset="CASH", symbol="EUR.USD", notional=10_000,
             commission=-2.00, commission_ccy="EUR")
    r = build_costs(conn)
    pair = r.fx[0]
    assert pair.auto.notional == pytest.approx(10_000)
    assert pair.manual.notional == pytest.approx(10_000)
    # 3bps of 10,000 -- NOT of the pair's 20,000 turnover.
    assert r.autofx_markup() == pytest.approx(3.00)
    assert pair.manual.commission.base == pytest.approx(2.00)


def test_the_markup_is_read_from_the_stored_note_string(conn):
    """The shape the DATABASE holds, which is where this engine reads from.

    `sources.py` stores `";".join(codes)`, so a conversion that was also a
    partial fill is stored `AFx;P`. The statement path never sees that form --
    py_ibkr pre-splits it -- which is exactly how a whole-field comparison
    survived there while being wrong here.
    """
    add_fill(conn, "1", asset="CASH", symbol="EUR.USD", notional=10_000,
             commission=0.0, notes="AFx;P")
    assert build_costs(conn).autofx_markup() == pytest.approx(3.00)


def test_an_unflagged_conversion_is_not_marked_up(conn):
    add_fill(conn, "1", asset="CASH", symbol="EUR.USD", notional=10_000,
             commission=-2.00, commission_ccy="EUR", notes="A")
    r = build_costs(conn)
    assert r.autofx_notional == 0.0
    assert r.autofx_markup() == 0.0
    assert not r.friction.is_estimated


def test_the_estimate_is_a_range_not_a_point(conn):
    """IBKR publishes 3.0 bps and hedges it with 'typically' and 'at its
    discretion'; a year of real conversions implied 3.2. Around a quarter of
    account friction is this figure, so the band is the honest form."""
    add_fill(conn, "1", asset="CASH", symbol="EUR.USD", notional=10_000,
             commission=0.0, notes="AFx")
    f = build_costs(conn).friction
    assert f.estimated_low == pytest.approx(3.00)
    assert f.estimated_high == pytest.approx(3.20)
    assert f.total_low < f.total_high
    assert f.is_estimated


def test_the_midpoint_sits_inside_the_range_it_summarises(conn):
    """The headline prints one number, and it has to be one the range contains.

    Stated as an ordering rather than against a literal: the property that keeps
    a headline honest is that it cannot fall outside the band shown beside it,
    whatever the two bps constants become.
    """
    add_fill(conn, "1", asset="CASH", symbol="EUR.USD", notional=10_000,
             commission=0.0, notes="AFx")
    f = build_costs(conn).friction
    assert f.estimated_low < f.estimated_mid < f.estimated_high
    assert f.total_low < f.total_mid < f.total_high
    assert f.estimated_mid == pytest.approx(3.10)


def test_a_scope_with_nothing_estimated_has_a_midpoint_equal_to_its_total(conn):
    """No conversions in scope means no estimate, so the three totals collapse to
    one figure and the surface has no range to show."""
    add_fill(conn, "1", asset="OPT", commission=-2.00)
    f = build_costs(conn, scope=CostScope.of(["OPT"])).friction
    assert not f.is_estimated
    assert f.total_low == f.total_mid == f.total_high == pytest.approx(2.00)


def test_the_markup_is_kept_out_of_the_charged_ledger(conn):
    """It was never billed in any currency, so it must never appear as an
    as-charged figure. `stated` is a Charge; the estimate is a bare float."""
    add_fill(conn, "1", asset="CASH", symbol="EUR.USD", notional=10_000,
             commission=0.0, notes="AFx")
    f = build_costs(conn).friction
    assert f.stated.by_ccy == {}
    assert f.stated.base == 0.0
    assert f.estimated_low == pytest.approx(3.00)


def test_no_markup_is_added_when_conversions_are_out_of_scope(conn):
    """A reader looking at options alone is not paying a conversion cost on that
    scope's terms, and adding it there would attribute a currency cost to a
    contract."""
    add_fill(conn, "opt", asset="OPT", commission=-2.00)
    add_fill(conn, "fx", asset="CASH", symbol="EUR.USD", notional=10_000,
             commission=0.0, notes="AFx")
    opt_only = build_costs(conn, scope=CostScope.of(["OPT"])).friction
    assert not opt_only.is_estimated
    assert opt_only.total_low == opt_only.total_high == pytest.approx(2.00)
    with_fx = build_costs(conn, scope=CostScope.of(["OPT", "CASH"])).friction
    assert with_fx.is_estimated


def test_stated_commission_is_comparable_in_bps(conn):
    """The only way to see that a manual conversion's fixed minimum costs
    multiples of the 3 bps an auto-conversion embeds: 2.00 on 10,000 is 2 bps."""
    add_fill(conn, "1", asset="CASH", symbol="EUR.SEK", notional=10_000,
             commission=-2.00, commission_ccy="EUR")
    assert build_costs(conn).fx[0].commission_bps == pytest.approx(2.0)
    assert AUTOFX_MARKUP_BPS < AUTOFX_MARKUP_MEASURED_BPS


# --- 0DTE, and subsets that are not categories -------------------------------
#
# 0DTE sits in the same selector as Options and Stocks but is a different KIND of
# thing: a subset of options, classified per round trip by comparing entry date to
# expiry, so it cannot be a WHERE on a column. It arrives as fills.


def test_a_fill_subset_narrows_within_the_categories(conn):
    """Both conditions apply, so Options + 0DTE is the 0DTE options.

    A union would be the wrong reading -- ticking a subset of what is already
    selected cannot widen the result -- and is what treating 0DTE as a peer
    category would have produced.
    """
    add_fill(conn, "a", asset="OPT", commission=-2.00)
    add_fill(conn, "b", asset="OPT", commission=-3.00)
    add_fill(conn, "c", asset="STK", commission=-9.00)
    scope = CostScope.of(["OPT"], fill_ids={"a"}, subset="0DTE")
    r = build_costs(conn, scope=scope)
    assert r.attributable.base == pytest.approx(2.00)
    assert r.fills == 1
    assert scope.is_subset


def test_a_subset_matching_nothing_reports_zeros_not_everything(conn):
    """The failure mode a dropped clause produces.

    An empty fill set is a subset that matched nothing -- an account with no 0DTE
    trades -- and is meaningfully different from None. `IN ()` is a SQLite syntax
    error, so the impossible condition has to be spelled out; forgetting to would
    silently widen the report back to the whole scope.
    """
    add_fill(conn, "a", asset="OPT", commission=-2.00)
    r = build_costs(conn, scope=CostScope.of(["OPT"], fill_ids=frozenset()))
    assert r.attributable.base == 0.0
    assert r.fills == 0
    assert r.by_category == []


def test_no_fill_subset_is_distinct_from_an_empty_one(conn):
    add_fill(conn, "a", asset="OPT", commission=-2.00)
    assert not CostScope.of(["OPT"]).is_subset
    assert CostScope.of(["OPT"], fill_ids=frozenset()).is_subset
    assert build_costs(
        conn, scope=CostScope.of(["OPT"])
    ).attributable.base == pytest.approx(2.00)


def test_a_subset_still_reports_account_fees_whole(conn):
    """Same rule as every other scope: a subset cannot narrow a cost that carries
    no attribution, and pro-rating a market-data fee into a 0DTE subset would be
    inventing a split twice over."""
    add_fill(conn, "a", asset="OPT", commission=-2.00)
    add_cash(conn, "f1", amount=-1.30)
    r = build_costs(conn, scope=CostScope.of(["OPT"], fill_ids={"a"}))
    assert r.unattributable.base == pytest.approx(1.30)


def test_the_odte_subset_comes_from_the_episode_layer(conn):
    """Not recomputed here.

    `stats.odte_scope` classifies 0DTE per round trip -- deliberately NOT "every
    fill whose trade date equals its expiry", which would also catch the
    expiry-day close of a position held for a month. This engine consumes that
    answer, so the two surfaces cannot disagree about which trades are which.
    """
    from optjournal.stats import odte_scope

    scope = odte_scope(conn)
    r = build_costs(
        conn, scope=CostScope.of(["OPT"], fill_ids=scope.trade_ids, subset="0DTE")
    )
    # No 0DTE round trips in this fixture, so the honest answer is zeros.
    assert r.attributable.base == 0.0


def test_the_odte_subset_narrows_a_journal_that_has_some(tmp_path):
    """The same path against data that actually contains a 0DTE round trip.

    The fixture above proves the wiring and reports an honest zero, which is the
    weaker half of the claim: a filter that always returns nothing would pass it.
    The demo statement carries a purpose-built 0DTE trade (opened and closed on
    expiry day), so this one asserts the subset is a PROPER narrowing -- fewer
    fills than the category, and a cost above zero.
    """
    from optjournal.demo import build_demo_statement
    from optjournal.ingest import ASSET_FILTER_ALL, ingest_file
    from optjournal.stats import odte_scope

    path = tmp_path / "demo.xml"
    path.write_text(build_demo_statement(), encoding="utf-8")
    conn = connect_migrated(tmp_path / "demo.db")
    ingest_file(conn, path, assets=ASSET_FILTER_ALL)

    options = build_costs(conn, scope=CostScope.of(["OPT"]))
    odte = build_costs(
        conn,
        scope=CostScope.of(
            ["OPT"], fill_ids=odte_scope(conn).trade_ids, subset="0DTE"
        ),
    )
    assert 0 < odte.fills < options.fills
    assert 0 < odte.attributable.base < options.attributable.base


# --- withholding -------------------------------------------------------------


def test_withholding_reports_an_effective_rate(conn):
    """Withheld over the GROSS dividend, which is what IBKR's Dividends row is.

    Verified on the real account: 0.8609 IBKR shares at USD 0.0875 is 0.0753,
    reported as a 0.08 Dividends row with 0.02 withheld beside it. So the row is
    the gross, and dividing by gross plus withheld understated a 30% rate as
    about 23%.
    """
    add_cash(conn, "d1", kind="Dividends", symbol="IBKR", amount=80.0,
             description="IBKR cash dividend")
    add_cash(conn, "w1", kind="Withholding Tax", symbol="IBKR", amount=-24.0,
             description="IBKR withholding")
    line = build_costs(conn).withholding[0]
    assert line.symbol == "IBKR"
    assert line.effective_rate == pytest.approx(30.0)


def test_a_withholding_refund_and_a_dividend_reversal_net_off(conn):
    """Both carry signs, and both were summed as magnitudes.

    A reclaimed withholding arrives positive and a reversed dividend negative;
    read as magnitudes they doubled the tax and the dividend instead of
    cancelling them.
    """
    add_cash(conn, "d1", kind="Dividends", symbol="ACME", amount=100.0)
    add_cash(conn, "d2", kind="Dividends", symbol="ACME", amount=-100.0)
    add_cash(conn, "d3", kind="Dividends", symbol="ACME", amount=100.0)
    add_cash(conn, "w1", kind="Withholding Tax", symbol="ACME", amount=-30.0)
    add_cash(conn, "w2", kind="Withholding Tax", symbol="ACME", amount=15.0)
    (line,) = build_costs(conn).withholding
    assert line.gross.base == pytest.approx(100.0)
    assert line.withheld.base == pytest.approx(15.0)


def test_withholding_with_no_dividend_reports_no_rate(conn):
    """Withholding on credit interest arrives with no DIVIDEND counterpart, and
    dividing by the withholding alone would report a meaningless 100%."""
    add_cash(conn, "w1", kind="Withholding Tax", symbol=None, amount=-0.02)
    line = build_costs(conn).withholding[0]
    assert line.effective_rate is None
    assert line.withheld.base == pytest.approx(0.02)


# --- period ------------------------------------------------------------------


def test_a_period_narrows_trades_and_fees_alike(conn):
    add_fill(conn, "1", date="2026-03-01", commission=-2.00)
    add_fill(conn, "2", date="2026-04-01", commission=-5.00)
    add_cash(conn, "f1", date="2026-03-01", amount=-1.00)
    add_cash(conn, "f2", date="2026-04-01", amount=-3.00)
    march = build_costs(conn, period="2026-03")
    assert march.attributable.base == pytest.approx(2.00)
    assert march.unattributable.base == pytest.approx(1.00)
    year = build_costs(conn, period="2026")
    assert year.attributable.base == pytest.approx(7.00)
    assert year.unattributable.base == pytest.approx(4.00)


def test_an_empty_period_reports_zeros_not_an_error(conn):
    """A month with no activity is a fact, and renders as honest zeros."""
    add_fill(conn, "1", date="2026-03-01", commission=-2.00)
    r = build_costs(conn, period="1999-01")
    assert r.by_category == []
    assert r.attributable.base == 0.0
    assert r.friction.total_low == 0.0
    assert r.from_date is None and r.to_date is None


def test_the_reported_period_comes_from_the_rows_in_scope(conn):
    """Not from the statement's own window, which is the whole reason this
    engine exists: the newest archive covers 30 days and the journal does not."""
    add_fill(conn, "1", date="2025-08-28", commission=-1.00)
    add_fill(conn, "2", date="2026-08-07", commission=-1.00)
    r = build_costs(conn)
    assert (r.from_date, r.to_date) == ("2025-08-28", "2026-08-07")


def test_orders_are_counted_once_per_order_not_per_fill(conn):
    add_fill(conn, "1", commission=-1.00, order="o1")
    add_fill(conn, "2", commission=-1.00, order="o1")
    add_fill(conn, "3", commission=-1.00, order="o2")
    cost = build_costs(conn).by_category[0]
    assert (cost.fills, cost.orders) == (3, 2)


# --- the payload -------------------------------------------------------------


def test_the_payload_is_json_safe(conn):
    """No Decimals, no sets, no domain objects. The CLI dumps this verbatim."""
    import json

    from optjournal.serialize import broker_costs_data

    add_fill(conn, "1", commission=-2.00)
    add_cash(conn, "f1", amount=-1.30)
    add_cash(conn, "d1", kind="Dividends", symbol="X", amount=10.0)
    add_cash(conn, "w1", kind="Withholding Tax", symbol="X", amount=-1.5)
    payload = broker_costs_data(build_costs(conn))
    assert json.loads(json.dumps(payload)) == payload


def test_every_cost_in_the_payload_carries_its_ledger(conn):
    """The shape that makes 'lead with one number, then dissect' possible without
    a second request: Money's three keys, plus `charged`."""
    from optjournal.serialize import broker_costs_data

    add_fill(conn, "1", asset="OPT", commission=-2.00, commission_ccy="USD")
    add_fill(conn, "2", asset="STK", commission=-10.00, commission_ccy="SEK",
             currency="SEK", rate=0.09)
    totals = broker_costs_data(build_costs(conn))["totals"]
    attributable = totals["attributable"]
    assert set(attributable) == {"base", "native", "ccy", "charged"}
    assert attributable["native"] is None, "mixed scope cannot name one currency"
    assert attributable["charged"] == {"USD": pytest.approx(2.00),
                                       "SEK": pytest.approx(10.00)}


def test_the_payload_keeps_the_estimate_out_of_the_charged_ledger(conn):
    """`stated` is billed money; the estimate is a range of bare floats.

    Sending the markup as a Charge would let the page render an as-charged figure
    for a cost that was never itemised in any currency.
    """
    from optjournal.serialize import broker_costs_data

    add_fill(conn, "1", asset="CASH", symbol="EUR.USD", notional=10_000,
             commission=0.0, notes="AFx")
    friction = broker_costs_data(build_costs(conn))["totals"]["friction"]
    assert friction["stated"]["charged"] == {}
    assert friction["estimated_low_base"] == pytest.approx(3.00)
    assert friction["estimated_high_base"] == pytest.approx(3.20)
    assert friction["total_low_base"] < friction["total_high_base"]
    assert friction["is_estimated"] is True
    # The headline figure travels with the range it came from, never instead of
    # it: the page cannot print a point estimate without the band beside it.
    assert (
        friction["total_low_base"]
        < friction["total_mid_base"]
        < friction["total_high_base"]
    )


def test_the_payload_echoes_the_scope_it_measured(conn):
    """So the page labels a total from the payload rather than from its own
    state. The two drifting is how a figure gets captioned with the wrong scope.
    """
    from optjournal.serialize import broker_costs_data

    add_fill(conn, "a", asset="OPT", commission=-2.00)
    scope = CostScope.of(["OPT"], fill_ids={"a"}, subset="0DTE")
    sent = broker_costs_data(build_costs(conn, scope=scope))["scope"]
    assert sent == {"categories": ["OPT"], "is_everything": False,
                    "subset": "0DTE", "is_subset": True}


# --- reconciliation against the statement path -------------------------------


#: Reconciled ONE statement at a time, each into its own database. The obvious
#: shape -- ingest everything, compare against the widest statement -- is wrong,
#: and wrong in the direction that matters: the database is the UNION of every
#: archive, so its OPT commission is 15.16 where the widest single statement sees
#: 4.85. That gap is not a defect, it is the entire reason this engine exists, so
#: a test built on the two being equal would have to be "fixed" by narrowing the
#: engine back to a statement's window. One statement in, one statement compared.
def _one_statement_db(path, tmp_path) -> sqlite3.Connection:
    from optjournal.ingest import ASSET_FILTER_ALL, ingest_file

    conn = connect_migrated(tmp_path / f"{path.stem}.db")
    ingest_file(conn, path, assets=ASSET_FILTER_ALL)
    return conn


def _statement_ids() -> list[str]:
    from conftest import STATEMENTS

    return [p.stem for p in STATEMENTS]


@pytest.mark.parametrize("index", range(len(_statement_ids())), ids=_statement_ids())
def test_the_two_engines_agree_on_every_archived_statement(index, tmp_path):
    """The load-bearing test: same broker data, two routes, one answer.

    `analysis.py` reads the XML; this module reads the SQLite that same XML was
    ingested into. A figure they disagree about is a defect in one of them, and
    which one is a question the reconciliation makes answerable rather than a
    judgement call.

    Every statement, not a representative one: the archive holds 30-day slices
    and 12-month spans, one statement with a single conversion and one with 127,
    and the interesting failures live at the edges.
    """
    from conftest import STATEMENTS

    from optjournal.analysis import analyse
    from optjournal.flex import load

    path = STATEMENTS[index]
    expected = analyse(load(path).FlexStatements[0])
    conn = _one_statement_db(path, tmp_path)
    try:
        engine = build_costs(conn)
    finally:
        conn.close()

    mine = {c.category: c for c in engine.by_category}
    for group in expected.commissions:
        got = mine.get(group.asset_category)
        assert got is not None, f"{group.asset_category} missing from the DB engine"
        assert got.commission.base == pytest.approx(
            float(group.commission_base), abs=1e-6
        ), group.asset_category
        assert got.fills == group.fills, group.asset_category

    assert engine.attributable.base == pytest.approx(
        float(expected.total_commission_base + expected.total_taxes_base), abs=1e-6
    )
    assert engine.unattributable.base == pytest.approx(
        float(expected.total_fees_base), abs=1e-6
    )


@pytest.mark.parametrize("index", range(len(_statement_ids())), ids=_statement_ids())
def test_both_engines_estimate_the_same_autofx_markup(index, tmp_path):
    """The figure the `AFx;P` defect silently reduced.

    Reconciled rather than asserted against a literal, so it stays true as the
    archive grows -- and per statement, because the one carrying `AFx;P` is the
    only one where the two engines could ever have disagreed.
    """
    from conftest import STATEMENTS

    from optjournal.analysis import analyse
    from optjournal.flex import load

    path = STATEMENTS[index]
    expected = analyse(load(path).FlexStatements[0])
    conn = _one_statement_db(path, tmp_path)
    try:
        engine = build_costs(conn)
    finally:
        conn.close()

    assert engine.autofx_conversions == sum(
        p.autofx_conversions for p in expected.fx
    )
    assert engine.autofx_notional == pytest.approx(
        float(expected.total_autofx_notional_base), abs=1e-6
    )
    assert engine.autofx_markup() == pytest.approx(
        float(expected.total_autofx_spread_base), abs=1e-6
    )
