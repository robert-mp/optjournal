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
from pathlib import Path

import pytest

from optjournal.db import connect, migrate
from optjournal.history import (
    NON_POSITION_CATEGORIES,
    Episode,
    build_history,
    disposition_of,
    split_notes,
)
from optjournal.ingest import ASSET_FILTER_ALL, ingest_file

RAW_DIR = Path(__file__).resolve().parent.parent / "raw"
STATEMENTS = sorted(RAW_DIR.glob("activity-*.xml"))


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    c = connect(tmp_path / "history.db")
    migrate(c)
    c.execute(
        "INSERT INTO statements (source_file, sha256, account_id, from_date,"
        " to_date, base_currency, asset_filter, ingested_at)"
        " VALUES ('t.xml','x','U1','2026-01-01','2026-12-31','EUR','OPT','now')"
    )
    return c


_TRADE_SQL = (
    "INSERT INTO trades (trade_id, ib_exec_id, transaction_id, ib_order_id,"
    " account_id, trade_date, date_time, asset_category, symbol, conid,"
    " open_close, notes, quantity, trade_price, currency, fx_rate_to_base,"
    " proceeds, proceeds_base, ib_commission, ib_commission_base,"
    " fifo_pnl_realized, fifo_pnl_realized_base, raw, source_file, first_seen_at)"
    " VALUES (?,?,?,?, 'U1', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'USD', 1.0,"
    " ?, ?, ?, ?, ?, ?, '{}', 't.xml', 'now')"
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
) -> None:
    proceeds = -qty * price * 100
    conn.execute(
        _TRADE_SQL,
        (tid, f"e{tid}", f"x{tid}", f"o{tid}", date, f"{date} 10:00:00", asset,
         symbol, conid, open_close, notes, qty, price, proceeds, proceeds,
         commission, commission, realized, realized),
    )


def add_snapshot(conn, conid: str, *, position: int, symbol: str = "OPT1",
                 date: str = "2026-12-31", asset: str = "OPT",
                 cost_basis: float | None = None) -> None:
    conn.execute(
        "INSERT INTO position_snapshots (report_date, conid, account_id, symbol,"
        " asset_category, position, cost_basis_money, currency, fx_rate_to_base,"
        " raw, source_file, ingested_at)"
        " VALUES (?,?, 'U1', ?, ?, ?, ?, 'USD', 1.0, '{}', 't.xml', 'now')",
        (date, conid, symbol, asset, position, cost_basis),
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


# ----------------------------------------------------------------- exclusions


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


@pytest.mark.skipif(not STATEMENTS, reason="no archived statements")
def test_real_sive_round_trip(tmp_path):
    """SIVE: bought twice, fully sold, re-entered. IBKR states the P&L itself.

    Basis 62,592.00 + 50.46 opening commission = 62,642.46; proceeds 131,400
    less 73.86 closing commission = 131,326.14; difference 68,683.68, which is
    exactly IBKR's fifoPnlRealized. That makes this an independent oracle.
    """
    conn = connect(tmp_path / "all.db")
    migrate(conn)
    for path in STATEMENTS:
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
    conn = connect(tmp_path / "opt.db")
    migrate(conn)
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
    assert snapshot_only[0].cost_basis == pytest.approx(3000.807, abs=0.01)


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
