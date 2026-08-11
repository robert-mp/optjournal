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
from optjournal.stats import Cohort, MonthStats, cohort_data, fx_quotes, stats_data


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
