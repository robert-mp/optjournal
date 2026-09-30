"""Tests for closed-position history.

Episode boundaries and note-code parsing are the two places this module can be
subtly wrong, so both are pinned with hand-built rows where the expected answer
is computable by hand. The real archived statements then act as an end-to-end
oracle: SIVE is a genuine round trip whose realized P&L IBKR states
independently, and it is followed by a re-entry three days later -- exactly the
case naive per-contract grouping gets wrong.
"""

from __future__ import annotations

import sqlite3

import pytest
from conftest import LIVE_STATEMENTS, STATEMENTS, add_statement, connect_migrated

from optjournal.history import (
    NON_POSITION_CATEGORIES,
    Episode,
    build_history,
    disposition_of,
    split_notes,
)
from optjournal.ingest import ASSET_FILTER_ALL, ingest_file


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    """A journal holding only the statement row the trade builders hang off."""
    c = connect_migrated(tmp_path / "history.db")
    add_statement(c, from_date="2026-01-01")
    return c


_TRADE_SQL = (
    "INSERT INTO trades (trade_id, ib_exec_id, transaction_id, ib_order_id,"
    " account_id, trade_date, date_time, asset_category, symbol, conid,"
    " open_close, notes, quantity, trade_price, currency, fx_rate_to_base,"
    " proceeds, proceeds_base, ib_commission, ib_commission_base,"
    " fifo_pnl_realized, fifo_pnl_realized_base, raw, source_file, first_seen_at)"
    " VALUES (?,?,?,?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'USD', 1.0,"
    " ?, ?, ?, ?, ?, ?, '{}', ?, 'now')"
)


def add_trade(
    conn,
    tid: str,
    *,
    conid: str = "C1",
    symbol: str = "OPT1",
    date: str = "2026-03-01",
    open_close: str = "O",
    qty: int = 1,
    price: float = 1.0,
    notes: str | None = None,
    commission: float = -1.0,
    realized: float = 0.0,
    asset: str = "OPT",
    account_id: str = "U1",
    source_file: str = "t.xml",
) -> None:
    proceeds = -qty * price * 100
    conn.execute(
        _TRADE_SQL,
        (tid, f"e{tid}", f"x{tid}", f"o{tid}", account_id, date,
         f"{date} 10:00:00", asset, symbol, conid, open_close, notes, qty, price,
         proceeds, proceeds, commission, commission, realized, realized,
         source_file),
    )


def add_snapshot(conn, conid: str, *, position: int, symbol: str = "OPT1",
                 date: str = "2026-12-31", asset: str = "OPT",
                 cost_basis: float | None = None, account_id: str = "U1",
                 source_file: str = "t.xml") -> None:
    conn.execute(
        "INSERT INTO position_snapshots (report_date, conid, account_id, symbol,"
        " asset_category, position, cost_basis_money, currency, fx_rate_to_base,"
        " raw, source_file, ingested_at)"
        " VALUES (?,?, ?, ?, ?, ?, ?, 'USD', 1.0, '{}', ?, 'now')",
        (date, conid, account_id, symbol, asset, position, cost_basis, source_file),
    )


def add_nav(conn, date: str, *, stock: float, options: float,
            account_id: str = "U1") -> None:
    """One day of IBKR's NAV breakdown (EquitySummaryByReportDateInBase)."""
    conn.execute(
        "INSERT INTO equity_summaries (report_date, account_id, currency,"
        " cash_base, stock_base, options_base, total_base, raw, source_file,"
        " ingested_at) VALUES (?, ?, 'EUR', 1000, ?, ?, ?, '{}', 't.xml', 'now')",
        (date, account_id, stock, options, 1000 + stock + options),
    )


# ------------------------------------------------------------ note code parsing


def test_split_notes_splits_on_semicolon():
    assert split_notes("AFx;P") == ("AFx", "P")
    assert split_notes(None) == ()
    assert split_notes("") == ()


def test_autofx_is_not_read_as_assignment():
    """'A' is a substring of 'AFx' and 'Adj'. Exact-token matching is required."""
    assert disposition_of("AFx") is None
    assert disposition_of("AFx;P") is None
    assert disposition_of("Adj") is None
    assert disposition_of("A") == "ASSIGNED"


def test_tax_lot_method_is_not_a_disposition():
    """SL is 'Specific Lot tax lot-matching method', not a closure type."""
    assert disposition_of("SL") is None
    assert disposition_of("LI") is None
    assert disposition_of("P") is None


def test_dispositions_recognised():
    assert disposition_of("Ep") == "EXPIRED"
    assert disposition_of("Ex") == "EXERCISED"
    assert disposition_of("AEx") == "EXERCISED"
    assert disposition_of("MEx") == "EXERCISED"


def test_assignment_outranks_expiry_when_both_present():
    assert disposition_of("Ep;A") == "ASSIGNED"


# ------------------------------------------------------------ episode boundaries


def test_round_trip_closes(conn):
    add_trade(conn, "1", open_close="O", qty=3, price=5.0)
    add_trade(conn, "2", open_close="C", qty=-3, price=2.0,
              date="2026-03-11", realized=900.0)
    report = build_history(conn)
    assert len(report.episodes) == 1
    ep = report.episodes[0]
    assert ep.status == "CLOSED"
    assert ep.net_qty == 0
    assert ep.realized_pnl == 900.0
    assert ep.holding_days == 10
    assert ep.contracts == 3


def test_contracts_is_the_largest_position_held_not_the_opens_summed(conn):
    """Short 2, buy 1 back, sell 1 again, buy 2 back: never more than 2 held.

    `contracts` summed the opening fills (2 + 1) and reported 3, so a trader who
    scaled out and back in read as having carried a bigger position than they
    ever did, and the 0DTE cohort's contract count inherited it.
    """
    add_trade(conn, "1", open_close="O", qty=-2, date="2026-03-01")
    add_trade(conn, "2", open_close="C", qty=1, date="2026-03-02", realized=99.0)
    add_trade(conn, "3", open_close="O", qty=-1, date="2026-03-03")
    add_trade(conn, "4", open_close="C", qty=2, date="2026-03-04", realized=198.0)
    (ep,) = build_history(conn).episodes
    assert ep.status == "CLOSED"
    assert ep.contracts == 2


def test_contracts_of_a_position_still_open_is_what_it_reached(conn):
    """Bought 1, then 4 more, sold 2: at its largest the position was 5."""
    add_trade(conn, "1", open_close="O", qty=1, date="2026-03-01")
    add_trade(conn, "2", open_close="O", qty=4, date="2026-03-02")
    add_trade(conn, "3", open_close="C", qty=-2, date="2026-03-03", realized=10.0)
    (ep,) = build_history(conn).episodes
    assert ep.net_qty == 3
    assert ep.contracts == 5


def test_a_fill_through_zero_closes_the_position_and_opens_the_opposite(conn):
    """Long 2, then SELL 3 in one fill: IBKR marks it `C;O` and realises the long.

    Only a bare `C` counted as a close, so the fill was read as an opening sale:
    the long never went flat, the +198 IBKR realised in September waited inside
    one open episode, and the whole outcome later landed in October as a single
    +347. The fill is split at zero: its closing 2 finish the long, which takes
    all of the realised P&L, and its leftover 1 opens the short. Commission and
    proceeds divide by quantity.
    """
    add_trade(conn, "1", open_close="O", qty=2, date="2026-09-01")
    add_trade(conn, "2", open_close="C;O", qty=-3, price=2.0, date="2026-09-10",
              realized=198.0, commission=-3.0)
    report = build_history(conn)
    (long_leg,), (short,) = report.closed, report.open
    assert long_leg.status == "CLOSED"
    assert long_leg.realized_pnl == pytest.approx(198.0)
    assert long_leg.closed_at == "2026-09-10 10:00:00"
    assert long_leg.contracts == 2
    assert long_leg.commission == pytest.approx(-1.0 - 2.0)
    assert long_leg.proceeds == pytest.approx(-200.0 + 400.0)
    assert short.net_qty == -1
    assert short.opened_at == "2026-09-10 10:00:00"
    assert short.entry_outside_window is False
    assert short.realized_pnl == 0.0
    assert short.commission == pytest.approx(-1.0)
    assert short.proceeds == pytest.approx(200.0)
    assert short.trade_ids == ["2"]

    add_trade(conn, "3", open_close="C", qty=1, price=0.5, date="2026-10-05",
              realized=149.0)
    closed = sorted(build_history(conn).closed, key=lambda e: e.closed_at)
    assert [(e.closed_at[:10], e.realized_pnl, e.contracts) for e in closed] == [
        ("2026-09-10", 198.0, 2), ("2026-10-05", 149.0, 1)]


def test_a_fill_marked_close_and_open_that_stops_at_zero_is_not_split(conn):
    """The split needs the fill to go THROUGH zero. One that only reaches it
    closes the position and opens nothing, whatever the marker says."""
    add_trade(conn, "1", open_close="O", qty=2, date="2026-09-01")
    add_trade(conn, "2", open_close="C;O", qty=-2, date="2026-09-10", realized=50.0)
    (ep,) = build_history(conn).episodes
    assert ep.status == "CLOSED" and ep.net_qty == 0


def test_reentry_after_close_is_a_separate_episode(conn):
    """The SIVE shape: open, fully close, then re-open the same contract."""
    add_trade(conn, "1", open_close="O", qty=2, date="2026-03-01")
    add_trade(conn, "2", open_close="C", qty=-2, date="2026-03-05", realized=100.0)
    add_trade(conn, "3", open_close="O", qty=5, date="2026-03-09")
    add_snapshot(conn, "C1", position=5)

    report = build_history(conn)
    assert len(report.episodes) == 2, "re-entry must not fuse with the closed trip"
    closed, still_open = report.closed, report.open
    assert len(closed) == 1 and len(still_open) == 1
    assert closed[0].realized_pnl == 100.0
    assert still_open[0].net_qty == 5
    assert still_open[0].realized_pnl == 0.0


def test_the_same_contract_in_two_accounts_is_two_episodes(conn):
    """A position exists within an account, so the same conid in two accounts is
    two positions -- not one fused round trip.

    The episode walk grouped by conid alone, so two accounts trading the same
    contract merged into a single episode whose quantities cancelled and whose
    P&L summed. Demonstrated on hand-built rows: a +200 round trip in one account
    and a -150 in another became one CLOSED episode reporting +50, an outcome
    neither account had. Nothing was wrong on this journal because it holds one
    account, which is exactly why the merge was silent.
    """
    add_statement(conn, source_file="b.xml", account_id="U2")
    # U1: a winning round trip in contract C1.
    add_trade(conn, "1", conid="C1", open_close="O", qty=-3, price=3.0,
              account_id="U1")
    add_trade(conn, "2", conid="C1", open_close="C", qty=3, price=1.0,
              date="2026-03-05", realized=200.0, account_id="U1")
    # U2: a losing round trip in the SAME contract.
    add_trade(conn, "3", conid="C1", open_close="O", qty=-2, price=2.0,
              date="2026-03-02", account_id="U2", source_file="b.xml")
    add_trade(conn, "4", conid="C1", open_close="C", qty=2, price=3.5,
              date="2026-03-06", realized=-150.0, account_id="U2",
              source_file="b.xml")

    report = build_history(conn)
    assert len(report.episodes) == 2, "the two accounts' trades were fused"
    by_account = {e.account_id: e for e in report.episodes}
    assert by_account["U1"].realized_pnl == 200.0
    assert by_account["U2"].realized_pnl == -150.0
    assert all(e.status == "CLOSED" for e in report.episodes)


def test_a_snapshot_is_matched_to_its_own_account(conn):
    """The snapshot lookup keys on (broker, account_id, conid) too.

    A conid-only key would let one account's holding decide the open/closed
    verdict for another's: U2 still holds C1, so a conid-keyed lookup would mark
    U1's fully-closed round trip in C1 as OPEN.
    """
    add_statement(conn, source_file="b.xml", account_id="U2")
    add_trade(conn, "1", conid="C1", open_close="O", qty=-2, account_id="U1")
    add_trade(conn, "2", conid="C1", open_close="C", qty=2, date="2026-03-05",
              realized=50.0, account_id="U1")
    add_trade(conn, "3", conid="C1", open_close="O", qty=-4, date="2026-03-02",
              account_id="U2", source_file="b.xml")
    # Only U2 still holds C1 at the snapshot.
    conn.execute(
        "INSERT INTO position_snapshots (report_date, conid, account_id, symbol,"
        " asset_category, position, currency, fx_rate_to_base, raw, source_file,"
        " ingested_at) VALUES ('2026-12-31','C1','U2','OPT1','OPT',-4,'USD',1.0,"
        " '{}','b.xml','now')"
    )
    report = build_history(conn)
    u1 = next(e for e in report.episodes if e.account_id == "U1")
    assert u1.status == "CLOSED", "U1's round trip must not be held open by U2's position"


def test_partial_close_stays_open(conn):
    add_trade(conn, "1", open_close="O", qty=5)
    add_trade(conn, "2", open_close="C", qty=-2, date="2026-03-05", realized=40.0)
    add_snapshot(conn, "C1", position=3)
    report = build_history(conn)
    assert len(report.episodes) == 1
    assert report.episodes[0].status == "OPEN"
    assert report.episodes[0].net_qty == 3


def test_expiry_is_recognised_as_disposition(conn):
    add_trade(conn, "1", open_close="O", qty=1)
    add_trade(conn, "2", open_close="C", qty=-1, date="2026-03-20",
              notes="Ep", realized=-250.0)
    report = build_history(conn)
    assert report.episodes[0].status == "EXPIRED"


def test_assignment_is_recognised_as_disposition(conn):
    add_trade(conn, "1", open_close="O", qty=-1)
    add_trade(conn, "2", open_close="C", qty=1, date="2026-03-20",
              notes="A;P", realized=-80.0)
    assert build_history(conn).episodes[0].status == "ASSIGNED"


# -------------------------------------------------- entry outside the archive


def test_close_only_episode_closed_when_snapshot_is_silent(conn):
    """Entry predates the archive; absence from the snapshot proves it is flat."""
    add_trade(conn, "1", open_close="C", qty=-1, date="2026-03-02", realized=500.0)
    add_snapshot(conn, "OTHER", position=1, symbol="OTHER")
    ep = build_history(conn).episodes[0]
    assert ep.entry_outside_window is True
    assert ep.status == "CLOSED"
    assert ep.realized_pnl == 500.0
    assert ep.holding_days is None, "no entry date means no holding period"


def test_close_only_episode_open_when_snapshot_still_holds_it(conn):
    add_trade(conn, "1", open_close="C", qty=-1, date="2026-03-02", realized=500.0)
    add_snapshot(conn, "C1", position=4)
    ep = build_history(conn).episodes[0]
    assert ep.entry_outside_window is True
    assert ep.status == "OPEN", "snapshot still lists it, so it is not flat"


def test_snapshot_only_position_is_reported(conn):
    """A held contract with no fills at all must still appear in the open book."""
    add_snapshot(conn, "C9", position=1, symbol="LONGCALL", cost_basis=3000.81)
    report = build_history(conn)
    assert len(report.episodes) == 1
    ep = report.episodes[0]
    assert ep.snapshot_only is True
    assert ep.status == "OPEN"
    assert ep.net_qty == 1
    assert ep.cost_basis == pytest.approx(3000.81)
    assert ep.open_fills == 0


def test_snapshot_only_not_duplicated_when_trades_exist(conn):
    add_trade(conn, "1", open_close="O", qty=1)
    add_snapshot(conn, "C1", position=1)
    assert len(build_history(conn).episodes) == 1


# ------------------------------------------------------------ the book's date
#
# IBKR's OpenPositions lists only what is held, and every row of one statement
# carries the same reportDate (checked across the real archive, STK and OPT
# alike). So the day the option book goes flat there is simply no OPT row, and
# reading "the newest date that had an option" falls back to a stale book.


def _current_option_conids(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT conid FROM current_option_positions")}


def test_an_option_missing_from_the_newest_book_is_flat(conn):
    """`pnl/s_empty_book.py`: a LEAP held from before the archive is sold, and
    the next statement lists only the stock. The LEAP stayed OPEN with its
    +1500 missing, and stayed on the Positions tab."""
    add_snapshot(conn, "LEAP", position=1, date="20260901")
    add_snapshot(conn, "STK1", position=10, date="20260901", asset="STK",
                 symbol="STK1")
    add_trade(conn, "1", conid="LEAP", open_close="C", qty=-1, date="2026-09-15",
              realized=1500.0)
    add_snapshot(conn, "STK1", position=10, date="20260916", asset="STK",
                 symbol="STK1")
    report = build_history(conn)
    (leap,) = report.episodes
    assert leap.status == "CLOSED"
    assert report.total_realized_base == 1500.0
    assert report.snapshot_date == "20260916"
    assert _current_option_conids(conn) == set()


def test_an_empty_book_is_flat_when_the_nav_says_nothing_is_held(conn):
    """Everything sold: the statement's OpenPositions section is present but
    empty, so no position row exists for that day in ANY category. IBKR's NAV
    breakdown for the day (no stock, no options) is what says the book is empty
    rather than unreported."""
    add_snapshot(conn, "LEAP", position=1, date="20260901")
    add_nav(conn, "20260901", stock=0, options=4500)
    add_trade(conn, "1", conid="LEAP", open_close="C", qty=-1, date="2026-09-15",
              realized=1500.0)
    add_nav(conn, "20260916", stock=0, options=0)
    report = build_history(conn)
    assert [e.status for e in report.episodes] == ["CLOSED"]
    assert report.snapshot_date == "20260916"
    assert _current_option_conids(conn) == set()


def test_a_statement_that_reports_no_positions_is_silent_not_flat(conn):
    """A query without the OpenPositions section leaves no row either, but the
    account still holds things, and its NAV says so. Reading that silence as an
    empty book would close every position held from before the archive."""
    add_snapshot(conn, "C1", position=-1, date="20260901")
    add_nav(conn, "20260901", stock=0, options=-300)
    add_trade(conn, "1", open_close="C", qty=1, date="2026-08-25", realized=20.0)
    add_nav(conn, "20260916", stock=0, options=-280)
    report = build_history(conn)
    assert [e.status for e in report.episodes] == ["OPEN"]
    assert report.snapshot_date == "20260901"
    assert _current_option_conids(conn) == {"C1"}


def test_an_options_only_journal_reads_its_option_book_flat_from_the_nav(conn):
    """A journal ingested with `--assets OPT` stores no stock position row, so
    the day its options go flat has no position row in any category and the NAV
    still prices the stock this journal does not track. Read together, the two
    columns left the option book on its last option date forever: a LEAP sold
    that day stayed OPEN, the Positions tab listed it, and allocation counted it.
    Each column speaks for its own category, so `options_base = 0` is a flat
    option book whatever the stock figure."""
    add_snapshot(conn, "LEAP", position=1, date="20260901")
    add_nav(conn, "20260901", stock=83000, options=4500)
    add_nav(conn, "20260916", stock=83000, options=0)
    report = build_history(conn, asset_category="OPT")
    assert report.episodes == [], "nothing held, and no fill to make an episode"
    assert report.snapshot_date == "20260916"


def test_a_stock_book_is_flat_when_the_nav_prices_no_stock(conn):
    """The same rule for the other column, which is the equities Dashboard's."""
    add_snapshot(conn, "S1", position=5, symbol="S1", asset="STK",
                 date="20260901")
    add_nav(conn, "20260901", stock=900, options=4500)
    add_nav(conn, "20260916", stock=0, options=4500)
    report = build_history(conn, asset_category="STK")
    assert report.episodes == []
    assert report.snapshot_date == "20260916"


def test_the_book_is_read_in_one_pass_not_once_per_snapshot_row(conn):
    """The book was a correlated scalar subquery, so its UNION ran once for every
    snapshot row it filtered: quadratic, and `/api/state` went from 0.2s to 35s
    on the rows two more years of daily statements bring. Asserted with SQLite's
    own plan, because the cost is invisible in a small fixture: the books are
    scanned once, into a materialised subquery the outer rows join against."""
    from optjournal.history import book_join_sql

    add_snapshot(conn, "A1", position=1, date="20260901")
    joined = f"SELECT p.* FROM position_snapshots p{book_join_sql('OPT')}"
    plan = [str(r[3]) for r in conn.execute(f"EXPLAIN QUERY PLAN {joined}")]
    assert not any("CORRELATED" in step.upper() for step in plan), plan
    assert any("SCAN" in step.upper() and "position_snapshots" in step for step in plan), plan
    for query in (joined,
                  f"SELECT p.* FROM position_snapshots p{book_join_sql()}",
                  "SELECT * FROM current_option_positions"):
        steps = [str(r[3]).upper() for r in conn.execute(f"EXPLAIN QUERY PLAN {query}")]
        assert not any("CORRELATED" in s for s in steps), (query, steps)


def test_the_positions_view_and_the_episode_walk_read_the_same_book(conn):
    """`db.current_option_positions` spells `history.book_dates_sql('OPT')` again,
    since a view cannot import it. Every case above, in three accounts at once."""
    from optjournal.history import _held

    for account in ("U2", "U3", "U4"):
        add_statement(conn, source_file=f"{account}.xml", account_id=account)
    # U1: options sold, the stock still held.
    add_snapshot(conn, "A1", position=1, date="20260901")
    add_snapshot(conn, "S1", position=5, date="20260916", asset="STK", symbol="S1")
    # U2: its statements lag, and it still holds its option.
    add_snapshot(conn, "B1", position=-1, date="20260901", account_id="U2",
                 source_file="U2.xml")
    # U3: everything sold, and its NAV says so.
    add_snapshot(conn, "C1", position=2, date="20260901", account_id="U3",
                 source_file="U3.xml")
    add_nav(conn, "20260916", stock=0, options=0, account_id="U3")
    # U4: options sold, the stock untracked (`--assets OPT`), so only its NAV
    # says the option book is flat, and it still prices the stock.
    add_snapshot(conn, "D1", position=1, date="20260901", account_id="U4",
                 source_file="U4.xml")
    add_nav(conn, "20260916", stock=5000, options=0, account_id="U4")
    held, _ = _held(conn, "OPT")
    assert {conid for _, _, conid in held} == _current_option_conids(conn) == {"B1"}


def test_every_category_at_once_is_each_category_read_at_its_own_book(conn):
    """`history --assets ALL` read the mixed scope flat only where the NAV priced
    stock AND options at nothing, so an options-only journal whose options went
    flat by NAV alone (U2), and an account that sold its stock on a day whose
    statement had no OpenPositions (U5), kept a position the per-category
    readings, the Positions tab and the allocation all called gone. Each row is
    now read at its own category's book, so the whole is the union of the parts,
    including a category the NAV does not price (U6's fund), which no NAV row may
    empty."""
    from optjournal.history import _held, book_date

    for account in ("U2", "U3", "U5", "U6"):
        add_statement(conn, source_file=f"{account}.xml", account_id=account,
                      asset_filter="ALL")

    def snap(conid, account, date, asset):
        add_snapshot(conn, conid, position=1, date=date, asset=asset, symbol=conid,
                     account_id=account,
                     source_file="t.xml" if account == "U1" else f"{account}.xml")

    # U1: options sold on 0916, the stock still listed.
    snap("U1OPT", "U1", "20260901", "OPT")
    snap("U1STK", "U1", "20260901", "STK")
    snap("U1STK", "U1", "20260916", "STK")
    add_nav(conn, "20260916", stock=900, options=0)
    # U2: an --assets OPT journal, whose options go flat by NAV alone.
    snap("U2OPT", "U2", "20260901", "OPT")
    add_nav(conn, "20260916", stock=5000, options=0, account_id="U2")
    # U3: a 0916 statement without OpenPositions, the NAV pricing both.
    snap("U3OPT", "U3", "20260901", "OPT")
    snap("U3STK", "U3", "20260901", "STK")
    add_nav(conn, "20260916", stock=900, options=100, account_id="U3")
    # U5: the stock sold on 0916, no OpenPositions that day.
    snap("U5OPT", "U5", "20260901", "OPT")
    snap("U5STK", "U5", "20260901", "STK")
    add_nav(conn, "20260916", stock=0, options=100, account_id="U5")
    # U6: a fund, which the NAV has no column for, beside nothing else held.
    snap("U6FUND", "U6", "20260901", "FUND")
    add_nav(conn, "20260916", stock=0, options=0, account_id="U6")

    def held(scope):
        return {conid for _, _, conid in _held(conn, scope)[0]}

    parts = {scope: held(scope) for scope in ("OPT", "STK", "FUND")}
    assert parts == {"OPT": {"U3OPT", "U5OPT"}, "STK": {"U1STK", "U3STK"},
                     "FUND": {"U6FUND"}}
    assert held(None) == set().union(*parts.values())
    assert {e.conid for e in build_history(conn, asset_category=None).open} == (
        set().union(*parts.values()))
    assert book_date(conn, None) == max(
        book_date(conn, scope) for scope in ("OPT", "STK", "FUND")) == "20260916"


def test_each_account_is_read_at_its_own_newest_date(conn):
    """U2's statements lag U1's. Measured against U1's newer date, U2's held
    position vanished from the book, and its pre-archive episode read CLOSED."""
    add_statement(conn, source_file="b.xml", account_id="U2")
    add_snapshot(conn, "C1", position=-1, date="20260916")
    add_snapshot(conn, "C2", position=-1, date="20260901", account_id="U2",
                 source_file="b.xml")
    add_trade(conn, "1", conid="C2", open_close="C", qty=1, date="2026-08-25",
              realized=20.0, account_id="U2", source_file="b.xml")
    report = build_history(conn)
    u2 = next(e for e in report.episodes if e.account_id == "U2" and not e.snapshot_only)
    assert u2.status == "OPEN"
    assert _current_option_conids(conn) == {"C1", "C2"}


# ----------------------------------------------------------------- exclusions


def test_a_residual_position_is_not_flat():
    """`_flat` decides whether a round trip is CLOSED, and nothing tested it.

    Found by mutation: replacing the epsilon with `abs(qty) < 0.5` -- so 0.4
    shares still held counts as flat -- passed all 579 tests. That defect books a
    partially-closed lot as a completed round trip, which means its P&L counts in
    the period and the remaining position disappears from the open book. Both
    halves of "nothing counts until the position is flat" break at once, silently.

    The epsilon exists for float dust on FRACTIONAL lots (a dividend
    reinvestment buys 1.79 shares), so the test has to pin both sides: dust is
    flat, a real fraction of a share is not.
    """
    from optjournal.history import _FLAT_EPS, _flat

    assert _flat(0) and _flat(0.0)
    # Integral quantities are exact -- options cannot leave dust.
    assert not _flat(1) and not _flat(-1)
    # Dust from float arithmetic on a fractional lot is not a position.
    assert _flat(_FLAT_EPS / 10) and _flat(-_FLAT_EPS / 10)
    # A real residual IS a position, however small a share fraction it is.
    assert not _flat(0.4), "0.4 shares held is not flat"
    assert not _flat(-0.4), "a short residual is not flat"
    assert not _flat(0.01), "a hundredth of a share is still a position"
    # The epsilon must stay far below any quantity a broker can report.
    assert _FLAT_EPS < 1e-4, "epsilon wide enough to swallow a real residual"


def test_a_fractional_residual_leaves_the_episode_open(conn):
    """The same property end to end, through `build_history`.

    The unit test above pins the predicate; this pins the consequence, because
    that is what a reader cares about: sell all but a fraction of a lot and the
    episode must stay OPEN and contribute no realised P&L.
    """
    add_trade(conn, "1", conid="S1", symbol="SIVE", asset="STK",
              open_close="O", qty=10.0, price=100.0)
    add_trade(conn, "2", conid="S1", symbol="SIVE", asset="STK",
              open_close="C", qty=-9.6, price=110.0, date="2026-03-10",
              realized=96.0)
    add_snapshot(conn, "S1", position=0.4, symbol="SIVE", asset="STK")

    report = build_history(conn, asset_category="STK")
    assert len(report.episodes) == 1
    episode = report.episodes[0]
    assert episode.status == "OPEN", (
        "0.4 shares are still held, so the round trip is not complete"
    )
    assert episode.net_qty == pytest.approx(0.4)
    assert report.closed == [], "a partial close must not count as an outcome"


def test_currency_conversions_are_not_positions(conn):
    """FX rows carry no openCloseIndicator, so they would fuse into one episode."""
    for i in range(4):
        add_trade(conn, str(i), conid="FX1", symbol="EUR.USD", asset="CASH",
                  open_close=None, qty=1000, commission=0.0)
    assert "CASH" in NON_POSITION_CATEGORIES
    assert build_history(conn, asset_category=None).episodes == []


def test_asset_scope_filters(conn):
    add_trade(conn, "1", conid="S1", symbol="AAPL", asset="STK", qty=10)
    add_trade(conn, "2", conid="C1", symbol="OPT1", asset="OPT", qty=1)
    assert len(build_history(conn, asset_category="OPT").episodes) == 1
    assert len(build_history(conn, asset_category="STK").episodes) == 1
    assert len(build_history(conn, asset_category=None).episodes) == 2


def test_the_snapshot_and_the_trade_query_share_one_category_predicate():
    """Both must scope identically, or an episode is judged against the wrong book.

    `_held` reads the newest position snapshot to decide whether a pre-archive
    episode is still open; `build_history` reads the trades. If one applied the
    `NON_POSITION_CATEGORIES` exclusion and the other did not, a position would be
    reconstructed from trades the snapshot query never considered -- and the
    symptom is a wrong open/closed verdict, not an error.

    The predicate was written out twice and the copies had already diverged in
    spelling, which is how one of them ends up fixed alone. Asserted on the
    helper's own output, including that `None` yields the exclusion rather than
    an empty clause: an empty string here would silently widen both queries to
    include currency conversions.
    """
    from optjournal.history import _position_scope_where

    narrow, params = _position_scope_where("OPT")
    assert narrow == "WHERE asset_category = ?" and params == ("OPT",)

    wide, wide_params = _position_scope_where(None)
    assert "NOT IN" in wide, "None must exclude non-position categories, not widen"
    assert wide_params == tuple(sorted(NON_POSITION_CATEGORIES))
    # One placeholder per excluded category, or sqlite raises on the bind.
    assert wide.count("?") == len(NON_POSITION_CATEGORIES)


# ------------------------------------------------------------------- totals


def test_realized_is_not_double_counted_with_commission(conn):
    """IBKR's realized P&L already nets commission; the report must not re-net."""
    add_trade(conn, "1", open_close="O", qty=1, commission=-2.0)
    add_trade(conn, "2", open_close="C", qty=-1, date="2026-03-05",
              commission=-2.0, realized=96.0)
    report = build_history(conn)
    ep = report.episodes[0]
    assert ep.net_of_commission is True
    assert ep.realized_pnl == 96.0
    assert report.total_realized_base == 96.0
    assert report.total_commission_base == pytest.approx(4.0)


def test_win_rate(conn):
    add_trade(conn, "1", conid="A", open_close="O", qty=1)
    add_trade(conn, "2", conid="A", open_close="C", qty=-1,
              date="2026-03-05", realized=10.0)
    add_trade(conn, "3", conid="B", open_close="O", qty=1)
    add_trade(conn, "4", conid="B", open_close="C", qty=-1,
              date="2026-03-05", realized=-4.0)
    report = build_history(conn)
    assert (report.wins, report.losses) == (1, 1)
    assert report.win_rate == pytest.approx(50.0)


def test_empty_database_yields_empty_report(conn):
    report = build_history(conn)
    assert report.episodes == []
    assert report.snapshot_date is None
    assert report.win_rate is None


# ----------------------------------------------- end-to-end against real data


@pytest.mark.skipif(not LIVE_STATEMENTS, reason="needs the optional live corpus")
def test_real_sive_round_trip(tmp_path):
    """SIVE: bought twice, fully sold, re-entered. IBKR states the P&L itself.

    Basis 62,592.00 + 50.46 opening commission = 62,642.46; proceeds 131,400
    less 73.86 closing commission = 131,326.14; difference 68,683.68, which is
    exactly IBKR's fifoPnlRealized. That makes this an independent oracle.
    """
    conn = connect_migrated(tmp_path / "all.db")
    for path in LIVE_STATEMENTS:
        ingest_file(conn, path, assets=ASSET_FILTER_ALL)

    report = build_history(conn, asset_category=None)
    sive_closed = [e for e in report.closed if e.symbol == "SIVE"]
    assert len(sive_closed) == 1, "expected exactly one closed SIVE round trip"
    ep = sive_closed[0]
    assert ep.realized_pnl == pytest.approx(68683.68, abs=0.01)
    assert ep.opened_at.startswith("2026-04-23")
    assert ep.closed_at.startswith("2026-05-22")
    assert ep.holding_days == 29
    assert ep.contracts == 1800

    sive_open = [e for e in report.open if e.symbol == "SIVE"]
    assert len(sive_open) == 1, "the re-entry must be a separate open episode"
    assert sive_open[0].opened_at.startswith("2026-05-25")


@pytest.mark.skipif(not STATEMENTS, reason="no archived statements")
def test_real_option_book_matches_snapshot(tmp_path):
    """History's open book must agree with the position snapshot, including the
    long call that has no opening trade anywhere in the archive."""
    conn = connect_migrated(tmp_path / "opt.db")
    for path in STATEMENTS:
        ingest_file(conn, path)

    report = build_history(conn)
    held = {
        r["conid"]
        for r in conn.execute("SELECT conid FROM current_option_positions")
    }
    assert {e.conid for e in report.open} == held
    snapshot_only = [e for e in report.open if e.snapshot_only]
    assert len(snapshot_only) == 1
    snapshot_basis = conn.execute(
        "SELECT ABS(cost_basis_money) FROM current_option_positions"
        " WHERE conid = ?",
        (snapshot_only[0].conid,),
    ).fetchone()[0]
    assert snapshot_only[0].cost_basis == pytest.approx(snapshot_basis, abs=0.01)


# --- re-entry after a pre-archive close ---------------------------------------
#
# The snapshot is keyed by conid, not by episode. A contract opened before the
# archive, closed inside it, then re-entered appears held -- because of the
# re-entry. Deciding the *closed* episode from that signal marks a completed
# round trip OPEN, drops its realised P&L from the totals, and counts the
# contract twice in the open book. The re-entry is itself proof the earlier
# position went flat, so it must override the snapshot.


def test_reentry_closes_the_prewindow_episode(conn):
    """Pre-archive entry, closed, then re-entered and still held."""
    add_trade(conn, "1", open_close="C", qty=3, realized=500.0, date="2026-03-01")
    add_trade(conn, "2", open_close="O", qty=-3, date="2026-03-05")
    add_snapshot(conn, "C1", position=-3)

    report = build_history(conn)
    assert len(report.episodes) == 2

    closed = [e for e in report.episodes if e.is_closed]
    open_eps = [e for e in report.episodes if not e.is_closed]
    assert len(closed) == 1, "the pre-archive round trip must be CLOSED"
    assert len(open_eps) == 1, "the re-entry must be the only OPEN episode"

    assert closed[0].entry_outside_window is True
    assert closed[0].close_fills == 1
    assert open_eps[0].entry_outside_window is False
    assert open_eps[0].open_fills == 1


def test_reentry_keeps_realized_pnl_in_the_total(conn):
    """The bug's real cost: realised P&L silently vanishing from the total."""
    add_trade(conn, "1", open_close="C", qty=3, realized=500.0, date="2026-03-01")
    add_trade(conn, "2", open_close="O", qty=-3, date="2026-03-05")
    add_snapshot(conn, "C1", position=-3)

    report = build_history(conn)
    assert report.total_realized_base == 500.0
    assert report.wins == 1


def test_reentry_does_not_double_count_the_contract(conn):
    add_trade(conn, "1", open_close="C", qty=3, realized=500.0, date="2026-03-01")
    add_trade(conn, "2", open_close="O", qty=-3, date="2026-03-05")
    add_snapshot(conn, "C1", position=-3)

    report = build_history(conn)
    assert len(report.open) == 1, "C1 must appear once in the open book"


def test_prewindow_close_without_reentry_still_defers_to_snapshot(conn):
    """The behaviour that must NOT regress.

    With no re-entry there is no independent proof of flatness, so the
    snapshot remains the deciding signal -- present means still open.
    """
    add_trade(conn, "1", open_close="C", qty=1, realized=50.0, date="2026-03-01")
    add_snapshot(conn, "C1", position=-2)

    report = build_history(conn)
    assert len(report.episodes) == 1
    assert not report.episodes[0].is_closed, "still in the snapshot, so still open"


def test_prewindow_close_absent_from_snapshot_is_closed(conn):
    add_trade(conn, "1", open_close="C", qty=1, realized=50.0, date="2026-03-01")

    report = build_history(conn)
    assert len(report.episodes) == 1
    assert report.episodes[0].is_closed
    assert report.total_realized_base == 50.0


# --- the pre-archive quantity, from the snapshot --------------------------------
#
# A pre-archive holding was only noticed when the contract's FIRST fill in the
# archive was a close. Scaling in or out first hid it, and the episode walked from
# zero: it went flat too early, or never. The snapshot says how much is held, so
# the difference between it and the fills up to the snapshot's date is what was
# held before the archive began, and the walk starts there.


def _book_elsewhere(conn, date: str = "20260930") -> None:
    """A snapshot row on another contract, so the book has a date and the
    contract under test is known to be absent from it."""
    add_snapshot(conn, "OTHER", position=1, symbol="OTHER", date=date)


def test_a_pre_archive_position_scaled_out_and_back_in_closes_once(conn):
    """`pnl/s_prearchive_scale.py`: long 2 from before the archive; sell 1, buy 1
    back, sell 2. The buy-back read as a re-entry, so the walk closed a +99
    episode and left a -1 phantom short OPEN holding the final +248."""
    add_trade(conn, "1", open_close="C", qty=-1, price=3.0, date="2026-09-02",
              realized=99.0)
    add_trade(conn, "2", open_close="O", qty=1, price=2.5, date="2026-09-03")
    add_trade(conn, "3", open_close="C", qty=-2, price=4.0, date="2026-09-20",
              realized=248.0)
    _book_elsewhere(conn)
    report = build_history(conn)
    (ep,) = [e for e in report.episodes if e.conid == "C1"]
    assert ep.status == "CLOSED"
    assert ep.realized_pnl == pytest.approx(347.0)
    assert ep.entry_outside_window is True and ep.opened_at is None
    assert ep.contracts == 2
    from optjournal.stats import month_stats
    september = month_stats(conn, "2026-09", report=report)
    assert (september.net_pnl.base, september.wins, september.open_episodes) == (
        347.0, 1, 1), "the one open episode is OTHER, the snapshot-only row"


def test_a_pre_archive_position_added_to_then_closed_is_one_round_trip(conn):
    """`pnl/s_prearchive_add.py`: long 1 from before the archive, buy 1, sell 2.
    Walked from zero it never went flat: a phantom -1 short, +398 missing."""
    add_trade(conn, "1", open_close="O", qty=1, price=2.0, date="2026-09-02")
    add_trade(conn, "2", open_close="C", qty=-2, price=4.0, date="2026-09-20",
              realized=398.0)
    _book_elsewhere(conn)
    (ep,) = [e for e in build_history(conn).episodes if e.conid == "C1"]
    assert (ep.status, ep.net_qty, ep.realized_pnl) == ("CLOSED", 0, 398.0)


def test_a_pre_archive_holding_still_held_matches_the_snapshot(conn):
    """The real TSLA stock: 110 shares from before the archive and 96 bought
    since. The episode read 96 open where the account held 206."""
    add_trade(conn, "1", conid="T", symbol="TSLA", asset="STK", open_close="O",
              qty=40, date="2025-08-28")
    add_trade(conn, "2", conid="T", symbol="TSLA", asset="STK", open_close="O",
              qty=56, date="2026-09-04")
    add_snapshot(conn, "T", position=206, symbol="TSLA", asset="STK",
                 date="20260929")
    (ep,) = build_history(conn, asset_category="STK").episodes
    assert (ep.status, ep.net_qty, ep.contracts) == ("OPEN", 206, 206)
    assert ep.entry_outside_window is True
    assert ep.opened_at is None, "the position was opened before the archive"


def test_selling_a_pre_archive_holding_leaves_no_phantom(conn):
    """`pnl/tsla_sellout.py`: sell all 206 and the next statement lists no TSLA.
    Walked from 96 the sale left a -110 short OPEN."""
    add_trade(conn, "1", conid="T", symbol="TSLA", asset="STK", open_close="O",
              qty=96, date="2025-08-28")
    add_trade(conn, "2", conid="T", symbol="TSLA", asset="STK", open_close="C",
              qty=-206, date="2026-09-29", realized=5000.0)
    add_snapshot(conn, "S", position=5, symbol="S", asset="STK", date="20260929")
    report = build_history(conn, asset_category="STK")
    (ep,) = [e for e in report.episodes if e.conid == "T"]
    assert (ep.status, ep.net_qty, ep.realized_pnl) == ("CLOSED", 0, 5000.0)


def test_fills_after_the_snapshot_are_not_read_as_a_pre_archive_holding(conn):
    """Today's Trade Confirmation fills postdate the newest statement, so the
    snapshot cannot know them. Only fills up to its date are reconciled."""
    add_trade(conn, "1", open_close="O", qty=-2, date="2026-09-30")
    _book_elsewhere(conn, date="20260929")
    (ep,) = [e for e in build_history(conn).episodes if e.conid == "C1"]
    assert (ep.status, ep.net_qty, ep.entry_outside_window) == ("OPEN", -2, False)


def test_a_pre_archive_long_sold_through_zero_opens_the_short(conn):
    """The two fixes meet: long 3 from before the archive, one SELL 5 (`C;O`)."""
    add_trade(conn, "1", open_close="C;O", qty=-5, date="2026-09-10", realized=90.0)
    add_snapshot(conn, "C1", position=-2, date="20260929")
    closed, still_open = (lambda r: (r.closed, r.open))(build_history(conn))
    assert [(e.realized_pnl, e.entry_outside_window) for e in closed] == [(90.0, True)]
    assert [(e.net_qty, e.entry_outside_window) for e in still_open] == [(-2, False)]


def _sive(conn) -> None:
    """The real SIVE shape, in one account with two snapshot dates: bought and
    fully sold in the spring, re-entered afterwards and still held."""
    for tid, date, qty, close, realized in (
        ("1", "2026-04-20", 100, "O", 0.0),
        ("2", "2026-05-22", -100, "C", 600.0),
        ("3", "2026-05-26", 200, "O", 0.0),
    ):
        add_trade(conn, tid, conid="S", symbol="SIVE", asset="STK", date=date,
                  qty=qty, open_close=close, realized=realized)
    add_snapshot(conn, "S", position=200, symbol="SIVE", asset="STK",
                 date="20260901")


def test_a_share_split_after_a_closed_round_trip_keeps_it_closed(conn):
    """A 3:1 split is a corporate action, never a trade row, so only the snapshot
    sees the tripled quantity. Read as a holding from before the archive, it
    seeded the walk with 400 shares nothing had bought: the closed April-May
    round trip fused with the re-entry into one OPEN episode and its realised
    P&L left the closed totals entirely."""
    _sive(conn)
    add_snapshot(conn, "S", position=600, symbol="SIVE", asset="STK",
                 date="20260929")
    report = build_history(conn, asset_category="STK")
    assert [(e.status, e.net_qty, e.realized_pnl) for e in report.episodes] == [
        ("CLOSED", 0, 600.0), ("OPEN", 200, 0.0)]
    assert report.total_realized_base == 600.0
    assert (report.wins, report.losses, report.win_rate) == (1, 0, 100.0)


def test_shares_transferred_in_after_a_closed_round_trip_keep_it_closed(conn):
    """The same gap the other way round: 500 shares arrive from another broker,
    which no trade row records either."""
    _sive(conn)
    add_snapshot(conn, "S", position=700, symbol="SIVE", asset="STK",
                 date="20260929")
    report = build_history(conn, asset_category="STK")
    assert [(e.status, e.realized_pnl) for e in report.episodes] == [
        ("CLOSED", 600.0), ("OPEN", 0.0)]


def test_shares_transferred_out_after_a_closed_round_trip_keep_it_closed(conn):
    """And out, which seeded a negative quantity: a -500 short opened on the day
    the real round trip closed, with that round trip marked entry-missing."""
    _sive(conn)
    add_snapshot(conn, "S", position=-300, symbol="SIVE", asset="STK",
                 date="20260929")
    report = build_history(conn, asset_category="STK")
    assert [(e.status, e.net_qty, e.entry_outside_window)
            for e in report.episodes] == [
        ("CLOSED", 0, False), ("OPEN", 200, False)]
    assert report.total_realized_base == 600.0


def test_a_gap_present_on_every_snapshot_date_is_still_a_pre_archive_holding(conn):
    """The real TSLA 110 across two dates rather than one. A holding from before
    the archive shows the SAME gap on every date, because every later change to
    it is a trade -- including the sale of the pre-archive shares themselves.
    That is what separates it from a split or a transfer, which starts mid-way."""
    add_trade(conn, "1", conid="T", symbol="TSLA", asset="STK", open_close="O",
              qty=40, date="2026-08-28")
    add_trade(conn, "2", conid="T", symbol="TSLA", asset="STK", open_close="O",
              qty=56, date="2026-09-04")
    add_snapshot(conn, "T", position=150, symbol="TSLA", asset="STK",
                 date="20260901")
    add_snapshot(conn, "T", position=206, symbol="TSLA", asset="STK",
                 date="20260929")
    (ep,) = build_history(conn, asset_category="STK").episodes
    assert (ep.status, ep.net_qty, ep.pre_archive_qty) == ("OPEN", 206, 110)
    assert ep.entry_outside_window is True and ep.opened_at is None


def _pre_archive_lines(tmp_path, n: int) -> int:
    """Lines `_pre_archive` runs for `n` traded contracts in an account with `n`
    snapshot dates, every one of them decided on the first date."""
    import sys

    from optjournal import history

    conn = connect_migrated(tmp_path / f"walk{n}.db")
    add_statement(conn, from_date="2026-01-01")
    for day in range(1, n + 1):
        add_snapshot(conn, "HELD", position=1, date=f"202607{day:02d}")
    for c in range(n):
        add_trade(conn, str(c), conid=f"C{c}", date="2026-09-30")
    rows = conn.execute("SELECT * FROM trades").fetchall()
    code, count = history._pre_archive.__code__, 0

    def local(frame, event, arg):
        nonlocal count
        count += event == "line"
        return local

    sys.settrace(lambda frame, event, arg: local if frame.f_code is code else None)
    try:
        assert history._pre_archive(rows, conn) == {}
    finally:
        sys.settrace(None)
    return count


def test_the_pre_archive_walk_stops_once_its_answer_is_settled(tmp_path):
    """Every contract with fills walked every snapshot date of its account, though
    the first date already decides almost all of them (a flat gap seeds nothing),
    so the walk grew as contracts times dates, twice per `/api/state`: 199ms on
    two and a half years of synthetic daily statements, 23ms stopped where the
    answer is settled. Counted in lines run rather than timed, which a loaded
    machine cannot make flaky: tripling both contracts and dates must triple the
    work (measured 592 to 1752 lines), where the full walk grew it sevenfold
    (1712 to 12312)."""
    small, large = _pre_archive_lines(tmp_path, 20), _pre_archive_lines(tmp_path, 60)
    assert large < 4 * small, (small, large)


# --- a bare close past flat -----------------------------------------------------


def test_a_plain_close_past_flat_takes_the_position_flat(conn):
    """`qa/s_prearchive_add.py` with no snapshot at all: held 1 before the
    archive, buy 1, sell 2 marked `C`.

    Only IBKR's `C;O` opens anything. A bare `C` closes only, so a quantity past
    flat says the position was larger than the archive saw -- one more share held
    before it, or transferred in -- and the position is flat. Split at zero like a
    reversal, it left a phantom -1 short OPEN carrying 400 of premium nothing
    sold."""
    add_trade(conn, "1", open_close="O", qty=1, price=2.0, date="2026-09-02")
    add_trade(conn, "2", open_close="C", qty=-2, price=4.0, date="2026-09-20",
              realized=398.0)
    (ep,) = build_history(conn).episodes
    assert (ep.status, ep.net_qty, ep.realized_pnl) == ("CLOSED", 0, 398.0)
    assert (ep.entry_outside_window, ep.pre_archive_qty) == (True, 1)
    assert ep.contracts == 2, "it closed 2, so it held 2"
    assert ep.proceeds == pytest.approx(-200.0 + 800.0), (
        "the whole sale, not the half a split at zero would have left here")


def test_a_bare_close_past_flat_does_not_absorb_a_later_re_entry(conn):
    """The episode the overshoot flattened is finished, so a later opening fill
    on the same contract starts a new one."""
    add_trade(conn, "1", open_close="O", qty=1, date="2026-09-02")
    add_trade(conn, "2", open_close="C", qty=-2, date="2026-09-20",
              realized=398.0)
    add_trade(conn, "3", open_close="O", qty=4, date="2026-09-25")
    report = build_history(conn)
    assert [(e.status, e.net_qty) for e in report.episodes] == [
        ("CLOSED", 0), ("OPEN", 4)]


# ------------------------------------------------------------------------ 0DTE


def _ep(*, opened, closed=None, expiry) -> Episode:
    """A bare episode carrying only the dates `is_odte` reads."""
    return Episode(
        conid="1", symbol="X", asset_category="OPT", currency="USD",
        opened_at=opened, closed_at=closed, expiry=expiry,
    )


def test_odte_is_not_the_same_question_as_a_zero_day_holding_period():
    """The distinction the demo data cannot make, so it is pinned here.

    Every closed episode in the synthetic statement agrees under either
    definition -- its one same-day round trip is also its one same-day expiry --
    so a `holding_days == 0` implementation would pass every other test in the
    suite while being wrong about what 0DTE means.
    """
    # Opened and closed within one session, but the contract had 45 days left.
    day_trade = _ep(opened="2026-01-16 10:02:00", closed="2026-01-16 15:44:00",
                    expiry="2026-03-02")
    assert day_trade.holding_days == 0, "precondition: this is a same-day trade"
    assert day_trade.is_odte is False, "a 45-DTE day trade is not a 0DTE trade"

    # Opened on expiry day and held to the bell: 0DTE, same holding period.
    odte = _ep(opened="2026-01-16 10:02:00", closed="2026-01-16 15:44:00",
               expiry="2026-01-16")
    assert odte.holding_days == 0
    assert odte.is_odte is True

    # Opened on expiry day is enough on its own -- the close date is irrelevant,
    # since a contract cannot outlive its expiry.
    assert _ep(opened="2026-01-16 10:02:00", expiry="2026-01-16").is_odte is True


def test_odte_survives_the_forms_the_two_dates_actually_arrive_in():
    """A string comparison would be False on every genuine 0DTE trade.

    `opened_at` carries a time of day and `expiry` does not, and expiry reaches
    the database in IBKR's compact form -- verified against the stored column,
    which holds `20250221` while `date_time` holds `2025-01-14 14:30:05`. So
    `opened_at == expiry` is not merely fragile, it never matches.
    """
    compact = _ep(opened="2026-01-16 10:02:00", expiry="20260116")
    assert compact.opened_at != compact.expiry, "precondition: raw forms differ"
    assert compact.is_odte is True

    assert _ep(opened="20260116;100200", expiry="2026-01-16").is_odte is True
    assert _ep(opened="2026-01-16", expiry="2026-01-17").is_odte is False


def test_odte_is_unknown_rather_than_false_without_an_expiry():
    """A stock has no DTE, which is a different claim from "not 0DTE"."""
    assert _ep(opened="2026-01-16 10:02:00", expiry=None).is_odte is None
    assert _ep(opened=None, expiry="2026-01-16").is_odte is None
    assert _ep(opened="not a date", expiry="2026-01-16").is_odte is None


# --------------------------------------------- the computed-P&L oracle
#
# `Episode.realized_pnl` comes from IBKR's `fifoPnlRealized`, which this module's
# docstring documents as already net of both legs' commission. A broker that does
# not supply a per-fill figure would force this journal to compute one, and that
# is PLAN.md's task 9 -- not built, deliberately.
#
# What IS built is the measurement that task needs. The walk below reconstructs
# realised P&L from the fills alone and compares it against the broker's own
# number wherever both exist. It has two data points today and gains one per
# closed position, so it accumulates the evidence while nothing depends on it.
#
# Two things it would catch, neither visible any other way:
#
# * The first PARTIAL close. Both closed positions in the archive are FULL
#   liquidations -- SIVE sold 1,800 against lots of 400 + 1,400 -- and when every
#   lot is consumed, FIFO, LIFO and specific-lot all agree. So the arithmetic is
#   verified and the lot-matching POLICY is not. The SIVE sale even carries IBKR's
#   `SL` (specific-lot) note and FIFO still matched, because on a full close the
#   method is unobservable. A partial close is where they diverge.
# * A change in the commission convention. The walk charges BOTH legs, per the
#   docstring's verified arithmetic, so a disagreement would appear as a fixed
#   offset rather than noise.
#
# If this ever fails, the failure IS the design input task 9 is waiting for.


def _fifo_realized(rows: list[dict]) -> dict[str, float]:
    """Realised P&L per contract, from fills alone. FIFO lot matching.

    Deliberately a local test helper rather than a module in `src`: nothing in the
    domain reads it, and putting it in the package would imply a decision task 9
    has not made. When that decision comes it belongs in a leaf (`lots.py`,
    alongside `money.py`), not here.

    The model, verified against IBKR on SIVE to the cent:
    P&L = (exit - entry) x qty x multiplier, plus BOTH legs' commission share.
    IBKR states commission negative for a charge, so it ADDS.
    """
    from collections import defaultdict, deque

    books: dict[str, deque] = defaultdict(deque)
    pnl: dict[str, float] = defaultdict(float)

    for row in rows:
        key = str(row["conid"])
        qty = float(row["quantity"])
        price = float(row["trade_price"])
        multiplier = float(row["multiplier"] or 1.0)
        commission = float(row["ib_commission"] or 0.0)
        per_unit = commission / abs(qty) if qty else 0.0
        book = books[key]

        # Opposite sign to the open lots means this fill CLOSES against them.
        # Read from the position rather than from `open_close`, so a broker that
        # omits the indicator still works -- and so a mislabelled fill cannot
        # make a close look like a second opening.
        if book and (book[0][0] > 0) != (qty > 0):
            remaining = qty
            while book and remaining != 0:
                lot = book[0]
                take = min(abs(lot[0]), abs(remaining))
                signed = take if lot[0] > 0 else -take
                pnl[key] += (price - lot[1]) * signed * multiplier
                pnl[key] += lot[2] * take + per_unit * take
                lot[0] -= signed
                remaining += signed
                if lot[0] == 0:
                    book.popleft()
            if remaining != 0:      # closed more than was held: the rest opens
                book.append([remaining, price, per_unit])
        else:
            book.append([qty, price, per_unit])
    return dict(pnl)


@pytest.mark.skipif(not LIVE_STATEMENTS, reason="needs the optional live corpus")
def test_computed_fifo_pnl_agrees_with_the_brokers_own_figure(tmp_path):
    """The oracle for task 9: can this journal derive what IBKR reports?

    Asserted per contract AND in total, because a compensating pair of errors
    would pass a total-only check. Tolerance is a cent: both sides are floats over
    six-figure notionals, and IBKR itself rounds its published figure (68683.68
    against a computed 68683.68001).

    Only over contracts with a PARTIAL close. That restriction is the point, and
    it currently makes this test skip -- see the assertion at the bottom, which
    fails if there are no partial closes rather than passing quietly. A full
    liquidation consumes every lot, so FIFO, LIFO and specific-lot all produce the
    same basis and agreement proves nothing about the METHOD. Both closed
    positions in the archive are full liquidations, and one of them
    (`notes="SL"`) was closed by SPECIFIC-LOT selection while still matching a
    FIFO walk to the cent -- exactly the false reassurance this guards against.
    """
    conn = connect_migrated(tmp_path / "fifo.db")
    for path in LIVE_STATEMENTS:
        ingest_file(conn, path, assets=ASSET_FILTER_ALL)

    rows = [dict(r) for r in conn.execute(
        "SELECT conid, symbol, quantity, trade_price, multiplier, ib_commission,"
        " fifo_pnl_realized, notes FROM trades WHERE asset_category IN ('OPT','STK')"
        " ORDER BY COALESCE(date_time, trade_date), trade_id"
    )]
    assert rows, "the archive should hold option and stock fills"

    computed = _fifo_realized(rows)
    reported: dict[str, float] = {}
    for row in rows:
        key = str(row["conid"])
        reported[key] = reported.get(key, 0.0) + float(row["fifo_pnl_realized"] or 0.0)

    closed = {k: v for k, v in reported.items() if abs(v) > 1e-9}
    assert closed, (
        "no contract in the archive reports a realised P&L, so this proves "
        "nothing -- the oracle needs at least one closed position"
    )

    for key, broker_pnl in sorted(closed.items()):
        ours = computed.get(key, 0.0)
        symbol = next(r["symbol"] for r in rows if str(r["conid"]) == key)
        assert ours == pytest.approx(broker_pnl, abs=0.01), (
            f"{symbol}: computed {ours:.4f} against IBKR's {broker_pnl:.4f}. "
            f"Either the FIFO walk is wrong, or IBKR used a different "
            f"lot-matching method (a PARTIAL close would do it) or a different "
            f"commission convention. See PLAN.md task 9 -- this failure is the "
            f"design input it is waiting for."
        )

    assert sum(computed.values()) == pytest.approx(sum(reported.values()), abs=0.01)

    # And the part that stops a green run from reading as more than it is.
    partial = _partially_closed(rows)
    if not partial:
        pytest.skip(
            "every closed position in the archive is a FULL liquidation, so this "
            "agreement says nothing about lot-matching METHOD -- FIFO, LIFO and "
            "specific-lot all give the same basis when every lot is consumed. "
            "Skipped rather than passed on purpose: a green tick here would read "
            "as 'specific-lot handled'. The first partial close makes this test "
            "meaningful, and it is the case that decides PLAN.md task 9."
        )


def _partially_closed(rows: list[dict]) -> set[str]:
    """Contracts whose position was reduced without reaching flat.

    The only case where lot-matching method is OBSERVABLE. Computed from the
    running position rather than from `open_close`, so a mislabelled fill cannot
    hide one.
    """
    position: dict[str, float] = {}
    partial: set[str] = set()
    for row in rows:
        key = str(row["conid"])
        before = position.get(key, 0.0)
        after = before + float(row["quantity"])
        # Reduced toward zero but did not reach it, and was not a fresh open.
        if before != 0 and abs(after) < abs(before) and abs(after) > 1e-9:
            partial.add(key)
        position[key] = after
    return partial


@pytest.mark.skipif(not LIVE_STATEMENTS, reason="needs the optional live corpus")
def test_specific_lot_selection_leaves_no_trace_this_journal_can_follow():
    """The limitation, pinned so it is discovered here rather than in April.

    A sale closed by SPECIFIC-LOT selection reports which lots were sold in
    IBKR's lot-level detail -- `origTradeID`, `origTradePrice`, `origTradeDate`,
    `holdingPeriodDateTime`. py_ibkr models all four and `raw` preserves all four,
    so nothing is being dropped by this code. They are EMPTY because the Flex
    query asks for `levelOfDetail="EXECUTION"`, and lot detail is a different
    level the query template does not enable.

    That is the honest limit: no computed P&L can reproduce specific-lot selection
    from execution-level data, because the lot-to-close mapping is the input it
    lacks. It is not hypothetical -- this account already sells specific lots
    (`notes="SL"`), and got the right answer only because the sale was a full
    liquidation, where the method cannot matter.

    Asserted rather than commented so that the day a statement DOES carry lot
    detail, this test fails and says the assumption changed. That is the trigger
    to reconsider, and it costs no schema: `raw` already holds the fields.
    """
    from optjournal.flex import load
    from optjournal.sources import _notes

    lot_fields = ("origTradeID", "origTradePrice", "origTradeDate",
                  "holdingPeriodDateTime")
    seen_sl = False
    populated: list[str] = []

    for path in LIVE_STATEMENTS:
        for stmt in load(path).FlexStatements:
            for trade in stmt.Trades or ():
                # Through `sources._notes`, because py_ibkr hands back a LIST of
                # Code enum members -- `str()` on it gives
                # "[<Code.SPECIFICLOT: 'SL'>]", so a naive split finds no 'SL'
                # token and this test would silently stop describing the account.
                if "SL" in split_notes(_notes(trade.notes)):
                    seen_sl = True
                for field in lot_fields:
                    value = getattr(trade, field, None)
                    # origTradePrice arrives as "0" rather than empty when absent.
                    if value not in (None, "", 0, "0"):
                        populated.append(f"{trade.symbol}.{field}={value!r}")

    assert seen_sl, (
        "no fill in the archive carries the SL (specific-lot) note, so this test "
        "no longer describes the account -- re-check whether lot selection is "
        "still in use before trusting a computed P&L"
    )
    assert not populated, (
        f"a statement now carries lot-level detail: {populated[:5]}. The Flex "
        f"query must have moved off levelOfDetail=EXECUTION, which means specific-"
        f"lot selection is finally followable -- see PLAN.md task 9, and note that "
        f"`raw` has been preserving these fields all along, so no refetch is needed."
    )


def test_the_fifo_walk_charges_both_legs_commission():
    """The convention, stated on a hand-built round trip rather than inferred.

    `history.py`'s docstring proves IBKR's figure is net of BOTH opening and
    closing commission, which is why `Episode.commission` must never be subtracted
    again. The walk has to follow the same rule or the two are not comparable --
    and a second broker charging differently is precisely what task 9 has to
    record rather than assume.
    """
    rows = [
        {"conid": "C1", "quantity": -3, "trade_price": 2.10, "multiplier": 100,
         "ib_commission": -1.95, "fifo_pnl_realized": 0.0},
        {"conid": "C1", "quantity": 3, "trade_price": 0.0, "multiplier": 100,
         "ib_commission": -1.95, "fifo_pnl_realized": 0.0},
    ]
    # 630 premium received, 1.95 charged on each leg.
    assert _fifo_realized(rows)["C1"] == pytest.approx(630.0 - 1.95 - 1.95)
    # Not the opening-only reading, which is what the demo's hand-written
    # literals use (628.05) and what a careless implementation would produce.
    assert _fifo_realized(rows)["C1"] != pytest.approx(630.0 - 1.95)


def test_the_fifo_walk_matches_oldest_lots_first():
    """FIFO is the claim, so a partial close must consume the OLDEST lot.

    The archive cannot test this -- both its closed positions are full
    liquidations, where every method agrees. Hand-built, because the property is
    what makes the oracle above meaningful: without it, "FIFO agrees with IBKR"
    could hold for a walk that is not FIFO at all.
    """
    rows = [
        {"conid": "C1", "quantity": 10, "trade_price": 1.00, "multiplier": 1,
         "ib_commission": 0.0, "fifo_pnl_realized": 0.0},
        {"conid": "C1", "quantity": 10, "trade_price": 5.00, "multiplier": 1,
         "ib_commission": 0.0, "fifo_pnl_realized": 0.0},
        {"conid": "C1", "quantity": -10, "trade_price": 6.00, "multiplier": 1,
         "ib_commission": 0.0, "fifo_pnl_realized": 0.0},
    ]
    # FIFO sells the 1.00 lot: (6 - 1) x 10 = 50. LIFO would give (6 - 5) x 10 = 10.
    assert _fifo_realized(rows)["C1"] == pytest.approx(50.0)


def test_the_fifo_walk_handles_reentry_after_a_full_close():
    """A contract closed and reopened is two episodes, and the second is flat.

    The SIVE case, which `history.py`'s docstring cites as the reason episodes
    rather than contracts are the unit: bought twice, fully sold, bought again
    three days later. The re-entry must not inherit the closed lots.
    """
    rows = [
        {"conid": "C1", "quantity": 4, "trade_price": 10.0, "multiplier": 1,
         "ib_commission": 0.0, "fifo_pnl_realized": 0.0},
        {"conid": "C1", "quantity": -4, "trade_price": 15.0, "multiplier": 1,
         "ib_commission": 0.0, "fifo_pnl_realized": 0.0},
        {"conid": "C1", "quantity": 4, "trade_price": 99.0, "multiplier": 1,
         "ib_commission": 0.0, "fifo_pnl_realized": 0.0},
    ]
    # Only the first round trip realises: (15 - 10) x 4 = 20. The re-entry is open.
    assert _fifo_realized(rows)["C1"] == pytest.approx(20.0)


def test_a_partial_close_is_distinguished_from_a_full_one():
    """The predicate the oracle's skip depends on, so it needs its own test.

    If `_partially_closed` under-reports, the oracle skips forever and the
    limitation is never surfaced. If it over-reports, the oracle starts asserting
    a method-sensitive agreement it has no evidence for. Both failures are silent,
    which is why this checks all three shapes rather than the interesting one.

    Re-entry is the case worth naming: closed to flat then reopened is TWO
    episodes, not a partial close, and the lot-matching method is unobservable in
    both -- the same distinction `history.py` makes by keying on episodes rather
    than contracts.
    """
    def fills(*quantities):
        return [{"conid": "C1", "quantity": q} for q in quantities]

    # 400 + 1,400 in, 1,800 out: the real SIVE shape. Every lot consumed.
    assert _partially_closed(fills(400, 1400, -1800)) == set()
    # Same lots, only 1,000 sold: 800 survive, so which 800 depends on the method.
    assert _partially_closed(fills(400, 1400, -1000)) == {"C1"}
    # Flat then reopened: two episodes, neither partial.
    assert _partially_closed(fills(4, -4, 9)) == set()
    # A short, since the sign of "reduced toward zero" flips.
    assert _partially_closed(fills(-10, 4)) == {"C1"}
    assert _partially_closed(fills(-10, 10)) == set()
