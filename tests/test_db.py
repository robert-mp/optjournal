"""Tests for persistence, ingest idempotency and the grouping views.

Ingest runs against a tracked, redacted Flex statement. Tests that need a
second statement create a content-distinct copy with the same fills, reproducing
the overlap that rolling Flex windows produce without depending on private data.
View maths is tested with hand-built rows so expectations are computable by hand.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from conftest import STATEMENTS, add_statement

from optjournal import db as db_module
from optjournal.db import CONFIRM_SOURCE, SCHEMA_VERSION, connect, migrate
from optjournal.ingest import ASSET_FILTER_ALL, ingest_file

# `conn` comes from conftest. The explicit connect()/migrate() pairs further
# down are NOT replaced with the shared helper on purpose: this module is what
# tests migration, so a test asserting that migrate() is idempotent, or that a
# schema bump heals an existing journal, has to call it itself. Hiding those
# calls behind a fixture would leave the subject under test invisible.


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


@pytest.mark.skipif(not STATEMENTS, reason="no tracked statement fixtures")
def test_ingest_keeps_everything_by_default(conn):
    """Storage is unfiltered; category scoping happens at query time.

    The old OPT-only default made the database disagree with its own archive:
    the Equities view read empty because ingest had dropped the rows, and any
    later sync silently re-narrowed a database that had been widened by hand.
    """
    r = ingest_file(conn, STATEMENTS[-1])
    assert r.trades_filtered_out == 0
    cats = {
        row["asset_category"]
        for row in conn.execute("SELECT DISTINCT asset_category FROM trades")
    }
    assert len(cats) > 1, "the fixture holds stock and FX besides options"


@pytest.mark.skipif(not STATEMENTS, reason="no tracked statement fixtures")
def test_ingest_can_still_narrow_to_options(conn):
    r = ingest_file(conn, STATEMENTS[-1], assets=("OPT",))
    assert r.trades_filtered_out > 0, "expected stock and FX trades to be filtered"
    cats = {
        row["asset_category"]
        for row in conn.execute("SELECT DISTINCT asset_category FROM trades")
    }
    assert cats == {"OPT"}


@pytest.mark.skipif(not STATEMENTS, reason="no tracked statement fixtures")
def test_ingest_all_assets_keeps_everything(conn):
    r = ingest_file(conn, STATEMENTS[-1], assets=ASSET_FILTER_ALL)
    assert r.trades_filtered_out == 0
    cats = {
        row["asset_category"]
        for row in conn.execute("SELECT DISTINCT asset_category FROM trades")
    }
    assert len(cats) > 1


@pytest.mark.skipif(not STATEMENTS, reason="no tracked statement fixtures")
def test_reingesting_same_file_is_a_noop(conn):
    first = ingest_file(conn, STATEMENTS[-1])
    again = ingest_file(conn, STATEMENTS[-1])
    assert again.already_ingested
    assert again.trades_inserted == 0
    total = conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"]
    assert total == first.trades_inserted


def _overlapping_statement(tmp_path: Path) -> Path:
    """A second statement file carrying the same fills under a new digest."""
    source = STATEMENTS[0]
    text = source.read_text(encoding="utf-8")
    old = 'whenGenerated="20260227;060000"'
    assert old in text, "fixture generation stamp changed; update this seam"
    path = tmp_path / "activity-overlap.xml"
    path.write_text(text.replace(old, 'whenGenerated="20260228;060000"', 1))
    return path


@pytest.mark.skipif(not STATEMENTS, reason="no tracked statement fixtures")
def test_overlapping_statements_do_not_duplicate(conn, tmp_path):
    ingest_file(conn, STATEMENTS[0])
    before = conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"]
    second = ingest_file(conn, _overlapping_statement(tmp_path))
    after = conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"]
    assert second.trades_skipped_existing > 0, "overlap should be detected"
    assert after >= before
    ids = [r["trade_id"] for r in conn.execute("SELECT trade_id FROM trades")]
    assert len(ids) == len(set(ids))


@pytest.mark.skipif(not STATEMENTS, reason="no tracked statement fixtures")
def test_first_seen_at_is_preserved_across_reingest(conn, tmp_path):
    ingest_file(conn, STATEMENTS[0])
    original = {
        r["trade_id"]: r["first_seen_at"]
        for r in conn.execute("SELECT trade_id, first_seen_at FROM trades")
    }
    ingest_file(conn, _overlapping_statement(tmp_path))
    for tid, seen in conn.execute("SELECT trade_id, first_seen_at FROM trades"):
        if tid in original:
            assert seen == original[tid], "first_seen_at must not be overwritten"


@pytest.mark.skipif(not STATEMENTS, reason="no tracked statement fixtures")
def test_base_currency_conversion_is_applied(conn):
    ingest_file(conn, STATEMENTS[-1])
    for r in conn.execute(
        "SELECT proceeds, proceeds_base, fx_rate_to_base FROM trades"
        " WHERE proceeds IS NOT NULL"
    ):
        assert r["proceeds_base"] == pytest.approx(
            r["proceeds"] * r["fx_rate_to_base"]
        )


@pytest.mark.skipif(not STATEMENTS, reason="no tracked statement fixtures")
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
    add_statement(
        conn, source_file="s.xml", from_date="2026-07-01", to_date="2026-07-31"
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
    """Re-ingesting the same date's snapshot updates the row, never adds one.

    The conflict target names `broker` because the key does. It was
    `(report_date, conid)` and moved when snapshot identity became per-broker --
    a conid is IBKR's numbering, so two brokers can each hold "contract 12345".
    """
    _statement_row(conn)
    for mark in (1.0, 5.0):
        conn.execute(
            "INSERT INTO position_snapshots (report_date, conid, account_id, symbol,"
            " asset_category, position, mark_price, currency, fx_rate_to_base, raw,"
            " source_file, ingested_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(broker, report_date, conid)"
            " DO UPDATE SET mark_price=excluded.mark_price",
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


def test_migrate_refreshes_stale_view_definitions(tmp_path):
    """Views are code: a definition change must reach existing databases.

    CREATE VIEW IF NOT EXISTS never updates, so a database created before a
    view changed would keep the old SQL forever -- exactly how the OPT-only
    order views would have survived into a journal that stores every
    category. Simulated here by planting a garbage definition and asserting
    migrate replaces it.
    """
    conn = connect(tmp_path / "v.db")
    migrate(conn)
    conn.execute("DROP VIEW trade_legs")
    conn.execute("CREATE VIEW trade_legs AS SELECT 1 AS stale")
    migrate(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_legs)")}
    assert "stale" not in cols and "asset_category" in cols
    conn.close()


def test_fractional_stock_quantities_survive_ingest(tmp_path):
    """413.22 shares must not become 413.

    The int coercion predates storing stocks: option quantities are always
    integral, stock lots are not -- a dividend reinvestment buys 1.79 shares
    and the real SIVE sale was 413.22. SQLite's INTEGER affinity keeps the
    fraction losslessly; truncating it changed the position size.
    """
    import json

    # From `sources`, not `ingest`: quantity coercion is the reading half of the
    # broker seam, so it moved there with the rest of the vocabulary.
    from optjournal.sources import _qty

    assert _qty("3") == 3 and isinstance(_qty("3"), int)
    assert _qty("-2.0") == -2 and isinstance(_qty("-2.0"), int)
    assert _qty("413.22") == 413.22
    assert _qty("1.79") == 1.79
    assert _qty("") is None

    conn = connect(tmp_path / "frac.db")
    migrate(conn)
    add_statement(
        conn, account_id="U0", from_date="2025", to_date="2025", asset_filter="ALL"
    )
    for tid, qty, oc in (("t1", 413.22, "O"), ("t2", -413.22, "C")):
        conn.execute(
            "INSERT INTO trades (trade_id, ib_exec_id, transaction_id,"
            " account_id, trade_date, asset_category, symbol, conid, quantity,"
            " currency, fx_rate_to_base, open_close, raw, source_file,"
            " first_seen_at) VALUES (?,?,?, 'U0', '2025-05-05', 'STK', 'SIVE',"
            " '1', ?, 'SEK', 0.09, ?, ?, 't.xml', 'now')",
            (tid, tid, tid, qty, oc, json.dumps({})),
        )
    row = conn.execute("SELECT quantity FROM trades WHERE trade_id='t1'").fetchone()
    assert row["quantity"] == 413.22, "INTEGER affinity must keep the fraction"

    # And the episode over the pair is flat -- closed, not stuck open on dust.
    from optjournal.history import build_history

    report = build_history(conn, asset_category="STK")
    assert len(report.episodes) == 1
    assert report.episodes[0].is_closed
    conn.close()


def test_a_column_added_after_ship_reaches_an_existing_database(tmp_path):
    """`executescript(_SCHEMA)` uses CREATE TABLE IF NOT EXISTS, which is a
    no-op on a table that already exists -- so a new column in the schema text
    reaches new databases only, and every journal on disk is an old one. The
    explicit ALTER is what makes the column real for them, and it must be
    idempotent because migrate() runs on every single connection.
    """
    from optjournal.db import _ADDED_COLUMNS

    path = tmp_path / "j.db"
    conn = connect(path)
    migrate(conn)
    for table, column, _decl in _ADDED_COLUMNS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert column in cols, f"{table}.{column} missing after migrate"
    # Simulate the pre-column database: drop it and migrate again.
    conn.execute("ALTER TABLE trades DROP COLUMN ib_commission_currency")
    assert "ib_commission_currency" not in {
        r["name"] for r in conn.execute("PRAGMA table_info(trades)")
    }
    migrate(conn)
    assert "ib_commission_currency" in {
        r["name"] for r in conn.execute("PRAGMA table_info(trades)")
    }, "the ALTER did not run on a database missing the column"
    migrate(conn)  # idempotent -- a second run must not raise
    conn.close()


def test_commission_currency_is_stored_and_read_back(tmp_path):
    """Stored rather than assumed. ib_commission_base is commission x
    fx_rate_to_base, and that rate belongs to the INSTRUMENT's currency -- so
    the base figure is only right while the commission is billed in that same
    currency. It is on every row observed, but an assumption that is never
    checked fails silently, so the field is persisted and compared.
    """
    from optjournal.demo import write_demo_statement

    statement = write_demo_statement(tmp_path / "demo", tmp_path / "demo.db")
    conn = connect(tmp_path / "demo.db")
    migrate(conn)
    result = ingest_file(conn, statement)

    rows = conn.execute(
        "SELECT currency, ib_commission, ib_commission_currency FROM trades"
        " WHERE ib_commission IS NOT NULL AND ib_commission <> 0"
    ).fetchall()
    assert rows, "no commissioned trades to check"
    stored = [r["ib_commission_currency"] for r in rows]
    assert all(stored), "the commission currency was not persisted"
    # The invariant the base conversion depends on.
    for r in rows:
        assert r["ib_commission_currency"] == r["currency"], (
            "commission currency differs from the instrument currency, which"
            " means ib_commission_base used the wrong rate"
        )
    # Agreement means no warning; the warning exists for the day it disagrees.
    assert not [w for w in result.warnings if "commission billed in" in w]
    conn.close()


def test_a_commission_billed_in_another_currency_warns_without_aborting(tmp_path):
    """A warning, not a raise: a genuine broker quirk should surface, not abort
    an ingest. The native amount is still stored correctly either way -- only
    the base conversion would be suspect, so the run continues and says so.
    """
    from optjournal.demo import write_demo_statement

    src = write_demo_statement(tmp_path / "demo", tmp_path / "demo.db")
    doctored = tmp_path / "doctored.xml"
    raw = src.read_text(encoding="utf-8")
    # Bill one USD option's commission in GBP, leaving everything else intact.
    # The demo emits a Flex XML statement, so this is an attribute, not JSON.
    assert 'ibCommissionCurrency="USD"' in raw
    doctored.write_text(raw.replace('ibCommissionCurrency="USD"',
                                    'ibCommissionCurrency="GBP"', 1))

    conn = connect(tmp_path / "d.db")
    migrate(conn)
    result = ingest_file(conn, doctored)
    assert [w for w in result.warnings if "commission billed in GBP" in w], \
        f"no warning raised; got {result.warnings}"
    assert result.trades_inserted, "the ingest aborted instead of warning"
    conn.close()


def test_the_commission_warning_does_not_repeat_on_every_re_ingest(tmp_path):
    """A warning reports a decision TAKEN, so a duplicate row must not warn.

    The real failure, seen in a nightly cron notification: the message read
    "0 new trade(s)" and carried a per-trade commission warning in the same
    breath. The warning was appended before the INSERT, so it fired whether or
    not `ON CONFLICT DO NOTHING` did anything -- describing a conversion choice
    made once, on 2026-08-03, as though it had just been made again.

    That matters beyond tidiness. IBKR's Flex window rolls about 365 days, so
    the row stays in every statement for a year: the warning would have fired
    daily until August 2027. A warning that cries every day over
    correctly-handled data is one nobody reads on the day it means something.

    `test_ingests_cleanly` asserts exactly this property already
    (`again.warnings == []`) and passes regardless, because every demo trade is
    a USD instrument billed in USD -- there is no divergent row for it to
    notice. Hence a statement doctored to carry one.
    """
    from optjournal.demo import write_demo_statement

    src = write_demo_statement(tmp_path / "demo", tmp_path / "demo.db")
    doctored = tmp_path / "doctored.xml"
    raw = src.read_text(encoding="utf-8")
    assert 'ibCommissionCurrency="USD"' in raw
    doctored.write_text(raw.replace('ibCommissionCurrency="USD"',
                                    'ibCommissionCurrency="GBP"', 1))

    conn = connect(tmp_path / "d.db")
    migrate(conn)

    first = ingest_file(conn, doctored)
    warned = [w for w in first.warnings if "commission billed in GBP" in w]
    assert len(warned) == 1, f"expected exactly one warning, got {first.warnings}"
    assert first.trades_inserted, "nothing was inserted, so nothing was decided"

    # Same statement again. Every row is a no-op, so the run has decided
    # nothing and has nothing to report.
    again = ingest_file(conn, doctored, reingest=True)
    assert again.trades_inserted == 0, "re-ingest stopped being idempotent"
    assert again.trades_skipped_existing > 0
    assert not [w for w in again.warnings if "commission billed in" in w], (
        "the commission warning repeated on a row that was skipped as existing"
        f"; got {again.warnings}"
    )

    # The stored figure is untouched by the second pass -- the point is that the
    # warning was noise, not that the conversion was wrong.
    row = conn.execute(
        "SELECT ib_commission_currency, ib_commission_base FROM trades"
        " WHERE ib_commission_currency = 'GBP'"
    ).fetchone()
    assert row["ib_commission_currency"] == "GBP"
    conn.close()


def test_commission_currency_backfills_from_the_stored_raw(tmp_path):
    """A journal that ingested before the column existed holds the value in
    `raw` and NULL in the column, and no broker request is needed to recover it.

    Deliberately not gated on schema_version: that stamp is written the first
    time migrate() runs after the bump, so for any journal merely OPENED since
    then a one-shot hook would silently never fire -- which is exactly the state
    the real journal was in. The work guards itself instead.
    """
    from optjournal.db import _backfill_commission_currency
    from optjournal.demo import write_demo_statement

    statement = write_demo_statement(tmp_path / "demo", tmp_path / "demo.db")
    conn = connect(tmp_path / "demo.db")
    migrate(conn)
    ingest_file(conn, statement)

    # Simulate the pre-column journal: the value survives only in `raw`.
    conn.execute("UPDATE trades SET ib_commission_currency = NULL")
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM trades WHERE ib_commission_currency IS NOT NULL"
    ).fetchone()[0] == 0

    filled = _backfill_commission_currency(conn)
    assert filled > 0, "nothing was recovered from raw"
    rows = conn.execute(
        "SELECT currency, ib_commission_currency FROM trades"
        " WHERE ib_commission IS NOT NULL AND ib_commission <> 0"
    ).fetchall()
    assert all(r["ib_commission_currency"] for r in rows), "rows still NULL"
    for r in rows:
        assert r["ib_commission_currency"] == r["currency"]

    # Self-terminating: the set it operates on is empty once it has run, so a
    # second pass does no work and cannot loop on rows raw cannot supply.
    assert _backfill_commission_currency(conn) == 0
    conn.close()


def test_the_backfill_runs_on_open_without_a_version_bump(tmp_path):
    """migrate() performs it, so an existing journal heals by being opened --
    no command to remember, and no dependence on a version transition that has
    already happened.
    """
    from optjournal.db import open_journal
    from optjournal.demo import write_demo_statement

    statement = write_demo_statement(tmp_path / "demo", tmp_path / "demo.db")
    conn = connect(tmp_path / "demo.db")
    migrate(conn)
    ingest_file(conn, statement)
    conn.execute("UPDATE trades SET ib_commission_currency = NULL")
    conn.commit()
    version_before = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    conn.close()

    with open_journal(tmp_path / "demo.db") as c:
        assert c.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] \
            == version_before, "the test relies on the version already being current"
        assert c.execute(
            "SELECT COUNT(*) FROM trades WHERE ib_commission_currency IS NOT NULL"
        ).fetchone()[0] > 0, "opening the journal did not heal it"


def test_commission_base_uses_a_rate_that_applies_to_the_commission():
    """`fxRateToBase` is the INSTRUMENT's rate, so applying it to the commission
    is right only while the two currencies agree.

    On an EUR.SEK conversion IBKR bills the commission in EUR while the row's
    currency is SEK -- multiplying a EUR amount by the SEK->EUR rate stored a
    figure 11x too small. A commission in a currency the statement carries no
    rate for yields None rather than a guess.
    """
    from optjournal.ingest import _commission_base

    # Agreeing currencies: unchanged behaviour, the instrument's rate applies.
    assert _commission_base(-2.0, "USD", "USD", 0.868920, "EUR") == -2.0 * 0.868920
    # Absent commission currency: same, because that is all the old data says.
    assert _commission_base(-2.0, None, "USD", 0.868920, "EUR") == -2.0 * 0.868920
    # Already base: no conversion applies at all. The real defect.
    assert _commission_base(-1.73464, "EUR", "SEK", 0.090897, "EUR") == -1.73464
    # A third currency: no rate exists anywhere in the statement.
    assert _commission_base(-1.5, "GBP", "SEK", 0.090897, "EUR") is None
    # Nothing to convert.
    assert _commission_base(None, "USD", "USD", 1.0, "EUR") is None


def test_a_wrongly_converted_commission_is_repaired_on_open(tmp_path):
    """Derived data, provably wrong and recomputable, so it heals by opening --
    no command to remember. Only rows the data can DEFINITIVELY correct are
    touched: a commission billed in base needs no conversion, so its base value
    is its native value. A third currency has no rate and is left alone.
    """
    from optjournal.db import _repair_base_commission, open_journal
    from optjournal.demo import write_demo_statement

    statement = write_demo_statement(tmp_path / "demo", tmp_path / "demo.db")
    conn = connect(tmp_path / "demo.db")
    migrate(conn)
    ingest_file(conn, statement)
    base = conn.execute("SELECT base_currency FROM statements").fetchone()[0]

    # Recreate the defect: a row billed in base, but converted as if it were in
    # the instrument's currency.
    victim = conn.execute(
        "SELECT trade_id, ib_commission FROM trades"
        " WHERE ib_commission <> 0 AND currency <> ?", (base,)
    ).fetchone()
    if victim is None:  # demo holds no non-base commissioned row
        return
    conn.execute(
        "UPDATE trades SET ib_commission_currency = ?,"
        " ib_commission_base = ib_commission * 0.09 WHERE trade_id = ?",
        (base, victim["trade_id"]),
    )
    conn.commit()
    conn.close()

    with open_journal(tmp_path / "demo.db") as c:
        row = c.execute(
            "SELECT ib_commission, ib_commission_base FROM trades WHERE trade_id = ?",
            (victim["trade_id"],),
        ).fetchone()
        assert row["ib_commission_base"] == row["ib_commission"], \
            "opening the journal did not repair the mis-converted row"
        # Idempotent: nothing left to repair.
        assert _repair_base_commission(c) == 0

    # A third currency is NOT touched -- there is no rate to correct it with.
    conn = connect(tmp_path / "demo.db")
    conn.execute(
        "UPDATE trades SET ib_commission_currency = 'GBP',"
        " ib_commission_base = -99.0 WHERE trade_id = ?", (victim["trade_id"],))
    conn.commit()
    assert _repair_base_commission(conn) == 0, "guessed at a currency with no rate"
    assert conn.execute(
        "SELECT ib_commission_base FROM trades WHERE trade_id = ?",
        (victim["trade_id"],)).fetchone()[0] == -99.0
    conn.close()


# --- broker identity ---------------------------------------------------------
#
# `trade_id` was the PRIMARY KEY and `ib_exec_id` carried a UNIQUE index: IBKR's
# own identifiers, treated as globally unique. A second broker numbering a fill
# `1` would either raise or, worse, be silently swallowed by the ingest's
# ON CONFLICT ... DO NOTHING and reported as a duplicate.


def test_trade_identity_is_per_broker(conn):
    """Two brokers may both issue trade id '1', and both rows must survive."""
    _statement_row(conn)
    _insert_trade(conn, trade_id="1", ib_exec_id="E1", broker="ibkr")
    _insert_trade(conn, trade_id="1", ib_exec_id="E1", broker="schwab")

    rows = conn.execute(
        "SELECT broker, trade_id FROM trades ORDER BY broker"
    ).fetchall()
    assert [(r["broker"], r["trade_id"]) for r in rows] == [
        ("ibkr", "1"), ("schwab", "1"),
    ], "the second broker's fill was dropped as a duplicate"


def test_the_same_broker_still_cannot_insert_a_trade_twice(conn):
    """The dedupe that makes ingest idempotent must keep working WITHIN a broker.

    This is the half that would break silently if the conflict target were simply
    widened without thought: overlapping statements re-present the same fills, and
    first-write-wins is what keeps `first_seen_at` truthful.
    """
    _statement_row(conn)
    _insert_trade(conn, trade_id="1", ib_exec_id="E1", broker="ibkr")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_trade(conn, trade_id="1", ib_exec_id="E9", broker="ibkr")


def test_execution_ids_are_also_scoped_to_their_broker(conn):
    """`trades_exec` was UNIQUE on `ib_exec_id` alone -- the second global-identity
    assumption, and the one easy to miss because it is an index rather than a key.
    """
    _statement_row(conn)
    _insert_trade(conn, trade_id="A", ib_exec_id="E1", broker="ibkr")
    _insert_trade(conn, trade_id="B", ib_exec_id="E1", broker="schwab")
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 2
    # Still unique within one broker.
    with pytest.raises(sqlite3.IntegrityError):
        _insert_trade(conn, trade_id="C", ib_exec_id="E1", broker="ibkr")


def test_missing_execution_ids_are_not_one_shared_identifier(conn):
    """A broker may omit the secondary id on more than one real execution."""
    _statement_row(conn)
    _insert_trade(conn, trade_id="A", ib_exec_id=None, broker="ibkr")
    _insert_trade(conn, trade_id="B", ib_exec_id=None, broker="ibkr")
    assert conn.execute(
        "SELECT COUNT(*) FROM trades WHERE ib_exec_id IS NULL"
    ).fetchone()[0] == 2


def test_migration_makes_execution_id_optional_and_normalises_empty(tmp_path):
    """Existing journals upgrade without preserving an empty fake identifier."""
    db = tmp_path / "old-exec.db"
    conn = connect(db)
    migrate(conn)
    conn.execute("DROP INDEX trades_exec")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("ALTER TABLE trades RENAME TO trades_current")
    old_ddl = db_module._TRADES_DDL.replace(
        "ib_exec_id              TEXT,",
        "ib_exec_id              TEXT    NOT NULL,",
    ).replace(
        "CREATE TABLE IF NOT EXISTS trades",
        "CREATE TABLE trades",
    )
    conn.executescript(old_ddl)
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(trades)")]
    names = ", ".join(columns)
    conn.execute(
        f"INSERT INTO trades ({names}) SELECT {names} FROM trades_current"
    )
    conn.execute("DROP TABLE trades_current")
    conn.execute(
        "CREATE UNIQUE INDEX trades_exec ON trades(broker, ib_exec_id)"
    )
    _statement_row(conn)
    _insert_trade(conn, trade_id="A", ib_exec_id="", broker="ibkr")
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")

    migrate(conn)

    info = {r["name"]: r for r in conn.execute("PRAGMA table_info(trades)")}
    assert info["ib_exec_id"]["notnull"] == 0
    assert conn.execute(
        "SELECT ib_exec_id FROM trades WHERE trade_id = 'A'"
    ).fetchone()[0] is None
    index_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'trades_exec'"
    ).fetchone()[0]
    assert "WHERE ib_exec_id IS NOT NULL" in index_sql


def test_an_existing_journal_is_rekeyed_losslessly(tmp_path):
    """The migration rebuilds `trades`, which is the risky kind of change.

    Built as a pre-migration journal on purpose: the old table is created with
    `trade_id` as the sole PRIMARY KEY and no broker column, then `migrate` is
    asked to rekey it. Asserting on row COUNT alone would pass a rebuild that
    scrambled columns, so every value is compared.
    """
    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.executescript(
        "CREATE TABLE statements (source_file TEXT PRIMARY KEY, sha256 TEXT NOT NULL,"
        " account_id TEXT NOT NULL, from_date TEXT NOT NULL, to_date TEXT NOT NULL,"
        " when_generated TEXT, base_currency TEXT NOT NULL, asset_filter TEXT NOT NULL,"
        " ingested_at TEXT NOT NULL);"
        # Faithful to the shipped pre-migration table in the columns that matter
        # here: sole `trade_id` key, no `broker`, and the columns _SCHEMA indexes
        # (ib_order_id, underlying_symbol) present -- a fixture without those fails
        # on CREATE INDEX for a reason that has nothing to do with rekeying.
        "CREATE TABLE trades (trade_id TEXT PRIMARY KEY, ib_exec_id TEXT NOT NULL,"
        " transaction_id TEXT NOT NULL, ib_order_id TEXT, account_id TEXT NOT NULL,"
        " trade_date TEXT NOT NULL, asset_category TEXT NOT NULL, symbol TEXT NOT NULL,"
        " underlying_symbol TEXT,"
        " quantity INTEGER NOT NULL, currency TEXT NOT NULL, fx_rate_to_base REAL NOT NULL,"
        " raw TEXT NOT NULL, source_file TEXT NOT NULL, first_seen_at TEXT NOT NULL);"
    )
    old.execute(
        "INSERT INTO statements VALUES ('s.xml','x','U1','2026-07-01','2026-07-31',"
        " NULL,'EUR','ALL','now')"
    )
    old.execute(
        "INSERT INTO trades VALUES ('T1','E1','X1','O1','U1','2026-07-24','OPT',"
        " 'TSLA  260904P00270000','TSLA',-3,'USD',0.9,'{}','s.xml','then')"
    )
    old.commit()
    old.close()

    conn = connect(db)
    assert [r["name"] for r in conn.execute("PRAGMA table_info(trades)") if r["pk"]] == [
        "trade_id"
    ], "fixture is not a pre-migration journal"
    migrate(conn)

    assert [r["name"] for r in conn.execute("PRAGMA table_info(trades)") if r["pk"]] == [
        "broker", "trade_id",
    ]
    row = dict(conn.execute("SELECT * FROM trades").fetchone())
    assert row["broker"] == "ibkr", "existing rows are IBKR's; that is a fact, not a guess"
    # Every original value intact, in the right column.
    assert (row["trade_id"], row["ib_exec_id"]) == ("T1", "E1")
    assert row["symbol"] == "TSLA  260904P00270000"
    assert (row["ib_order_id"], row["underlying_symbol"]) == ("O1", "TSLA")
    assert (row["quantity"], row["currency"], row["fx_rate_to_base"]) == (-3, "USD", 0.9)
    assert row["first_seen_at"] == "then", "first_seen_at must survive a rebuild"
    # The views the rebuild had to drop are back.
    views = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view'")}
    assert "trade_legs" in views and "current_option_positions" in views


def test_rekeying_is_idempotent(tmp_path):
    """migrate() runs on every connection, so the rebuild must not repeat."""
    db = tmp_path / "j.db"
    conn = connect(db)
    migrate(conn)
    _statement_row(conn)
    _insert_trade(conn, trade_id="T1", ib_exec_id="E1")
    conn.commit()
    for _ in range(3):
        migrate(conn)
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    assert not conn.execute(
        "SELECT name FROM sqlite_master WHERE name='trades_rekeyed'"
    ).fetchall(), "the scratch table was left behind"


# ---------------------------------------------------------------------------
# The job ledger (v8). SCHEDULER_PLAN.md step 4.
# ---------------------------------------------------------------------------


def test_a_job_cannot_fire_twice_for_the_same_instant(tmp_path):
    """Idempotency is a CONSTRAINT here, not a convention in the reconciler.

    The reconciler decides due-ness by asking whether a row exists for a
    scheduled instant. If two ticks could both claim the same slot, `sync` would
    spend two IBKR requests against a lockout budget -- so the database refuses
    rather than the code remembering to check.

    This is also the DST fall-back guard, for free: the repeated 01:30 is the same
    instant, so the second one cannot be claimed.
    """
    conn = connect(tmp_path / "j.db")
    migrate(conn)
    row = ("sync", 1786310000, "2026-08-09T12:00:00Z", "running")
    conn.execute(
        "INSERT INTO job_runs (job, fired_for, started_at, status) VALUES (?,?,?,?)",
        row)
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="job_runs.job"):
        conn.execute(
            "INSERT INTO job_runs (job, fired_for, started_at, status)"
            " VALUES (?,?,?,?)", row)
    conn.rollback()

    # Per job, not global: two jobs share a scheduled minute all the time.
    conn.execute(
        "INSERT INTO job_runs (job, fired_for, started_at, status)"
        " VALUES ('bars_daily', 1786310000, '2026-08-09T12:00:00Z', 'running')")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0] == 2


def test_a_manual_run_is_never_blocked_by_the_schedule(tmp_path):
    """A Run-now press claims no scheduled slot, so it repeats freely.

    HONEST ABOUT WHAT THIS PROVES. The first version of this test claimed it was
    demonstrating why the unique index is PARTIAL, and SCHEDULER_PLAN.md gives the
    same reason -- "the partial index is what keeps manual run-now presses
    unconstrained". Both are wrong, and ablation showed it: removing
    `WHERE fired_for IS NOT NULL` left this test green, because SQLite treats NULLs
    as DISTINCT in a unique index. Three NULL rows are accepted either way.

    So this is a behaviour test for the button, not an argument for the index.
    What the WHERE clause actually buys is a smaller index that holds only
    scheduled rows -- real, minor, and not a correctness property. Recorded rather
    than quietly deleted, because a test whose docstring justifies a design
    decision it cannot detect is worse than no test.
    """
    conn = connect(tmp_path / "j.db")
    migrate(conn)
    for _ in range(3):
        conn.execute(
            "INSERT INTO job_runs (job, fired_for, started_at, status)"
            " VALUES ('sync', NULL, '2026-08-09T12:00:00Z', 'ok')")
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM job_runs WHERE fired_for IS NULL").fetchone()[0] == 3


def test_a_refused_claim_must_be_rolled_back(tmp_path):
    """The wedge, measured before it was designed around.

    `sqlite3` does NOT roll back on IntegrityError: the connection stays in a
    transaction holding the write lock. Measured on this schema -- the next writer
    then waits the full BUSY_TIMEOUT_MS (15.5s) and fails with "database is
    locked". So the runner must rollback in every IntegrityError branch, and the
    failure mode without it is "the scheduler wedges its own database by losing a
    race it was designed to lose".

    Asserted here rather than left to `jobs.py`, because it is a property of the
    constraint, and this is where the constraint lives.
    """
    conn = connect(tmp_path / "j.db")
    migrate(conn)
    row = ("sync", 1786310000, "2026-08-09T12:00:00Z", "running")
    sql = "INSERT INTO job_runs (job, fired_for, started_at, status) VALUES (?,?,?,?)"
    conn.execute(sql, row)
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, row)
    assert conn.in_transaction, (
        "premise of this test: a refused INSERT leaves the write lock held. If "
        "sqlite3 ever changes this, the rollback in jobs.py is still correct but "
        "this test is no longer measuring anything"
    )
    conn.rollback()
    assert not conn.in_transaction


def test_the_ledger_survives_v7_and_changes_nothing_else(tmp_path):
    """v7 -> v8 is purely additive: two tables, no existing row touched.

    Verified against a copy of the real journal before shipping -- 168 trades,
    1,816 bars, 99 events and 261 NAV rows all hashed identically before and
    after. This is the same shape at test scale, plus the idempotency `migrate`
    needs because it runs on every connection.
    """
    conn = connect(tmp_path / "j.db")
    migrate(conn)
    _statement_row(conn)
    _insert_trade(conn, trade_id="T1", ib_exec_id="E1")
    conn.execute("INSERT INTO watchlist (symbol, added_at) VALUES ('SPY','x')")
    conn.commit()

    # Simulate a v7 journal: drop the new tables and stamp the old version.
    conn.execute("DROP TABLE job_runs")
    conn.execute("DROP TABLE job_state")
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version, applied_at)"
                 " VALUES (7, datetime('now'))")
    conn.commit()

    migrate(conn)
    # SCHEMA_VERSION rather than the literal 8 this shipped with: what the test is
    # about is that a journal stamped at an OLD version arrives at the current one
    # with every row intact, and a literal makes that assertion need an edit on
    # every bump -- which is an invitation to edit the number and stop reading the
    # rest.
    assert conn.execute(
        "SELECT MAX(version) FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    for table in ("job_state", "job_runs"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 1

    for _ in range(3):
        migrate(conn)
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1


def test_a_pre_migration_journal_gains_the_earnings_column(tmp_path):
    """`earnings_on` reaches a journal that already has a `watchlist` table.

    The trap this exists for is structural rather than hypothetical: `_SCHEMA` uses
    CREATE TABLE IF NOT EXISTS, so adding a column there reaches NEW databases only
    -- and every journal on disk is an old one. The `_ADDED_COLUMNS` entry is the
    only thing that makes the column real for them, and without it the tab renders,
    the endpoint accepts a date and the write fails with "no such column" on the one
    machine that has been keeping a journal.

    So the table is created here at the OLD shape rather than by dropping the column
    from a current one: that is the state on disk, three columns and a stamp from
    before this change, and it exercises the ALTER against exactly it.

    The row planted first is what proves the migration is additive. A rebuild that
    lost a typed note would be worse than a missing column, because `watchlist` is
    the user-input table -- nothing can re-derive what was in it.
    """
    from optjournal.db import schema_is_current

    path = tmp_path / "j.db"
    conn = connect(path)
    conn.executescript(
        "CREATE TABLE watchlist ("
        "  symbol   TEXT PRIMARY KEY,"
        "  note     TEXT,"
        "  added_at TEXT NOT NULL"
        ");"
        "CREATE TABLE schema_version (version INTEGER NOT NULL,"
        "                             applied_at TEXT NOT NULL);"
        "INSERT INTO schema_version (version, applied_at) VALUES (8, 'then');"
    )
    conn.execute("INSERT INTO watchlist (symbol, note, added_at)"
                 " VALUES ('DELL', 'watching the print', '2026-08-01')")
    conn.commit()
    assert "earnings_on" not in {
        r["name"] for r in conn.execute("PRAGMA table_info(watchlist)")
    }, "the fixture is meant to start at the pre-migration shape"
    assert not schema_is_current(conn)

    migrate(conn)

    assert "earnings_on" in {
        r["name"] for r in conn.execute("PRAGMA table_info(watchlist)")
    }, (
        "the ALTER did not run: CREATE TABLE IF NOT EXISTS is a no-op on an "
        "existing table, so _ADDED_COLUMNS is what reaches a real journal"
    )
    # Additive, and the typed row is untouched -- with the new column NULL rather
    # than a date the migration invented.
    row = conn.execute(
        "SELECT note, earnings_on FROM watchlist WHERE symbol = 'DELL'").fetchone()
    assert (row["note"], row["earnings_on"]) == ("watching the print", None)

    # And the column is writable, which is the whole point of it existing. A date
    # written here is what `optjournal watch --earnings` and the endpoint both do.
    conn.execute("UPDATE watchlist SET earnings_on = '2026-08-27'"
                 " WHERE symbol = 'DELL'")
    conn.commit()
    assert conn.execute(
        "SELECT earnings_on FROM watchlist WHERE symbol = 'DELL'"
    ).fetchone()["earnings_on"] == "2026-08-27"
    migrate(conn)  # idempotent: migrate runs on every single connection
    assert schema_is_current(conn)
    conn.close()


def test_the_heartbeat_and_the_anchor_are_separable(tmp_path):
    """Two tables, because pruning history must not delete the schedule.

    `job_runs` is trimmed; `job_state` is one row per job forever. Folded into
    one table, a retention pass could remove the row recording when `sync` last
    fired, and the reconciler would then either replay a year of instants or lose
    the schedule silently.

    Asserted by doing what the retention pass does -- emptying `job_runs` -- and
    checking the anchor is still there.
    """
    conn = connect(tmp_path / "j.db")
    migrate(conn)
    conn.execute(
        "INSERT INTO job_state (job, last_fired_for, last_status, heartbeat_at)"
        " VALUES ('sync', 1786310000, 'ok', 1786310100)")
    conn.execute(
        "INSERT INTO job_runs (job, fired_for, started_at, status)"
        " VALUES ('sync', 1786310000, '2026-08-09T12:00:00Z', 'ok')")
    conn.commit()

    conn.execute("DELETE FROM job_runs")          # the retention pass
    conn.commit()

    anchor = conn.execute(
        "SELECT last_fired_for, heartbeat_at FROM job_state WHERE job='sync'"
    ).fetchone()
    assert anchor["last_fired_for"] == 1786310000, (
        "pruning history deleted the catch-up anchor"
    )
    assert anchor["heartbeat_at"] == 1786310100


def _confirm_shaped(statement, target):
    """The demo statement with its settled figures removed, as a same-session
    Trade Confirmation reports the same fills.

    An execution has a price and a quantity the instant it happens, but no FIFO
    match and no final commission until the day is closed out, so those are the
    two fields dropped.

    Doctored from an Activity Statement rather than built from a real
    TradeConfirms payload because the rank guard under test never reads the XML:
    it compares `source_kind` on rows a source has already normalised. The
    parser for IBKR's own confirm shape is a separate concern, and cannot be
    written honestly without a sample of it to read.
    """
    import re

    raw = statement.read_text(encoding="utf-8")
    assert 'fifoPnlRealized="907.4"' in raw, "the demo statement stopped settling"
    # `ibCommission="` and not `ibCommission`, so ibCommissionCurrency survives.
    raw = re.sub(r'fifoPnlRealized="[^"]*"', 'fifoPnlRealized=""', raw)
    raw = re.sub(r'ibCommission="[^"]*"', 'ibCommission="0"', raw)
    target.write_text(raw)
    return target


def _settlement(conn):
    return conn.execute(
        "SELECT COUNT(*) AS n,"
        " SUM(fifo_pnl_realized IS NULL) AS unsettled,"
        " SUM(ib_commission = 0 AND asset_category != 'CASH') AS free,"
        " COUNT(DISTINCT source_kind) AS kinds,"
        " MIN(source_kind) AS kind"
        " FROM trades"
    ).fetchone()


def test_the_activity_statement_supersedes_a_same_day_confirmation(tmp_path):
    """The settled record replaces the same-session one, in place.

    Both queries describe one execution under one `tradeID`, so first-write-wins
    meant a confirm arriving first BLOCKED the row that carries the realised
    P&L, the FIFO match and the final commission -- silently and permanently,
    because a confirm looks like a complete fill. Ranked instead: a confirm may
    be superseded, and superseding is not the same event as a new fill, so it is
    counted apart from one.
    """
    from optjournal.db import ACTIVITY_SOURCE
    from optjournal.demo import write_demo_statement

    activity = write_demo_statement(tmp_path / "demo", tmp_path / "demo.db")
    confirm = _confirm_shaped(activity, tmp_path / "confirm.xml")

    conn = connect(tmp_path / "j.db")
    migrate(conn)

    first = ingest_file(conn, confirm, source_kind=CONFIRM_SOURCE)
    assert first.trades_inserted, "the confirm stored no fills"
    before = _settlement(conn)
    assert before["unsettled"] == before["n"], "the confirm arrived pre-settled"
    assert before["kind"] == CONFIRM_SOURCE

    second = ingest_file(conn, activity)

    assert second.trades_inserted == 0, (
        "the confirm's fills were counted as new fills again, so a morning sync "
        "would announce yesterday's trades as today's"
    )
    assert second.trades_superseded == before["n"]
    after = _settlement(conn)
    assert after["n"] == before["n"], "the same execution was stored twice"
    assert after["unsettled"] == 0, "the settled P&L never landed"
    assert after["free"] == 0, "the provisional zero commission survived"
    assert (after["kinds"], after["kind"]) == (1, ACTIVITY_SOURCE)
    conn.close()


def test_a_confirmation_arriving_late_cannot_walk_a_settled_row_back(tmp_path):
    """Rank runs one way. Re-running the confirm query after the Activity
    Statement has landed must not restore the mid-session view of the fill.

    This is the failure the guard exists for and the one nothing would report:
    the row would still be there, still under the right id, with its realised
    P&L quietly back to nothing.
    """
    from optjournal.db import ACTIVITY_SOURCE
    from optjournal.demo import write_demo_statement

    activity = write_demo_statement(tmp_path / "demo", tmp_path / "demo.db")
    confirm = _confirm_shaped(activity, tmp_path / "confirm.xml")

    conn = connect(tmp_path / "j.db")
    migrate(conn)
    ingest_file(conn, activity)
    settled = _settlement(conn)
    assert settled["unsettled"] == 0

    late = ingest_file(conn, confirm, source_kind=CONFIRM_SOURCE)

    assert late.trades_inserted == 0 and late.trades_superseded == 0
    assert late.trades_skipped_existing == settled["n"]
    assert dict(_settlement(conn)) == dict(settled)
    assert _settlement(conn)["kind"] == ACTIVITY_SOURCE
    conn.close()


def test_a_source_of_unknown_rank_cannot_supersede_anything(tmp_path):
    """`SOURCE_RANK` fails closed: a kind nobody ranked ranks below every kind
    somebody did.

    So adding a third Flex query is a decision made in `SOURCE_RANK`, not one
    made accidentally by whoever first passes its name -- which would otherwise
    let an untested reader overwrite settled figures on its first run.
    """
    from optjournal.demo import write_demo_statement

    activity = write_demo_statement(tmp_path / "demo", tmp_path / "demo.db")
    stranger = _confirm_shaped(activity, tmp_path / "stranger.xml")

    conn = connect(tmp_path / "j.db")
    migrate(conn)
    ingest_file(conn, activity)
    settled = _settlement(conn)

    result = ingest_file(conn, stranger, source_kind="some-later-query")

    assert result.trades_superseded == 0
    assert result.trades_skipped_existing == settled["n"]
    assert dict(_settlement(conn)) == dict(settled)
    conn.close()


def test_a_supersede_does_not_re_date_when_the_journal_first_saw_a_fill(tmp_path):
    """`first_seen_at` answers "what is new since yesterday", so it records a
    SIGHTING, not the last write.

    Refreshed on supersede, every fill confirmed yesterday would look new again
    the morning its Activity Statement lands -- which is precisely the day the
    reader stops needing to be told about it. Backdated by hand rather than
    trusting two ingests a second apart to differ, since the stamp has
    second resolution and both would tie.
    """
    from optjournal.demo import write_demo_statement

    activity = write_demo_statement(tmp_path / "demo", tmp_path / "demo.db")
    confirm = _confirm_shaped(activity, tmp_path / "confirm.xml")

    conn = connect(tmp_path / "j.db")
    migrate(conn)
    ingest_file(conn, confirm, source_kind=CONFIRM_SOURCE)
    conn.execute("UPDATE trades SET first_seen_at = '2026-08-30T21:15:00+00:00'")
    conn.commit()

    ingest_file(conn, activity)

    stamps = {
        r["first_seen_at"]
        for r in conn.execute("SELECT DISTINCT first_seen_at FROM trades")
    }
    assert stamps == {"2026-08-30T21:15:00+00:00"}, (
        "superseding a confirm re-dated the fill, so every settled trade would "
        f"read as newly seen; got {stamps}"
    )
    conn.close()


def test_a_superseded_row_equals_one_the_statement_wrote_from_scratch(tmp_path):
    """A supersede must leave the winning source's row ENTIRE, not a blend.

    The upsert used to name its updated columns by hand and missed some, so a
    superseded row took the statement's `proceeds_base` while keeping the
    confirm's `fx_rate_to_base` -- the rate that figure was derived from. Nothing
    would have reported that: both values are plausible, and only their ratio is
    wrong.

    Every column the confirm wrote is SCRAMBLED first, rather than trusting the
    doctored statement to differ. That is the difference between a test with teeth
    and one that looks like it has them: the first version compared a confirm that
    diverged in two fields, so dropping `fx_rate_to_base` from the update left
    both rows agreeing and the test passed on a defect it was written to catch.
    With every value wrong to begin with, a column the supersede forgets is a
    column that stays wrong.

    The key is left alone because it is what the two rows are matched on, and
    `source_kind` because a scrambled rank would make the supersede itself
    unreachable. `first_seen_at` is excluded from the comparison, being the one
    field a supersede deliberately keeps -- see
    `test_a_supersede_does_not_re_date_when_the_journal_first_saw_a_fill`.
    """
    from optjournal.demo import write_demo_statement

    activity = write_demo_statement(tmp_path / "demo", tmp_path / "demo.db")
    confirm = _confirm_shaped(activity, tmp_path / "confirm.xml")

    def rows(conn):
        return {
            str(r["trade_id"]): {
                k: v for k, v in dict(r).items() if k != "first_seen_at"
            }
            for r in conn.execute("SELECT * FROM trades")
        }

    superseded = connect(tmp_path / "superseded.db")
    migrate(superseded)
    ingest_file(superseded, confirm, source_kind=CONFIRM_SOURCE)
    # `source_file` is scrambled to the confirm's own name, so it stays a valid
    # foreign key while still being the wrong answer.
    keep = {"broker", "trade_id", "source_kind"}
    columns = [
        r["name"] for r in superseded.execute("PRAGMA table_info(trades)")
        if r["name"] not in keep
    ]
    # Per-row values, not one constant: `ib_exec_id` and `transaction_id` are
    # unique per broker, so a single sentinel across 28 rows collides.
    superseded.execute(
        "UPDATE trades SET " + ", ".join(
            f"{c} = " + ("'confirm.xml'" if c == "source_file"
                         else "'SCRAMBLED-' || trade_id")
            for c in columns
        )
    )
    superseded.commit()

    ingest_file(superseded, activity)

    direct = connect(tmp_path / "direct.db")
    migrate(direct)
    ingest_file(direct, activity)

    got, want = rows(superseded), rows(direct)
    assert got.keys() == want.keys() and got
    differing = {
        trade_id: {
            column: (value, want[trade_id][column])
            for column, value in fields.items()
            if value != want[trade_id][column]
        }
        for trade_id, fields in got.items()
    }
    left_behind = {k: v for k, v in differing.items() if v}
    assert not left_behind, (
        "superseding did not rewrite every column, so the row keeps values the "
        f"settled statement never wrote: {left_behind}"
    )
    superseded.close()
    direct.close()


# --- a statement that fails part-way leaves nothing behind (H1) ---------------


def _positions_break_ingest(tmp_path: Path, name: str = "activity-broken.xml") -> Path:
    """The fixture with one open position missing its symbol.

    The trades and cash sections are written before the positions, so the NOT
    NULL on `position_snapshots.symbol` raises part-way through the file, after
    the provenance row and every fill have been written.
    """
    text = STATEMENTS[0].read_text(encoding="utf-8")
    old = 'symbol="NVDA  260320P00140000"'
    assert old in text, "fixture position changed; update this seam"
    path = tmp_path / name
    path.write_text(text.replace(old, 'symbol=""', 1), encoding="utf-8")
    return path


@pytest.mark.skipif(not STATEMENTS, reason="needs an archived statement")
def test_a_statement_that_fails_part_way_leaves_nothing_for_the_caller_to_commit(
    conn, tmp_path,
):
    """The job runner commits on the same connection after a failure.

    Before, the half-written statement survived that commit, and every later
    run skipped the file as "byte-identical, nothing to do": the live journal
    kept three statements with zero position snapshots that way.
    """
    broken = _positions_break_ingest(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        ingest_file(conn, broken)
    conn.commit()  # what `jobs._finish` does next, on this connection

    for table in ("statements", "trades", "cash_transactions",
                  "position_snapshots", "equity_summaries"):
        n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        assert n == 0, f"{table} kept {n} row(s) from the failed statement"

    # The same bytes are not "already ingested": the retry tries again.
    with pytest.raises(sqlite3.IntegrityError):
        ingest_file(conn, broken)
    conn.commit()

    # And once the file reads cleanly, it is ingested in full.
    broken.write_bytes(STATEMENTS[0].read_bytes())
    result = ingest_file(conn, broken)
    assert not result.already_ingested
    assert result.trades_inserted > 0
    assert result.positions_written > 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM position_snapshots").fetchone()["n"] == \
        result.positions_written


@pytest.mark.skipif(not STATEMENTS, reason="needs an archived statement")
def test_a_failed_ingest_keeps_the_callers_own_pending_writes(conn, tmp_path):
    """Undoing the statement must not undo what the caller wrote before it."""
    add_statement(conn, source_file="caller.xml")  # uncommitted, the caller's
    with pytest.raises(sqlite3.IntegrityError):
        ingest_file(conn, _positions_break_ingest(tmp_path))
    conn.commit()
    names = [r["source_file"] for r in conn.execute("SELECT source_file FROM statements")]
    assert names == ["caller.xml"]


# --- confirm rows stored before M3 are rewritten on open -----------------------


def test_compact_confirm_dates_already_stored_are_rewritten_on_open(tmp_path):
    """M3: the live journal holds confirm rows written as IBKR's compact text.

    Opening the journal rewrites them into the forms an Activity Statement row
    has, and leaves everything else alone: an activity row, and a confirm row
    whose date is already ISO. Run twice to show it settles.
    """
    conn = connect(tmp_path / "j.db")
    migrate(conn)
    add_statement(conn, source_file="confirm-20260924.xml",
                  from_date="20260924", to_date="20260924")
    conn.execute("UPDATE statements SET when_generated = '20260924;114524'")
    _statement_row(conn)
    _insert_trade(conn, trade_id="C1", ib_exec_id="EC1", source_kind=CONFIRM_SOURCE,
                  source_file="confirm-20260924.xml", trade_date="20260924",
                  date_time="20260924;101659", expiry="20261016")
    _insert_trade(conn, trade_id="A1", ib_exec_id="EA1", expiry="2026-09-04")
    conn.execute(
        "INSERT INTO journal_entries (account_id, anchor_order_id, opened_on,"
        " created_at, updated_at) VALUES ('U1', 'O1', '20260924', 'now', 'now')")
    conn.commit()

    migrate(conn)
    migrate(conn)

    confirm = conn.execute(
        "SELECT trade_date, date_time, expiry FROM trades WHERE trade_id = 'C1'"
    ).fetchone()
    assert tuple(confirm) == ("2026-09-24", "2026-09-24 10:16:59", "2026-10-16")
    activity = conn.execute(
        "SELECT trade_date, date_time, expiry FROM trades WHERE trade_id = 'A1'"
    ).fetchone()
    assert tuple(activity) == ("2026-07-24", "2026-07-24 10:00:00", "2026-09-04")
    stmt = conn.execute(
        "SELECT from_date, to_date, when_generated FROM statements"
        " WHERE source_file = 'confirm-20260924.xml'").fetchone()
    assert tuple(stmt) == ("2026-09-24", "2026-09-24", "2026-09-24 11:45:24")
    assert conn.execute(
        "SELECT opened_on FROM journal_entries").fetchone()[0] == "2026-09-24"
    conn.close()
