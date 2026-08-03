"""Tests for persistence, ingest idempotency and the grouping views.

Ingest runs against the real archived statements, because the behaviour that
matters -- overlapping statements re-presenting the same fills -- only exists
in genuine data. View maths is tested with hand-built rows so expectations
are computable by hand.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from optjournal.db import SCHEMA_VERSION, connect, migrate
from optjournal.ingest import ASSET_FILTER_ALL, ingest_file

RAW_DIR = Path(__file__).resolve().parent.parent / "raw"
STATEMENTS = sorted(RAW_DIR.glob("activity-*.xml"))


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    c = connect(tmp_path / "test.db")
    migrate(c)
    return c


def test_migrate_sets_version(conn):
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    assert row["v"] == SCHEMA_VERSION


def test_migrate_is_idempotent(conn):
    migrate(conn)
    migrate(conn)
    n = conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"]
    assert n == 1, "repeated migrate must not append version rows"


def test_pragmas_applied(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


@pytest.mark.skipif(not STATEMENTS, reason="no archived statements")
def test_ingest_filters_to_options_by_default(conn):
    r = ingest_file(conn, STATEMENTS[-1])
    assert r.trades_filtered_out > 0, "expected stock and FX trades to be filtered"
    cats = {
        row["asset_category"]
        for row in conn.execute("SELECT DISTINCT asset_category FROM trades")
    }
    assert cats == {"OPT"}


@pytest.mark.skipif(not STATEMENTS, reason="no archived statements")
def test_ingest_all_assets_keeps_everything(conn):
    r = ingest_file(conn, STATEMENTS[-1], assets=ASSET_FILTER_ALL)
    assert r.trades_filtered_out == 0
    cats = {
        row["asset_category"]
        for row in conn.execute("SELECT DISTINCT asset_category FROM trades")
    }
    assert len(cats) > 1


@pytest.mark.skipif(not STATEMENTS, reason="no archived statements")
def test_reingesting_same_file_is_a_noop(conn):
    first = ingest_file(conn, STATEMENTS[-1])
    again = ingest_file(conn, STATEMENTS[-1])
    assert again.already_ingested
    assert again.trades_inserted == 0
    total = conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"]
    assert total == first.trades_inserted


@pytest.mark.skipif(len(STATEMENTS) < 2, reason="need two overlapping statements")
def test_overlapping_statements_do_not_duplicate(conn):
    ingest_file(conn, STATEMENTS[0])
    before = conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"]
    second = ingest_file(conn, STATEMENTS[1])
    after = conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"]
    assert second.trades_skipped_existing > 0, "overlap should be detected"
    assert after >= before
    ids = [r["trade_id"] for r in conn.execute("SELECT trade_id FROM trades")]
    assert len(ids) == len(set(ids))


@pytest.mark.skipif(not STATEMENTS, reason="no archived statements")
def test_first_seen_at_is_preserved_across_reingest(conn):
    ingest_file(conn, STATEMENTS[0])
    original = {
        r["trade_id"]: r["first_seen_at"]
        for r in conn.execute("SELECT trade_id, first_seen_at FROM trades")
    }
    ingest_file(conn, STATEMENTS[1])
    for tid, seen in conn.execute("SELECT trade_id, first_seen_at FROM trades"):
        if tid in original:
            assert seen == original[tid], "first_seen_at must not be overwritten"


@pytest.mark.skipif(not STATEMENTS, reason="no archived statements")
def test_base_currency_conversion_is_applied(conn):
    ingest_file(conn, STATEMENTS[-1])
    for r in conn.execute(
        "SELECT proceeds, proceeds_base, fx_rate_to_base FROM trades"
        " WHERE proceeds IS NOT NULL"
    ):
        assert r["proceeds_base"] == pytest.approx(
            r["proceeds"] * r["fx_rate_to_base"]
        )


@pytest.mark.skipif(not STATEMENTS, reason="no archived statements")
def test_raw_column_is_populated(conn):
    ingest_file(conn, STATEMENTS[-1])
    for r in conn.execute("SELECT raw FROM trades"):
        assert r["raw"] and r["raw"] != "{}"


# --- view maths, on synthetic rows ------------------------------------------

def _insert_trade(conn, **kw):
    defaults = dict(
        trade_id="T1", ib_exec_id="E1", transaction_id="X1", ib_order_id="O1",
        account_id="U1", trade_date="2026-07-24", date_time="2026-07-24 10:00:00",
        asset_category="OPT", symbol="TSLA 260904P00270000", conid="C1",
        underlying_symbol="TSLA", underlying_conid="U", put_call="P", strike=270.0,
        expiry="20260904", multiplier=100.0, buy_sell="SELL", open_close="O",
        notes="P", level_of_detail="EXECUTION", quantity=-1, trade_price=5.0,
        currency="USD", fx_rate_to_base=0.9, proceeds=500.0, proceeds_base=450.0,
        ib_commission=-0.5, ib_commission_base=-0.45, taxes=0.0,
        fifo_pnl_realized=0.0, fifo_pnl_realized_base=0.0, mtm_pnl=0.0,
        raw="{}", source_file="s.xml", first_seen_at="2026-08-03T00:00:00+00:00",
    )
    defaults.update(kw)
    cols = ",".join(defaults)
    marks = ",".join("?" * len(defaults))
    conn.execute(f"INSERT INTO trades ({cols}) VALUES ({marks})", tuple(defaults.values()))


def _statement_row(conn):
    conn.execute(
        "INSERT INTO statements (source_file, sha256, account_id, from_date,"
        " to_date, base_currency, asset_filter, ingested_at)"
        " VALUES ('s.xml','x','U1','2026-07-01','2026-07-31','EUR','OPT','now')"
    )


def test_legs_collapse_partial_fills_with_weighted_price(conn):
    _statement_row(conn)
    _insert_trade(conn, trade_id="T1", ib_exec_id="E1", quantity=-2, trade_price=6.0)
    _insert_trade(conn, trade_id="T2", ib_exec_id="E2", quantity=-1, trade_price=3.0)
    leg = conn.execute("SELECT * FROM option_legs").fetchone()
    assert leg["fills"] == 2
    assert leg["quantity"] == -3
    # weighted by absolute quantity: (2*6 + 1*3) / 3 == 5.0
    assert leg["avg_price"] == pytest.approx(5.0)


def test_multi_leg_order_is_one_order_two_legs(conn):
    _statement_row(conn)
    _insert_trade(conn, trade_id="T1", ib_exec_id="E1", conid="C1", strike=270.0)
    _insert_trade(conn, trade_id="T2", ib_exec_id="E2", conid="C2", strike=280.0)
    assert conn.execute("SELECT COUNT(*) AS n FROM option_legs").fetchone()["n"] == 2
    order = conn.execute("SELECT * FROM option_orders").fetchone()
    assert order["leg_count"] == 2
    assert order["fills"] == 2


def test_current_positions_uses_latest_report_date(conn):
    _statement_row(conn)
    for report_date, mark in (("20260630", 1.0), ("20260731", 2.0)):
        conn.execute(
            "INSERT INTO position_snapshots (report_date, conid, account_id, symbol,"
            " asset_category, position, mark_price, currency, fx_rate_to_base, raw,"
            " source_file, ingested_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (report_date, "C1", "U1", "TSLA P", "OPT", -3, mark, "USD", 0.9,
             "{}", "s.xml", "now"),
        )
    rows = conn.execute("SELECT * FROM current_option_positions").fetchall()
    assert len(rows) == 1
    assert rows[0]["report_date"] == "20260731"
    assert rows[0]["mark_price"] == pytest.approx(2.0)


def test_snapshot_reingest_updates_rather_than_duplicates(conn):
    _statement_row(conn)
    for mark in (1.0, 5.0):
        conn.execute(
            "INSERT INTO position_snapshots (report_date, conid, account_id, symbol,"
            " asset_category, position, mark_price, currency, fx_rate_to_base, raw,"
            " source_file, ingested_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(report_date, conid) DO UPDATE SET mark_price=excluded.mark_price",
            ("20260731", "C1", "U1", "TSLA P", "OPT", -3, mark, "USD", 0.9,
             "{}", "s.xml", "now"),
        )
    rows = conn.execute("SELECT * FROM position_snapshots").fetchall()
    assert len(rows) == 1
    assert rows[0]["mark_price"] == pytest.approx(5.0)


# --- digest dedupe across filenames -----------------------------------------
#
# `flex._archive` dedupes at write time, so `fetch` cannot produce two names
# for identical bytes. A direct `ingest`, a copied file or a restored backup
# can, and each would otherwise add a provenance row claiming to be a distinct
# statement. Row data is protected by the primary keys; `statements` is not,
# and it is the audit trail.


@pytest.mark.skipif(not STATEMENTS, reason="needs an archived statement")
def test_identical_bytes_under_a_new_name_are_skipped(conn, tmp_path):
    original = STATEMENTS[0]
    first = ingest_file(conn, original, assets=ASSET_FILTER_ALL)
    assert not first.already_ingested

    copy = tmp_path / "activity-COPY.xml"
    copy.write_bytes(original.read_bytes())
    second = ingest_file(conn, copy, assets=ASSET_FILTER_ALL)

    assert second.already_ingested
    assert second.duplicate_of == original.name
    rows = conn.execute("SELECT COUNT(*) AS n FROM statements").fetchone()["n"]
    assert rows == 1, "a byte-identical copy must not add a provenance row"


@pytest.mark.skipif(not STATEMENTS, reason="needs an archived statement")
def test_duplicate_skip_warns_actionably(conn, tmp_path):
    original = STATEMENTS[0]
    ingest_file(conn, original, assets=ASSET_FILTER_ALL)
    copy = tmp_path / "activity-COPY.xml"
    copy.write_bytes(original.read_bytes())
    result = ingest_file(conn, copy, assets=ASSET_FILTER_ALL)

    assert len(result.warnings) == 1
    text = result.warnings[0]
    assert original.name in text and "prune" in text


@pytest.mark.skipif(not STATEMENTS, reason="needs an archived statement")
def test_duplicate_skip_inserts_no_rows(conn, tmp_path):
    """The skip must be total: no trades, cash, positions or securities."""
    original = STATEMENTS[0]
    ingest_file(conn, original, assets=ASSET_FILTER_ALL)
    before = {
        t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        for t in ("trades", "cash_transactions", "position_snapshots", "securities")
    }

    copy = tmp_path / "activity-COPY.xml"
    copy.write_bytes(original.read_bytes())
    ingest_file(conn, copy, assets=ASSET_FILTER_ALL)

    after = {
        t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        for t in ("trades", "cash_transactions", "position_snapshots", "securities")
    }
    assert before == after


@pytest.mark.skipif(not STATEMENTS, reason="needs an archived statement")
def test_reingest_overrides_digest_dedupe(conn, tmp_path):
    """`--reingest` is an explicit override and must bypass the digest check."""
    original = STATEMENTS[0]
    ingest_file(conn, original, assets=ASSET_FILTER_ALL)
    copy = tmp_path / "activity-COPY.xml"
    copy.write_bytes(original.read_bytes())

    result = ingest_file(conn, copy, assets=ASSET_FILTER_ALL, reingest=True)
    assert not result.already_ingested
    assert result.duplicate_of is None


@pytest.mark.skipif(len(STATEMENTS) < 2, reason="needs two distinct statements")
def test_distinct_statements_are_not_treated_as_duplicates(conn):
    """Guard against the dedupe being too eager and swallowing real data."""
    for path in STATEMENTS[:2]:
        result = ingest_file(conn, path, assets=ASSET_FILTER_ALL)
        assert result.duplicate_of is None
    rows = conn.execute("SELECT COUNT(*) AS n FROM statements").fetchone()["n"]
    assert rows == 2
