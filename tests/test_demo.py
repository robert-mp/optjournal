"""Tests driven by the synthetic statement.

The real archive is the better oracle where it reaches, and most of the suite
is parametrized over it. But it holds two option fills from one order with
nothing closed, so whole paths have never seen data: closed round trips,
`leg_count > 1`, expiry and assignment dispositions, a roll, a 0DTE trade, a
commission credit, and more than one calendar year. Those are what this module
covers.

The generator is treated as code under test, not as a trusted fixture: the
money arithmetic is asserted to be internally consistent, so a bug in the
generator surfaces here rather than quietly weakening every test that uses it.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from conftest import add_statement, connect_migrated

from optjournal.demo import (
    FROM_DATE,
    TO_DATE,
    assert_not_real,
    build_demo_statement,
    commission_for,
    write_demo_statement,
)
from optjournal.flex import load
from optjournal.history import build_history
from optjournal.ingest import ingest_file
from optjournal.stats import (
    ALL_TRADES,
    _day_of,
    _in_period,
    annual_stats,
    available_months,
    month_stats,
    monthly_stats,
    odte_cohorts,
    odte_scope,
    scope_for,
)

MULTIPLIER = Decimal("100")


@pytest.fixture
def demo(tmp_path) -> Path:
    return write_demo_statement(tmp_path / "demo", tmp_path / "demo.db")


@pytest.fixture
def conn(demo, tmp_path):
    db = connect_migrated(tmp_path / "demo.db")
    # The production default: everything stored, categories scoped per query.
    ingest_file(db, demo)
    yield db
    db.close()


# ------------------------------------------------------------------- generator


def test_output_is_deterministic():
    """Re-running must produce identical bytes.

    Not cosmetic: conids are derived from the contract symbol, and if they moved
    between runs a second ingest would create a parallel set of contracts
    instead of being recognised as the same ones. An earlier version used
    `hash()`, which Python randomises per process.
    """
    assert build_demo_statement() == build_demo_statement()


def test_statement_parses_through_the_real_loader(demo):
    """Must go through py_ibkr's models, not a lenient subset of them."""
    st = load(demo).FlexStatements[0]
    assert st.fromDate == FROM_DATE and st.toDate == TO_DATE
    assert st.Trades and st.CashTransactions


def test_trade_arithmetic_is_self_consistent(demo):
    """The generator is under test too: these identities are IBKR's own."""
    for t in load(demo).FlexStatements[0].Trades or ():
        qty, price = Decimal(t.quantity), Decimal(str(t.tradePrice))
        # The multiplier comes off the row, not from a constant: options carry
        # 100, stock carries 1, and using one for the other is exactly the
        # arithmetic slip this identity exists to catch.
        mult = Decimal(str(t.multiplier))
        assert Decimal(str(t.tradeMoney)) == qty * price * mult, t.symbol
        # proceeds is the cash effect, so it is signed opposite to the position.
        assert Decimal(str(t.proceeds)) == -qty * price * mult, t.symbol
        assert (Decimal(str(t.netCash))
                == Decimal(str(t.proceeds)) + Decimal(str(t.ibCommission))), t.symbol
        assert (t.buySell.value if hasattr(t.buySell, "value") else t.buySell) == (
            "BUY" if qty > 0 else "SELL"
        )


def test_commission_minimum_binds_and_over_collection_is_credited():
    """A split order over-collects the minimum, so the true-up is positive."""
    assert commission_for(-1) == [Decimal("-1.00")], "minimum binds on one lot"
    assert commission_for(-5) == [Decimal("-3.25")], "0.65 per contract above it"

    split = commission_for(-5, (-1, -1, -1, -1, -1))
    assert sum(split) == Decimal("-3.25"), "allocation must sum to the order total"
    assert any(c > 0 for c in split), "no credit, so the sign path is untested"


def test_refuses_to_touch_the_real_archive_or_database(tmp_path):
    root = Path(__file__).resolve().parent.parent
    with pytest.raises(ValueError, match="real archive"):
        assert_not_real(root / "raw")
    with pytest.raises(ValueError, match="real database"):
        assert_not_real(tmp_path, root / "journal.db")
    assert_not_real(tmp_path, tmp_path / "demo.db")  # a scratch pair is fine


# ------------------------------------------------------------ what it unlocks


def test_ingests_cleanly(conn, demo):
    """No warnings, and a second pass inserts nothing."""
    again = ingest_file(conn, demo, reingest=True)
    assert again.warnings == []
    assert again.trades_inserted == 0, "re-ingest must be idempotent"
    assert again.trades_skipped_existing > 0


def test_spans_more_than_one_calendar_year(conn):
    months = available_months(conn)
    assert len({m[:4] for m in months}) > 1, "Annual needs two calendar years"
    assert len(months) >= 12


def test_has_closed_round_trips_with_wins_and_losses(conn):
    """The real account has none, so these metrics were permanently empty."""
    s = month_stats(conn, period=None)
    assert s.wins and s.losses, "need both sides for Win Rate to mean anything"
    assert s.avg_win is not None and s.avg_loss is not None
    assert s.avg_win.base > 0 > s.avg_loss.base
    assert 0 < s.win_rate < 100


def test_has_a_multi_leg_order(conn):
    """`leg_count > 1` is what distinguishes a spread from a single-leg trade."""
    rows = conn.execute(
        "SELECT ib_order_id, COUNT(DISTINCT conid) AS legs FROM trades"
        " GROUP BY ib_order_id HAVING legs > 1"
    ).fetchall()
    assert rows, "no spread, so the option_orders grouping is untested"


def test_has_a_multi_fill_leg_carrying_a_credit(conn):
    """The case that made per-fill abs() overstate commission."""
    credits = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE ib_commission > 0"
    ).fetchone()[0]
    assert credits, "no commission credit in the data"


@pytest.mark.parametrize("disposition", ["EXPIRED", "ASSIGNED"])
def test_note_code_dispositions_are_present(conn, disposition):
    """`disposition_of` maps note codes; the real archive has none to map.

    Asserts on `Episode.status`, which is where the mapped disposition lands
    and what the report's "how" column shows -- rather than re-deriving it and
    testing a parallel implementation.
    """
    assert disposition in {e.status for e in build_history(conn).closed}


def test_has_a_zero_day_round_trip(conn):
    """A 0DTE trade opens and closes on expiry day, so held days is zero."""
    same_day = [
        e for e in build_history(conn).closed
        if e.opened_at and e.closed_at and str(e.opened_at)[:10] == str(e.closed_at)[:10]
    ]
    assert same_day, "no 0DTE round trip"
    assert same_day[0].expiry and str(same_day[0].expiry).replace("-", "")[:8] == (
        str(same_day[0].closed_at).replace("-", "")[:8]
    ), "a 0DTE trade must expire the day it closes"


def test_leaves_positions_open_including_one_without_fills(conn):
    """All three open-position paths: reconstructed from fills, snapshot-only,
    and partially closed -- the one whose booked P&L must stay out of the
    totals until the position is flat."""
    open_eps = build_history(conn).open
    assert len(open_eps) == 3
    assert any(e.snapshot_only for e in open_eps), "no snapshot-only position"
    assert any(not e.snapshot_only for e in open_eps), "no fill-derived position"
    assert any(
        e.close_fills and e.net_qty != 0 for e in open_eps
    ), "no partially closed position"


# ----------------------------------------------------------------- annual/0DTE


def test_the_years_account_for_everything(conn):
    """Per-year figures must sum to the all-time ones they sit beside.

    The Annual tab shows an all-time total row, so any figure that failed to
    reconcile would be visible on screen. Guarded here because the two are
    computed over different period widths and a filter that dropped or
    double-counted a boundary date would still look plausible per year.
    """
    years = annual_stats(conn)
    everything = month_stats(conn, None)
    assert len(years) == 2, "the demo spans two calendar years"

    assert sum(y.total_trades for y in years) == everything.total_trades
    assert sum(y.closed_episodes for y in years) == everything.closed_episodes
    assert sum(y.wins for y in years) == everything.wins
    assert sum(y.losses for y in years) == everything.losses
    assert sum(y.net_pnl.base for y in years) == pytest.approx(
        everything.net_pnl.base, abs=1e-9
    )
    assert sum(y.commissions.base for y in years) == pytest.approx(
        everything.commissions.base, abs=1e-9
    )
    # Every trading day belongs to exactly one year.
    assert sum(len(y.days) for y in years) == len(everything.days)


def test_years_are_newest_first_and_span_only_traded_years(conn):
    years = [y.month for y in annual_stats(conn)]
    assert years == sorted(years, reverse=True)
    assert years == ["2026", "2025"]


def test_a_year_crossing_round_trip_counts_in_the_year_it_closed(conn):
    """The attribution rule, which the generated data cannot exercise.

    Every synthetic episode opens and closes inside one calendar year, so
    attributing by entry instead of exit would pass every other assertion here.
    A December-to-January round trip is the case that separates them, and the
    convention has to match the month selector's -- otherwise the annual rows
    stop summing to the monthly ones.
    """
    template = conn.execute(
        "SELECT * FROM trades WHERE open_close = 'O' LIMIT 1"
    ).fetchone()
    columns = list(template.keys())

    def clone(**overrides) -> None:
        row = dict(template)
        row.update(overrides)
        conn.execute(
            f"INSERT INTO trades ({','.join(columns)})"
            f" VALUES ({','.join('?' for _ in columns)})",
            [row[c] for c in columns],
        )

    before = {y.month: y for y in annual_stats(conn)}
    common = dict(conid="999000001", symbol="XCROSS 260130C00100000",
                  expiry="20260130", notes=None)
    clone(trade_id="X-OPEN", ib_exec_id="X-EXEC-1", transaction_id="X-TXN-1",
          ib_order_id="X-ORD-1", quantity=-1,
          trade_date="2025-12-22", date_time="2025-12-22 14:30:05",
          open_close="O", fifo_pnl_realized=0.0, fifo_pnl_realized_base=0.0,
          **common)
    clone(trade_id="X-CLOSE", ib_exec_id="X-EXEC-2", transaction_id="X-TXN-2",
          ib_order_id="X-ORD-2", quantity=1,
          trade_date="2026-01-05", date_time="2026-01-05 14:30:05",
          open_close="C", fifo_pnl_realized=100.0, fifo_pnl_realized_base=90.0,
          **common)
    conn.commit()

    after = {y.month: y for y in annual_stats(conn)}
    assert after["2026"].closed_episodes == before["2026"].closed_episodes + 1, (
        "a round trip closed in January must count as a January-year outcome"
    )
    assert after["2025"].closed_episodes == before["2025"].closed_episodes, (
        "counting it in the entry year would double it across the two rows"
    )
    # The realised P&L follows the closing fill's own trade date, so the two
    # measures agree about which year the money landed in.
    assert after["2026"].net_pnl.base == pytest.approx(
        before["2026"].net_pnl.base + 90.0, abs=1e-9
    )
    # And the reconciliation still holds with a boundary-crossing episode.
    everything = month_stats(conn, None)
    assert sum(y.closed_episodes for y in after.values()) == everything.closed_episodes


def test_odte_cohorts_partition_every_closed_round_trip(conn):
    """Nothing may be counted twice or fall between the two columns."""
    odte, rest, unknown = odte_cohorts(conn)
    closed = len(build_history(conn, asset_category="OPT").closed)
    assert odte.episodes + rest.episodes + unknown == closed
    assert unknown == 0, "every option in the demo carries an expiry"


def test_the_demo_has_exactly_one_odte_round_trip(conn):
    """The path that was unreachable: the ODTE tab had no trade to show."""
    odte, rest, _ = odte_cohorts(conn)
    assert odte.episodes == 1
    assert rest.episodes > 1, "a cohort of one needs something to compare against"
    assert odte.win_rate is not None
    assert odte.avg_pnl.base == pytest.approx(odte.net_pnl.base, abs=1e-9), (
        "one round trip, so the average is the total"
    )


def test_period_filter_matches_both_widths_and_both_date_forms():
    """`_in_period` is what lets one summation serve months and years."""
    assert _in_period("2025-03-14 10:00:00", "2025")
    assert _in_period("2025-03-14 10:00:00", "2025-03")
    assert not _in_period("2025-03-14", "2025-04")
    assert not _in_period("2026-01-05", "2025")
    # IBKR's compact form must not be prefix-matched raw: "20250314" does not
    # start with "2025-03", so the value has to be normalised first.
    assert _in_period("20250314", "2025-03")
    assert _in_period("20250314", "2025")
    # No period means everything; an unparseable date belongs to no period.
    assert _in_period("20250314", None)
    assert not _in_period(None, "2025")
    assert not _in_period("garbage", "2025")


# ------------------------------------------------------------------- scoping


def test_odte_scope_is_episode_membership_not_a_fill_predicate(conn):
    """The trap: "every fill whose trade date equals its expiry" is wider.

    An expiry-day close of a position held for weeks satisfies that predicate
    without being 0DTE trading at all. The demo has exactly such a fill -- an
    expiry and an assignment both settle on the expiry date -- so the two
    definitions give different answers here and the scope must take the
    narrower one.
    """
    scope = odte_scope(conn)
    same_day_fills = {
        str(r["trade_id"])
        for r in conn.execute("SELECT trade_id, trade_date, expiry FROM trades")
        if _day_of(r["trade_date"]) == _day_of(r["expiry"])
    }
    assert same_day_fills > set(scope.trade_ids), (
        "precondition: the fill-level predicate must be strictly wider here, "
        "otherwise this test proves nothing"
    )
    # Every fill the scope claims is one of a 0DTE round trip's own fills.
    odte_eps = [e for e in build_history(conn, asset_category="OPT").episodes
                if e.is_odte is True]
    assert set(scope.trade_ids) == {str(t) for e in odte_eps for t in e.trade_ids}


def test_scope_narrows_every_trade_derived_figure(conn):
    """A filter that moved only some of the numbers would be worse than none."""
    everything = month_stats(conn, None)
    only = month_stats(conn, None, scope=odte_scope(conn))
    assert 0 < only.total_trades < everything.total_trades
    assert 0 < only.closed_episodes < everything.closed_episodes
    assert only.orders < everything.orders
    assert abs(only.net_pnl.base) < abs(everything.net_pnl.base)
    assert abs(only.commissions.base) < abs(everything.commissions.base)
    assert len(only.days) < len(everything.days)


def test_scope_leaves_account_level_fees_alone(conn):
    """Fees carry no trade linkage, so narrowing them would invent one."""
    everything = month_stats(conn, None)
    only = month_stats(conn, None, scope=odte_scope(conn))
    assert only.fees == everything.fees


def test_scope_hides_months_it_has_emptied(conn):
    """Offering a month the filter emptied would look like a broken dashboard."""
    all_months = available_months(conn)
    odte_months = available_months(conn, "OPT", odte_scope(conn))
    assert odte_months, "the demo has a 0DTE trade, so at least one month remains"
    assert set(odte_months) < set(all_months)
    for month in odte_months:
        assert month_stats(conn, month, scope=odte_scope(conn)).total_trades > 0


def test_the_default_scope_changes_nothing(conn):
    """ALL_TRADES must be a true no-op, not a filter that happens to pass all."""
    plain = month_stats(conn, None)
    explicit = month_stats(conn, None, scope=ALL_TRADES)
    assert (plain.total_trades, plain.net_pnl.base, plain.closed_episodes) == (
        explicit.total_trades, explicit.net_pnl.base, explicit.closed_episodes
    )
    assert ALL_TRADES.trade_ids is None, "no id set to build when nothing is filtered"
    assert available_months(conn) == available_months(conn, "OPT", ALL_TRADES)


def test_an_unknown_scope_key_shows_everything(conn):
    """The key arrives from a query parameter, so it must fail open, not error."""
    for key in (None, "", "all", "nonsense", "ODTE"):
        scope = scope_for(conn, key)
        expected_odte = key is not None and key.lower() == "odte"
        assert (scope.key == "odte") is expected_odte, key


# --------------------------------------------------------- monthly breakdown


def test_the_months_account_for_everything(conn):
    """Same reconciliation as the years, one granularity down."""
    months = monthly_stats(conn)
    everything = month_stats(conn, None)
    assert len(months) == 14, "the demo spans fourteen months with option fills"
    assert sum(m.total_trades for m in months) == everything.total_trades
    assert sum(m.closed_episodes for m in months) == everything.closed_episodes
    assert sum(m.net_pnl.base for m in months) == pytest.approx(
        everything.net_pnl.base, abs=1e-9
    )


def test_the_months_under_each_year_sum_to_that_year(conn):
    """What the Annual tab's grouping invites the reader to check by eye."""
    years = {y.month: y for y in annual_stats(conn)}
    by_year: dict[str, list] = {}
    for m in monthly_stats(conn):
        by_year.setdefault(str(m.month)[:4], []).append(m)

    assert set(by_year) == set(years), "every month must sit under a listed year"
    for year, months in by_year.items():
        assert sum(m.total_trades for m in months) == years[year].total_trades, year
        assert sum(m.closed_episodes for m in months) == years[year].closed_episodes
        assert sum(m.net_pnl.base for m in months) == pytest.approx(
            years[year].net_pnl.base, abs=1e-9
        ), year


def test_a_monthly_row_equals_what_the_month_selector_produces(conn):
    """The breakdown and the Dashboard must not be two opinions of one month."""
    for row in monthly_stats(conn):
        picked = month_stats(conn, row.month)
        assert row.total_trades == picked.total_trades, row.month
        assert row.net_pnl.base == pytest.approx(picked.net_pnl.base, abs=1e-9)
        assert row.closed_episodes == picked.closed_episodes
        assert row.win_rate == picked.win_rate


def test_months_are_newest_first(conn):
    months = [m.month for m in monthly_stats(conn)]
    assert months == sorted(months, reverse=True)


def test_the_scope_reaches_the_payload_end_to_end(demo, tmp_path):
    """The wiring, on data that actually has a 0DTE round trip.

    The equivalent test over the real archive can only skip -- there is no 0DTE
    trade there to filter to -- so the parameter-to-aggregation path is proven
    here or nowhere.
    """
    from optjournal.web import build_state

    db = tmp_path / "state.db"
    conn = connect_migrated(db)
    ingest_file(conn, demo)
    conn.close()

    kw = dict(db_path=db, archive_dir=demo.parent, query_id=None)
    everything = build_state(**kw)
    scoped = build_state(**kw, trade_type="odte")

    assert everything["trade_type"] == "all"
    assert scoped["trade_type"] == "odte"
    assert everything["odte"]["selectable"] is True

    assert scoped["stats"]["total_trades"] < everything["stats"]["total_trades"]
    assert scoped["stats"]["closed_episodes"] == 1
    assert len(scoped["orders"]) < len(everything["orders"])
    assert set(scoped["months"]) < set(everything["months"])

    # Tabs with no filter bar must not move: a tab whose numbers change with a
    # control it does not display leaves the reader nothing to explain it with.
    assert scoped["annual"] == everything["annual"]
    assert scoped["monthly"] == everything["monthly"]
    assert scoped["annual_total"] == everything["annual_total"], (
        "the Annual table's total row must not follow a filter that tab does "
        "not show, or it drops below the sum of its own rows"
    )
    assert scoped["positions"] == everything["positions"]
    assert scoped["odte"] == everything["odte"]
    # And fees have no trade to attach to, so they cannot narrow either.
    assert scoped["stats"]["fees"] == everything["stats"]["fees"]

    # The reconciliation the Annual table invites must survive an active filter.
    assert sum(y["total_trades"] for y in scoped["annual"]) == (
        scoped["annual_total"]["total_trades"]
    )
    # all_time, by contrast, is the Dashboard's own figure and does narrow.
    assert scoped["all_time"]["total_trades"] < everything["all_time"]["total_trades"]


# ------------------------------------------------- closed-trades-only P&L


def test_a_partial_close_contributes_nothing_until_the_position_is_flat(conn):
    """The case that separates episode P&L from summing per-fill realisation.

    The demo sells 3 puts and buys back 1: IBKR books realised P&L on that
    fill immediately, but the position is not flat, so a "fully closed trades
    only" Net P&L must exclude it. The strictness assertion first proves the
    data really contains the disagreement -- without it, this test would pass
    on any archive where every close is total, i.e. on data that cannot tell
    the two rules apart.
    """
    booked = conn.execute(
        "SELECT SUM(COALESCE(fifo_pnl_realized_base, 0)) AS s FROM trades"
        " WHERE asset_category = 'OPT'"
    ).fetchone()["s"]
    stats = month_stats(conn, None)
    assert booked > stats.net_pnl.base, (
        "precondition: a partial close must have booked per-fill P&L that the"
        " episode rule excludes"
    )
    report = build_history(conn)
    assert stats.net_pnl.base == pytest.approx(
        sum(e.realized_pnl_base for e in report.closed)
    ), "Net P&L must equal the sum of fully closed round trips, nothing else"

    partial_month = month_stats(conn, "2026-02")
    assert partial_month.total_trades == 1, "the buyback fill is activity"
    assert partial_month.net_pnl.base == 0, (
        "the month holding only the partial close realises nothing"
    )


def test_the_whole_outcome_lands_on_the_day_the_round_trip_closed(conn):
    """Attribution: money follows the close date, activity stays on fill days."""
    from optjournal.stats import daily_series

    days = {d.day: d for d in daily_series(conn)}
    # The partial-close day shows the fill and no money.
    partial = days["2026-02-11"]
    assert partial.trades == 1 and partial.realized.base == 0
    # The 0DTE round trip opened and closed on 2026-01-16; the whole outcome
    # sits on that day and equals the episode's own figure.
    report = build_history(conn)
    zero_dte = next(e for e in report.closed if e.is_odte)
    assert days["2026-01-16"].realized.base == pytest.approx(
        zero_dte.realized_pnl_base
    )
    # And nothing realised sits on any day without a close.
    close_days = {_day_of(e.closed_at) for e in report.closed}
    for day, bucket in days.items():
        if day not in close_days:
            assert bucket.realized.base == 0, day


def test_stock_pnl_keeps_ibkrs_per_fill_realisation(conn):
    """"Preserve existing P&L behaviour for all other asset types" -- pinned.

    Each share lot sold is realised when it is sold, so the stock figure is
    the plain sum of per-fill realisation, attributed to the sale's own month
    -- even though a stock episode may never be "closed" in the options sense.
    """
    booked = conn.execute(
        "SELECT SUM(COALESCE(fifo_pnl_realized_base, 0)) AS s FROM trades"
        " WHERE asset_category = 'STK'"
    ).fetchone()["s"]
    stats = month_stats(conn, None, asset_category="STK")
    assert stats.net_pnl.base == pytest.approx(booked)
    assert stats.net_pnl.base > 0, "the scripted stock round trip is a win"
    sale_month = month_stats(conn, "2025-09", asset_category="STK")
    assert sale_month.net_pnl.base == pytest.approx(booked), (
        "stock realisation lands in the month the lot was sold"
    )


# ------------------------------------------------------------- net liquidity


def test_equity_summaries_are_ingested_with_the_long_short_fallback(conn):
    """One NAV row per month end, and the split-field fallback is exercised.

    The demo deliberately emits `stockLong`/`stockShort` instead of a single
    `stock` attribute, so a regression in the combined-field fallback shows
    up here as a NULL stock component through the real pipeline.
    """
    rows = conn.execute(
        "SELECT * FROM equity_summaries ORDER BY report_date"
    ).fetchall()
    assert len(rows) == 14, "one row per month of the demo period"
    for row in rows:
        assert row["stock_base"] is not None, "long/short fallback failed"
        assert row["total_base"] == pytest.approx(
            row["cash_base"] + row["stock_base"] + row["options_base"]
        ), "NAV components must sum to the total they were generated from"


def test_gain_pct_of_net_liq_uses_the_nav_at_the_periods_end(conn):
    everything = month_stats(conn, None)
    assert everything.net_liq_base is not None
    assert everything.net_liq_date == "2026-02-27", "TO_DATE caps the last row"
    assert everything.gain_pct_of_net_liq == pytest.approx(
        everything.net_pnl.base / everything.net_liq_base * 100.0
    )
    # A month period reads its own month-end NAV, not the latest overall.
    october = month_stats(conn, "2025-10")
    assert october.net_liq_date == "2025-10-31"
    # A NAV-less database yields None, not zero -- unavailable is not broke.
    bare = connect_migrated(
        Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent / "bare.db"
    )
    assert month_stats(bare, None).net_liq_base is None
    bare.close()


# ------------------------------------------------------------------ equities


def test_the_equities_selection_switches_category_and_reaches_no_further(conn,
                                                                  demo, tmp_path):
    """`type=equities` swaps the category for the filter-bar views only."""
    from optjournal.web import build_state

    kw = dict(db_path=tmp_path / "demo.db", archive_dir=demo.parent,
              query_id=None)
    plain = build_state(**kw)
    stk = build_state(trade_type="equities", **kw)

    assert stk["trade_type"] == "equities"
    assert stk["stats"]["asset_category"] == "STK"
    assert stk["stats"]["total_trades"] == 3
    assert all(o["asset_category"] == "STK" for o in stk["orders"])
    assert set(stk["months"]) == {"2025-04", "2025-09", "2025-11"}
    # Pinned to the journal's home category: no filter bar on these tabs.
    for key in ("annual", "annual_total", "monthly", "odte", "positions",
                "history"):
        assert stk[key] == plain[key], f"{key} must not follow the control"
    # Offerability is derived from the data, not asserted in markup.
    assert plain["asset_counts"]["STK"] == 3


# ------------------------------------------------------- synthetic option bars

#: Every option contract the demo scripts, with the REAL close of its underlying
#: on the day the statement first states a price for it. Transcribed rather than
#: fetched because these are settled historical closes and cannot change, and
#: because the whole point is to check the demo's strikes against the market the
#: chart actually draws -- a fixture at invented price levels would agree with
#: invented strikes and prove nothing.
#:
#: This table is the oracle the demo lacked. Two call strikes had been chosen for
#: a price level SPY never traded at during their windows, leaving the statement
#: claiming a premium below the contract's intrinsic value. Nothing caught it:
#: strike does not enter any P&L arithmetic, so every money assertion passed
#: while the replay chart's band, delta and modelled P&L were silently absent.
_REAL_SPOT_AT_ANCHOR = [
    # symbol,                     anchor day,   real close, strike, right, price
    ("NVDA  250620P00105000", "2025-05-12", 123.00, 105.0, "P", 2.10),
    ("NVDA  250815P00112000", "2025-07-09", 162.88, 112.0, "P", 3.35),
    ("NVDA  251017P00160000", "2025-09-08", 168.31, 160.0, "P", 2.10),
    ("NVDA  251017P00170000", "2025-09-08", 168.31, 170.0, "P", 4.55),
    ("NVDA  260320P00140000", "2026-01-27", 188.52, 140.0, "P", 5.05),
    ("SPY   250417C00580000", "2025-03-05", 583.06, 580.0, "C", 9.10),
    ("SPY   251121P00560000", "2025-10-20", 671.30, 560.0, "P", 5.80),
    ("SPY   251219P00555000", "2025-11-17", 665.67, 555.0, "P", 6.15),
    ("SPY   260116C00695000", "2026-01-16", 691.66, 695.0, "C", 1.95),
    ("SPY   260417P00590000", "2026-01-08", 689.51, 590.0, "P", 7.20),
    ("SPY   260618C00740000", "2026-02-27", 685.99, 740.0, "C", 11.40),
]

#: Approximate years from each anchor to expiry. Only needs to be close: the
#: assertion is that a vol EXISTS, not that it takes a particular value.
_YEARS_TO_EXPIRY = {
    "NVDA  250620P00105000": 0.11, "NVDA  250815P00112000": 0.10,
    "NVDA  251017P00160000": 0.11, "NVDA  251017P00170000": 0.11,
    "NVDA  260320P00140000": 0.14, "SPY   250417C00580000": 0.12,
    "SPY   251121P00560000": 0.09, "SPY   251219P00555000": 0.09,
    "SPY   260116C00695000": 0.0007, "SPY   260417P00590000": 0.27,
    "SPY   260618C00740000": 0.30,
}


@pytest.mark.parametrize(
    ("symbol", "day", "spot", "strike", "right", "price"), _REAL_SPOT_AT_ANCHOR,
    ids=[row[0].strip() for row in _REAL_SPOT_AT_ANCHOR],
)
def test_every_scripted_option_is_priceable_against_the_real_market(
    symbol, day, spot, strike, right, price
):
    """A demo premium must be above intrinsic at the real spot for its own date.

    Below intrinsic there is no volatility that reproduces the price, so the vol
    solve refuses it -- correctly -- and the contract silently loses its band,
    its effective delta and its modelled P&L. The demo exists to exercise paths
    the real account cannot, so a contract that cannot be priced exercises
    nothing.
    """
    from optjournal.blackscholes import implied_vol

    intrinsic = max(0.0, (strike - spot) if right == "P" else (spot - strike))
    assert price > intrinsic, (
        f"{symbol} is priced at {price} on {day} when {spot} spot makes it worth "
        f"{intrinsic:.2f} at once -- no volatility can produce that"
    )
    vol = implied_vol(price, spot, strike, _YEARS_TO_EXPIRY[symbol], right)
    assert vol is not None, f"{symbol} has no implied vol on {day}"
    assert 0.01 < vol < 3.0, f"{symbol} implies an absurd {vol:.1%} vol"


def test_a_premium_below_intrinsic_has_no_vol_at_all():
    """The control for the test above. Without it, an implied_vol that returned a
    number for every input would make that whole parametrized set vacuous -- and
    the strike bug it exists to catch would pass again.

    These are the two contracts as they were actually scripted, against the spot
    their windows really traded at.
    """
    from optjournal.blackscholes import implied_vol

    assert implied_vol(1.95, 691.66, 600.0, 0.0007, "C") is None, (
        "a 600 call at 1.95 with spot at 692 is 92 dollars in the money"
    )
    assert implied_vol(11.40, 685.99, 640.0, 0.30, "C") is None, (
        "a 640 call at 11.40 with spot at 686 is 46 dollars in the money"
    )


def _with_underlying(conn, symbol="SPY", closes=((0, 690.0),)):
    """Store a daily underlying series at given (day offset, close) pairs."""
    from datetime import UTC, datetime, timedelta

    from optjournal.bars import upsert_bars
    from optjournal.demo import _UNDERLYING_CONID
    from optjournal.marketdata import Bar

    start = datetime(2026, 1, 5, 5, 0, tzinfo=UTC)   # midnight ET
    bars = [
        Bar(ts=int((start + timedelta(days=offset)).timestamp()),
            open=close, high=close, low=close, close=close, volume=0)
        for offset, close in closes
    ]
    upsert_bars(conn, conid=_UNDERLYING_CONID[symbol], symbol=symbol,
                bar_size="1d", source="yahoo", bars=bars)


def test_synthetic_bars_reprice_the_statements_own_anchor(conn):
    """The property that makes computed bars trustworthy rather than decorative.

    Vol is solved from a price the statement itself states, and the bar for that
    same session carries that price verbatim -- so the series the chart draws
    passes through the figure the journal reports. A generator that assumed a
    plausible vol instead would mark positions at values contradicting the
    realised P&L printed beside them.
    """
    from optjournal.bars import close_series, epoch_et, et_day
    from optjournal.blackscholes import implied_vol
    from optjournal.demo import _UNDERLYING_CONID, write_demo_bars

    _with_underlying(conn, "SPY", [(n, 690.0 - n) for n in range(40)])
    assert write_demo_bars(conn) > 0, "no bars written for a priceable contract"

    # The partial-close SPY 590 put: opened 2026-01-08 at 7.20.
    leg = conn.execute(
        "SELECT conid, first_fill_at, avg_price FROM trade_legs"
        " WHERE symbol = 'SPY   260417P00590000' ORDER BY first_fill_at LIMIT 1"
    ).fetchone()
    option = close_series(conn, str(leg["conid"]), bar_size="1d")
    assert option, "the contract got no bars"

    anchor_day = et_day(epoch_et(leg["first_fill_at"]))
    priced = {et_day(stamp): close for stamp, close in option}
    assert priced[anchor_day] == pytest.approx(abs(float(leg["avg_price"])), abs=0.01), (
        "the session the statement priced does not carry the statement's price"
    )

    spots = {
        et_day(stamp): close
        for stamp, close in close_series(
            conn, _UNDERLYING_CONID["SPY"], bar_size="1d"
        )
    }
    for stamp, close in option[:5]:
        vol = implied_vol(close, spots[et_day(stamp)], 590.0, 0.27, "P")
        assert vol is not None, f"a generated bar at {close} cannot be solved back"


def test_the_generated_vol_steps_between_sessions(conn):
    """Otherwise the demo misrepresents the feature it exists to show. On real
    data the vol input is re-solved from each session's own close, so the band
    steps; a single vol held flat across a window would draw a smooth cone the
    real journal never produces.
    """
    from optjournal.bars import close_series
    from optjournal.demo import write_demo_bars

    # A FLAT underlying, so any variation in the option's price can only come
    # from the vol input rather than from spot moving.
    _with_underlying(conn, "SPY", [(n, 690.0) for n in range(40)])
    write_demo_bars(conn)
    conid = conn.execute(
        "SELECT conid FROM trade_legs WHERE symbol = 'SPY   260417P00590000'"
        " LIMIT 1"
    ).fetchone()["conid"]
    closes = [close for _stamp, close in close_series(conn, str(conid), bar_size="1d")]
    assert len(closes) > 5, "too few bars to judge"
    # Decay alone would make this monotonic; stepping vol must break that.
    falling = all(b <= a for a, b in zip(closes, closes[1:], strict=False))
    assert not falling, "the vol input never changed between sessions"


def test_synthetic_bars_are_stamped_where_the_source_stamps_them(conn):
    """Midnight ET, which is where the price source puts an option's daily bar --
    and NOT the session open, where it puts the underlying's. The two series are
    joined on the trading day precisely because those conventions differ, so bars
    emitted at the open here would make the demo the one place a raw-timestamp
    join works and leave that bug untestable.
    """
    from datetime import datetime

    from optjournal.bars import MARKET_TZ, close_series
    from optjournal.demo import write_demo_bars

    _with_underlying(conn, "SPY", [(n, 690.0 - n) for n in range(40)])
    write_demo_bars(conn)
    rows = conn.execute(
        "SELECT ts FROM price_bars WHERE source = 'synthetic' LIMIT 20"
    ).fetchall()
    assert rows, "no synthetic bars to check"
    for row in rows:
        local = datetime.fromtimestamp(int(row["ts"]), MARKET_TZ)
        assert (local.hour, local.minute) == (0, 0), (
            f"stamped {local:%H:%M} ET, not midnight"
        )
    assert not close_series(conn, "nope", bar_size="1h"), "sanity: no hourly bars"


def test_synthetic_bars_are_reproducible(conn):
    """`optjournal demo` must produce the same journal twice. The vol jitter is
    hashed from the calendar day rather than drawn from `random` for exactly this
    reason -- otherwise every re-run would move the band and no chart in the demo
    could be compared with itself.
    """
    from optjournal.demo import write_demo_bars

    _with_underlying(conn, "SPY", [(n, 690.0 - n) for n in range(40)])
    write_demo_bars(conn)
    first = conn.execute(
        "SELECT conid, ts, close FROM price_bars WHERE source = 'synthetic'"
        " ORDER BY conid, ts"
    ).fetchall()
    conn.execute("DELETE FROM price_bars WHERE source = 'synthetic'")
    conn.commit()
    write_demo_bars(conn)
    second = conn.execute(
        "SELECT conid, ts, close FROM price_bars WHERE source = 'synthetic'"
        " ORDER BY conid, ts"
    ).fetchall()
    assert first, "nothing generated"
    assert [tuple(r) for r in first] == [tuple(r) for r in second]


def test_synthetic_bars_refuse_a_database_holding_a_real_statement(conn):
    """Every row in `price_bars` is supposed to be something a source served, so
    a computed one in the real journal would break the reproducibility the
    archive exists to provide -- and would be indistinguishable from a fetched
    row afterwards. Scoped to the DATA rather than to a path, so pointing `--db`
    at a copy of the real journal is refused too.
    """
    from optjournal.demo import write_demo_bars

    _with_underlying(conn, "SPY", [(n, 690.0 - n) for n in range(40)])
    assert write_demo_bars(conn) > 0, "the control: it works before the intruder"
    # A statement that is NOT a demo one: the file name and account are the
    # whole point, since the refusal is scoped to what the data says rather
    # than to which path the database sits at.
    add_statement(
        conn, source_file="activity-U123-real.xml", sha256="y",
        account_id="U123", from_date="2026-01-01", to_date="2026-02-01",
    )
    conn.commit()
    with pytest.raises(ValueError, match="real statements"):
        write_demo_bars(conn)


def test_a_rerun_replaces_the_demo_and_keeps_the_real_underlying_series(conn):
    """Trades dedupe on identifiers the generator derives deterministically, so a
    changed contract arrives under an existing trade_id and the insert is a no-op
    -- the database keeps describing a statement it was not built from. Found by
    changing a strike and watching the old one survive alongside a snapshot of
    the new one.

    The underlying series must survive: it is real market history that cost
    network requests, and re-fetching it is the expensive part of a rebuild.
    """
    from optjournal.demo import DEMO_ACCOUNT, reset_demo_rows, write_demo_bars

    _with_underlying(conn, "SPY", [(n, 690.0 - n) for n in range(40)])
    write_demo_bars(conn)
    before = conn.execute(
        "SELECT COUNT(*) AS n FROM price_bars WHERE source = 'yahoo'"
    ).fetchone()["n"]
    assert before > 0 and conn.execute(
        "SELECT COUNT(*) AS n FROM trades WHERE account_id = ?", (DEMO_ACCOUNT,)
    ).fetchone()["n"] > 0

    reset_demo_rows(conn)
    for table in ("trades", "position_snapshots", "statements"):
        left = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE account_id = ?",
            (DEMO_ACCOUNT,),
        ).fetchone()["n"]
        assert left == 0, f"{table} kept demo rows a re-ingest would not replace"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM price_bars WHERE source = 'yahoo'"
    ).fetchone()["n"] == before, "the real underlying history was thrown away"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM price_bars WHERE source = 'synthetic'"
    ).fetchone()["n"] == 0, "stale computed bars survived a rebuild"


def test_every_demo_replay_carries_a_band_and_an_effective_delta(demo, tmp_path):
    """The guard that was missing while the demo was blank.

    Both the payload contract and the render sweep check that markup MATCHES the
    payload, so an empty band satisfied every one of them: nothing drawn, nothing
    to draw, no complaint. 322 render checks passed across 42 pages while two of
    the demo's ten replays carried no band, no effective delta and no modelled
    P&L at all -- and the demo exists precisely to exercise what the real account
    cannot.

    Asserted absolutely rather than conditionally because the demo's data is
    fixed: every contract it scripts is priceable against the real underlying
    series (see the parametrized test above), so a replay without a band means
    something upstream stopped working rather than data being unavailable.
    """
    from optjournal.db import open_journal
    from optjournal.demo import write_demo_bars
    from optjournal.web import build_state

    db = tmp_path / "replays.db"
    with open_journal(db) as conn:
        ingest_file(conn, demo)
        _with_underlying(conn, "SPY", [(n, 690.0 - n * 0.5) for n in range(60)])
        _with_underlying(conn, "NVDA", [(n, 190.0 - n * 0.4) for n in range(60)])
        assert write_demo_bars(conn) > 0, "no option bars generated"

    state = build_state(db_path=db, archive_dir=tmp_path / "demo", query_id=None)
    replays = state.get("replays") or {}
    assert replays, "no replays in the payload at all"

    blank = sorted(
        key for key, replay in replays.items()
        if replay.get("points") and not (replay.get("band") and replay.get("marks"))
    )
    assert not blank, (
        f"{len(blank)} of {len(replays)} replays draw a price line and nothing "
        f"modelled on it: {blank}"
    )
    compared = 0
    for key, replay in replays.items():
        # A replay outside this fixture's fabricated underlying window has no
        # points and so nothing to compare -- the assertion above already covers
        # the case that matters, which is points WITHOUT a band.
        band, marks = replay.get("band") or [], replay.get("marks") or []
        if not band or not marks:
            continue
        compared += 1
        assert band[-1][0] == marks[-1][0], (
            f"{key}: band ends at {band[-1][0]}, marks run to {marks[-1][0]}"
        )
    assert compared, "nothing was actually compared"

