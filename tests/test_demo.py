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

from optjournal.db import connect, migrate
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
from optjournal.ingest import ASSET_FILTER_OPTIONS, ingest_file
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
    db = connect(tmp_path / "demo.db")
    migrate(db)
    ingest_file(db, demo, assets=ASSET_FILTER_OPTIONS)
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
        assert Decimal(str(t.tradeMoney)) == qty * price * MULTIPLIER, t.symbol
        # proceeds is the cash effect, so it is signed opposite to the position.
        assert Decimal(str(t.proceeds)) == -qty * price * MULTIPLIER, t.symbol
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
    again = ingest_file(conn, demo, assets=ASSET_FILTER_OPTIONS, reingest=True)
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
    assert s.avg_win_base is not None and s.avg_loss_base is not None
    assert s.avg_win_base > 0 > s.avg_loss_base
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
    """Both open-position paths: reconstructed from fills, and snapshot-only."""
    open_eps = build_history(conn).open
    assert len(open_eps) == 2
    assert any(e.snapshot_only for e in open_eps), "no snapshot-only position"
    assert any(not e.snapshot_only for e in open_eps), "no fill-derived position"


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
    assert sum(y.net_pnl_base for y in years) == pytest.approx(
        everything.net_pnl_base, abs=1e-9
    )
    assert sum(y.commissions_base for y in years) == pytest.approx(
        everything.commissions_base, abs=1e-9
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
    assert after["2026"].net_pnl_base == pytest.approx(
        before["2026"].net_pnl_base + 90.0, abs=1e-9
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
    assert odte.avg_pnl_base == pytest.approx(odte.net_pnl_base, abs=1e-9), (
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
    assert abs(only.net_pnl_base) < abs(everything.net_pnl_base)
    assert abs(only.commissions_base) < abs(everything.commissions_base)
    assert len(only.days) < len(everything.days)


def test_scope_leaves_account_level_fees_alone(conn):
    """Fees carry no trade linkage, so narrowing them would invent one."""
    everything = month_stats(conn, None)
    only = month_stats(conn, None, scope=odte_scope(conn))
    assert only.fees_base == everything.fees_base


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
    assert (plain.total_trades, plain.net_pnl_base, plain.closed_episodes) == (
        explicit.total_trades, explicit.net_pnl_base, explicit.closed_episodes
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
    assert len(months) == 13, "the demo spans thirteen months"
    assert sum(m.total_trades for m in months) == everything.total_trades
    assert sum(m.closed_episodes for m in months) == everything.closed_episodes
    assert sum(m.net_pnl_base for m in months) == pytest.approx(
        everything.net_pnl_base, abs=1e-9
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
        assert sum(m.net_pnl_base for m in months) == pytest.approx(
            years[year].net_pnl_base, abs=1e-9
        ), year


def test_a_monthly_row_equals_what_the_month_selector_produces(conn):
    """The breakdown and the Dashboard must not be two opinions of one month."""
    for row in monthly_stats(conn):
        picked = month_stats(conn, row.month)
        assert row.total_trades == picked.total_trades, row.month
        assert row.net_pnl_base == pytest.approx(picked.net_pnl_base, abs=1e-9)
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
    conn = connect(db)
    migrate(conn)
    ingest_file(conn, demo, assets=ASSET_FILTER_OPTIONS)
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
    assert scoped["stats"]["fees_base"] == everything["stats"]["fees_base"]

    # The reconciliation the Annual table invites must survive an active filter.
    assert sum(y["total_trades"] for y in scoped["annual"]) == (
        scoped["annual_total"]["total_trades"]
    )
    # all_time, by contrast, is the Dashboard's own figure and does narrow.
    assert scoped["all_time"]["total_trades"] < everything["all_time"]["total_trades"]
