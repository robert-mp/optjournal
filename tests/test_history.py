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
from conftest import STATEMENTS, add_statement, connect_migrated

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


@pytest.mark.skipif(not STATEMENTS, reason="no archived statements")
def test_real_sive_round_trip(tmp_path):
    """SIVE: bought twice, fully sold, re-entered. IBKR states the P&L itself.

    Basis 62,592.00 + 50.46 opening commission = 62,642.46; proceeds 131,400
    less 73.86 closing commission = 131,326.14; difference 68,683.68, which is
    exactly IBKR's fifoPnlRealized. That makes this an independent oracle.
    """
    conn = connect_migrated(tmp_path / "all.db")
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


@pytest.mark.skipif(not STATEMENTS, reason="needs an archived statement")
def test_computed_fifo_pnl_agrees_with_the_brokers_own_figure(tmp_path):
    """The oracle for task 9: can this journal derive what IBKR reports?

    Asserted per contract AND in total, because a compensating pair of errors
    would pass a total-only check. Tolerance is a cent: both sides are floats over
    six-figure notionals, and IBKR itself rounds its published figure (68683.68
    against a computed 68683.68001).
    """
    conn = connect_migrated(tmp_path / "fifo.db")
    for path in STATEMENTS:
        ingest_file(conn, path, assets=ASSET_FILTER_ALL)

    rows = [dict(r) for r in conn.execute(
        "SELECT conid, symbol, quantity, trade_price, multiplier, ib_commission,"
        " fifo_pnl_realized FROM trades WHERE asset_category IN ('OPT','STK')"
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
