"""Tests for persistence, ingest idempotency and the grouping views.

Ingest runs against the real archived statements, because the behaviour that
matters -- overlapping statements re-presenting the same fills -- only exists
in genuine data. View maths is tested with hand-built rows so expectations
are computable by hand.
"""

from __future__ import annotations

import pytest
from conftest import STATEMENTS, add_statement

from optjournal.db import SCHEMA_VERSION, connect, migrate
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


@pytest.mark.skipif(not STATEMENTS, reason="no archived statements")
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
    assert len(cats) > 1, "the real archive holds stock and FX besides options"


@pytest.mark.skipif(not STATEMENTS, reason="no archived statements")
def test_ingest_can_still_narrow_to_options(conn):
    r = ingest_file(conn, STATEMENTS[-1], assets=("OPT",))
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

    from optjournal.ingest import _qty

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
    raw = src.read_text()
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
    raw = src.read_text()
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
