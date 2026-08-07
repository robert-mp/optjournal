"""The broker seam: statements in, broker-neutral fills out.

`ingest` reads `NormalisedFill`s from a `StatementSource` rather than py_ibkr
models, so a second broker is a new source and an unchanged writer. These tests
pin the two properties that makes true: the IBKR source produces exactly the
fills the old direct-read ingest did, and the registry refuses an unknown broker
loudly rather than journalling a statement under the wrong parser.
"""

from __future__ import annotations

import dataclasses

import pytest
from conftest import STATEMENTS, connect_migrated

from optjournal.db import DEFAULT_BROKER
from optjournal.fills import NormalisedFill
from optjournal.ingest import ingest_file
from optjournal.sources import SOURCES, IbkrSource, source_for


def test_the_registry_refuses_an_unknown_broker():
    """Unlike stats.SCOPE_BUILDERS, this does NOT fall open.

    An unrecognised trade-type scope should show the whole journal; an
    unrecognised broker means the caller asked for a reader this build lacks, and
    parsing a statement with the wrong source is worse than stopping. The error
    names what is available so the caller can see the typo.
    """
    with pytest.raises(ValueError, match="no statement source for broker 'schwab'"):
        source_for("schwab")
    # And it lists the known ones.
    with pytest.raises(ValueError, match="ibkr"):
        source_for("schwab")


def test_ibkr_is_registered_under_the_default_broker():
    """The default broker must resolve, or every existing call site breaks."""
    assert source_for(DEFAULT_BROKER) is SOURCES[DEFAULT_BROKER]
    assert isinstance(source_for(DEFAULT_BROKER), IbkrSource)


def test_a_source_yields_broker_neutral_fills():
    """The seam's whole point: no IBKR vocabulary crosses it.

    `NormalisedFill` names fields in trading terms (`exec_id`, `realized_pnl`),
    and a source fills them from its own broker's shape. Asserting the type and a
    couple of fields is enough -- the byte-for-byte equivalence is the next test.
    """
    if not STATEMENTS:
        pytest.skip("needs an archived statement")
    source = source_for(DEFAULT_BROKER)
    account, fills = next(source.statements(STATEMENTS[-1]))
    assert account.startswith("U"), "IBKR account ids look like U..."
    assert fills, "the newest statement has trades"
    fill = fills[0]
    assert isinstance(fill, NormalisedFill)
    # Broker-neutral names carry values; the raw dict keeps the source's own.
    assert fill.trade_id and fill.exec_id
    assert isinstance(fill.raw, dict) and fill.raw, "raw column source missing"


def test_the_seam_ingests_byte_identically_to_the_direct_read(tmp_path):
    """Every trade row through the seam equals what py_ibkr-direct ingest wrote.

    This is the guarantee that made the extraction safe: the parsing did not
    move, only the attribute reads. Compared on the trades table because that is
    the only table the seam touches, and with `first_seen_at` dropped because it
    is a wall-clock stamp rather than data.

    Pinned as a stored fingerprint: the direct-read code is gone, so the only
    thing to compare against is the value it produced, captured here as a
    constant. If a future change to the IBKR source alters an ingested value,
    this fails -- which is what a seam that claims to be transparent must prove.
    """
    if not STATEMENTS:
        pytest.skip("needs an archived statement")
    import hashlib
    import json

    conn = connect_migrated(tmp_path / "seam.db")
    for path in STATEMENTS:
        ingest_file(conn, path)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM trades ORDER BY broker, trade_id")]
    for r in rows:
        r.pop("first_seen_at", None)
    fingerprint = hashlib.sha256(
        json.dumps(rows, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    assert len(rows) == 160, "the real archive holds 160 trades"
    assert fingerprint == "2deefcc5353a4955", (
        "the trades the IBKR source ingests differ from what the direct-read "
        "ingest produced; the seam is no longer transparent"
    )


def test_every_normalised_fill_field_maps_to_a_trades_column():
    """A field on the fill that the writer forgot is a value silently dropped.

    Not every trades column comes from a fill -- broker, source_file,
    first_seen_at and the `_base` translations are the journal's own -- but every
    fill field except `raw` must land somewhere, or adding one to the type would
    quietly do nothing.
    """
    import inspect

    from optjournal import ingest

    fill_fields = {f.name for f in dataclasses.fields(NormalisedFill)} - {"raw"}
    writer = inspect.getsource(ingest._ingest_trades)
    missing = sorted(f for f in fill_fields if f"fill.{f}" not in writer)
    assert not missing, f"_ingest_trades never reads these fill fields: {missing}"


# --------------------------------------------- a second broker, end to end
#
# Every test above exercises ONE broker, which is how the seam came to look
# finished while three layers still assumed IBKR was the only source. These
# drive a real statement through the whole pipeline twice, once under each of
# two broker names, and assert the two stay separate.
#
# Registering a second source that happens to reuse the IBKR reader is the
# point: it holds the DATA identical so any difference in the output is the
# journal's handling of `broker` and nothing else. Identical trade ids, order
# ids and conids across two brokers is precisely the collision the composite
# keys exist for, and it is the case a real second broker makes possible on
# day one.


@pytest.fixture
def two_brokers(tmp_path, monkeypatch):
    """A journal holding the same statement under two broker names.

    Yields (conn, single_broker_counts) so each test can compare against what
    one broker alone produced, rather than against a hard-coded number that
    would drift with the archive.
    """
    if not STATEMENTS:
        pytest.skip("needs an archived statement")

    class SecondSource(IbkrSource):
        broker = "testbroker"

    monkeypatch.setitem(SOURCES, "testbroker", SecondSource())

    conn = connect_migrated(tmp_path / "two.db")
    statement = STATEMENTS[-1]
    ingest_file(conn, statement, assets=())

    def count(sql: str) -> int:
        return conn.execute(f"SELECT COUNT(*) AS n FROM {sql}").fetchone()["n"]

    before = {t: count(t) for t in (
        "trades", "cash_transactions", "position_snapshots",
        "trade_legs", "trade_orders", "current_option_positions",
    )}

    # A copy under a new name: same bytes, different broker. The digest guard
    # must not treat the other broker's archive as a reason to skip.
    twin = tmp_path / "second-broker.xml"
    twin.write_bytes(statement.read_bytes())
    result = ingest_file(conn, twin, assets=(), broker="testbroker")
    assert not result.already_ingested, (
        "the second broker's statement was skipped as a duplicate; the digest "
        "guard is reasoning across brokers"
    )
    assert result.trades_inserted > 0, "the second broker stored no trades"
    return conn, before


def test_a_second_brokers_rows_are_stored_not_swallowed(two_brokers):
    """Same identifiers, two brokers, nothing lost.

    `ON CONFLICT ... DO NOTHING` is the hazard: an id collision does not raise,
    it silently discards the row and reports it as already-seen. So this asserts
    each table DOUBLED, per broker, rather than merely that the ingest exited 0.
    """
    conn, before = two_brokers
    for table in ("trades", "cash_transactions", "position_snapshots"):
        rows = {
            r["broker"]: r["n"] for r in conn.execute(
                f"SELECT broker, COUNT(*) AS n FROM {table} GROUP BY broker")
        }
        assert set(rows) == {DEFAULT_BROKER, "testbroker"}, f"{table}: {rows}"
        assert rows[DEFAULT_BROKER] == rows["testbroker"], (
            f"{table}: identical statements gave {rows} -- rows were dropped"
        )
        total = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        assert total == before[table] * 2, f"{table}: {total} != {before[table]} x 2"


def test_the_views_do_not_merge_two_brokers_fills(two_brokers):
    """The read side, which is where this failed worst.

    The rows were being stored correctly all along; `trade_legs` GROUPed by
    `(ib_order_id, conid)` and summed two brokers' fills into one leg -- 18
    trades collapsed to 8 legs carrying DOUBLED quantities, a position of -3
    reported as -6. Nothing raises, and a doubled quantity is not obviously
    wrong on sight, which is why this asserts the per-broker quantity rather
    than just the row count.
    """
    conn, before = two_brokers
    # Not current_option_positions: both statements here share a report_date, so
    # its global-MAX bug is invisible until one broker lags. That case is the
    # next test, deliberately separate -- a count assertion here would pass
    # against the broken view and read as coverage it does not have.
    for view in ("trade_legs", "trade_orders"):
        total = conn.execute(f"SELECT COUNT(*) AS n FROM {view}").fetchone()["n"]
        assert total == before[view] * 2, (
            f"{view}: {total} rows, expected {before[view] * 2} -- two brokers "
            f"are being folded into one"
        )

    # Same order id under both brokers must keep its own quantity, not their sum.
    pairs = conn.execute(
        "SELECT ib_order_id, conid, COUNT(DISTINCT broker) AS brokers,"
        " COUNT(DISTINCT quantity) AS quantities"
        " FROM trade_legs GROUP BY ib_order_id, conid"
    ).fetchall()
    assert pairs, "no legs to compare"
    for row in pairs:
        assert row["brokers"] == 2, (
            f"order {row['ib_order_id']} is not present for both brokers"
        )
        assert row["quantities"] == 1, (
            f"order {row['ib_order_id']} has different quantities per broker; "
            f"identical statements must produce identical legs"
        )


def test_a_lagging_brokers_positions_are_still_current(two_brokers):
    """"Most recent snapshot" is per broker, or the broker who lags disappears.

    The subtle one, and the reason it needs its own test: a single
    `MAX(report_date)` over the whole table is HARMLESS while both brokers file
    on the same date, which is exactly what identical fixture statements do. So
    the lag has to be simulated, and without it the assertions below pass against
    the broken code -- verified by ablation, not assumed.

    Two consumers take that MAX independently, and the second is the dangerous
    one. `current_option_positions` merely shows a shorter book. `history._held`
    DECIDES open versus closed, so a dropped broker has every episode judged
    against an empty holding and a position still open reads as CLOSED. Measured
    before the fix: 10 snapshot rows in, 5 out, all one broker.
    """
    from optjournal.history import _held, build_history

    conn, before = two_brokers
    conn.execute(
        "UPDATE position_snapshots SET report_date = '20200101' WHERE broker = ?",
        ("testbroker",),
    )
    conn.commit()

    # The view: both brokers' latest, not one date applied to both.
    in_view = conn.execute(
        "SELECT broker, COUNT(*) AS n FROM current_option_positions GROUP BY broker"
    ).fetchall()
    assert {r["broker"] for r in in_view} == {DEFAULT_BROKER, "testbroker"}, (
        f"current_option_positions holds only {[r['broker'] for r in in_view]}; "
        f"one broker's report_date is deciding what is current for the other"
    )
    total = sum(r["n"] for r in in_view)
    assert total == before["current_option_positions"] * 2, (
        f"{total} rows, expected {before['current_option_positions'] * 2}"
    )

    held, as_of = _held(conn, "OPT")
    brokers = {key[0] for key in held}
    assert brokers == {DEFAULT_BROKER, "testbroker"}, (
        f"only {brokers} in the held book; the lagging broker was dropped"
    )
    # The label is the newest statement across brokers, not the oldest.
    assert as_of != "20200101"

    report = build_history(conn, asset_category="OPT")
    open_by_broker = {}
    for episode in report.open:
        open_by_broker[episode.broker] = open_by_broker.get(episode.broker, 0) + 1
    assert open_by_broker.get("testbroker") == open_by_broker.get(DEFAULT_BROKER), (
        f"open positions differ per broker ({open_by_broker}) on identical "
        f"statements -- the lagging broker's book was closed out"
    )
