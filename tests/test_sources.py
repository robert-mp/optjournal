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
from conftest import ROOT, STATEMENTS, connect_migrated

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

    Pinned PER TRADE, keyed by trade id, rather than as one hash of the whole
    table. The first version hashed everything and asserted a row count, so it
    broke the moment the archive grew -- which it does every time a statement is
    fetched, for reasons that have nothing to do with the seam. A guard that fails
    on new DATA teaches you to re-baseline it, and a guard you re-baseline on
    reflex is not a guard.

    The constants below are not "whatever the code produces today". They were
    derived by checking out `dcb49d5` -- the commit before the seam existed -- into
    a worktree, running its DIRECT-READ ingest over the current archive, and
    hashing the resulting rows. Both versions produce byte-identical values,
    including for a fill fetched from IBKR long after the direct-read code was
    deleted. That is what makes this a comparison rather than a snapshot.

    `broker` is excluded from the hash because the pre-seam schema had no such
    column, and `first_seen_at` because it is a wall clock rather than data.
    """
    if not STATEMENTS:
        pytest.skip("needs an archived statement")
    import hashlib
    import json

    #: trade_id -> sha256 (first 16) of the row as the PRE-SEAM ingest wrote it.
    #: Checked when present, ignored when absent: a pruned archive is not a seam
    #: failure, and nothing can attest to what deleted code would have done with a
    #: statement it never saw. Reproduce with:
    #:   git worktree add --detach /tmp/preseam dcb49d5
    #:   then ingest raw/ with /tmp/preseam/src on sys.path.
    baseline = {
        # The 68,683.68 close. Deliberately included: its commission is billed in
        # a currency the instrument does not trade in, which is the conversion the
        # seam had to preserve exactly.
        "1439867164": "49d61e3ef8e92ce2",
        "1404562790": "16a6b4b6a94535ad",   # SIVE open, 1,400 shares
        # Fetched 2026-08-07, so the direct-read code never saw this statement --
        # yet both versions agree on the row. The strongest form of the claim.
        "1534769849": "572d8269a9a2471e",   # PLTR 260918P130, sold to open
    }

    conn = connect_migrated(tmp_path / "seam.db")
    for path in STATEMENTS:
        ingest_file(conn, path)
    rows = {
        str(r["trade_id"]): dict(r)
        for r in conn.execute("SELECT * FROM trades ORDER BY broker, trade_id")
    }
    assert rows, "the archive should hold trades"

    checked = 0
    for trade_id, expected in baseline.items():
        row = rows.get(trade_id)
        if row is None:
            continue
        row.pop("first_seen_at", None)
        row.pop("broker", None)
        actual = hashlib.sha256(
            json.dumps(row, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        assert actual == expected, (
            f"trade {trade_id} ({row.get('symbol')}) no longer ingests to the "
            f"value the pre-seam direct read produced; the seam is not transparent"
        )
        checked += 1

    assert checked, (
        f"none of the baselined trades {sorted(baseline)} are in the archive any "
        f"more, so this test is asserting nothing -- re-baseline it against rows "
        f"that are present, or delete it"
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
        "securities", "equity_summaries",
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
    # All five row tables, because all five were keyed on an identifier that is
    # the BROKER's rather than universal, and each was found separately: trades
    # first, then cash and snapshots, then securities and NAV. `securities` and
    # `equity_summaries` UPSERT rather than DO NOTHING, so their failure is worse
    # than a dropped row -- the second broker's contract 12345 overwrites the
    # first's definition, and its NAV overwrites that day's account value.
    for table in ("trades", "cash_transactions", "position_snapshots",
                  "securities", "equity_summaries"):
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


def test_a_broker_overwriting_anothers_contract_definition_is_impossible(two_brokers):
    """`securities` and `equity_summaries` UPSERT, which makes them the worst case.

    A dropped row at least leaves the first broker's data intact. An upsert keyed
    without the broker REPLACES it: the second broker's contract 12345 becomes the
    definition of the first's, so `bars.underlying_ids` resolves a symbol to the
    wrong contract and price history is attributed to an instrument that never
    traded. Same shape for NAV, where the survivor becomes the denominator of
    "gain as % of net liquidation" for both accounts' P&L.

    Asserted by making the second broker's rows DIFFER. Identical statements
    cannot show an overwrite -- the replacement value equals the original -- so
    this rewrites one broker's rows and then checks the other's are untouched.
    """
    conn, _ = two_brokers
    conn.execute("UPDATE securities SET symbol = 'CLOBBERED' WHERE broker = ?",
                 ("testbroker",))
    conn.execute("UPDATE equity_summaries SET total_base = -1 WHERE broker = ?",
                 ("testbroker",))
    conn.commit()

    survivors = conn.execute(
        "SELECT COUNT(*) AS n FROM securities WHERE broker = ? AND symbol = 'CLOBBERED'",
        (DEFAULT_BROKER,),
    ).fetchone()["n"]
    assert survivors == 0, (
        "one broker's securities rows changed when the other's were rewritten"
    )
    navs = conn.execute(
        "SELECT COUNT(*) AS n FROM equity_summaries WHERE broker = ? AND total_base = -1",
        (DEFAULT_BROKER,),
    ).fetchone()["n"]
    assert navs == 0, "one broker's NAV changed when the other's was rewritten"


def test_ingest_reads_no_broker_vocabulary_of_its_own():
    """The structural claim: adding a broker is a new source, not an ingest edit.

    `ingest.py` must not import the IBKR parsers and must not name py_ibkr's
    camelCase fields in CODE. Enforced statically because that is what the claim
    IS -- a runtime test cannot distinguish "never reads an IBKR field" from
    "happened not to hit one on this fixture".

    Over the AST rather than the text, so prose is not scanned as code: the
    commission rule's docstring legitimately explains what `fxRateToBase` is and
    why it cannot be trusted for a commission, and a text search fails on that
    explanation. Attribute names, string constants and imports are checked;
    comments and docstrings are not, since documenting a broker's vocabulary is
    the opposite of depending on it.

    The seam looked finished for a whole commit while four defects hid in it, and
    this is the guard that keeps its remaining half from drifting back: a new
    section wired straight into the writer fails here, rather than at the point
    someone tries to add a second broker.
    """
    import ast

    tree = ast.parse((ROOT / "src" / "optjournal" / "ingest.py").read_text("utf-8"))

    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    for module in ("optjournal.flex", "optjournal.sections"):
        assert module not in imported, (
            f"ingest.py imports {module} again -- statement parsing belongs to "
            f"sources.py, the one place that knows a broker's vocabulary"
        )

    # Attribute accesses (`t.fxRateToBase`) and string constants (a section name,
    # or a `row.get(\"camelCase\")` key). Docstrings are Constants too, so only
    # short ones are treated as identifiers -- a paragraph is prose.
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and len(node.value) < 40):
            used.add(node.value)

    ibkr = {"fxRateToBase", "assetCategory", "reportDate", "costBasisMoney",
            "costBasisPrice", "transactionID", "tradeID", "ibExecID", "ibOrderID",
            "ibCommission", "fifoPnlRealized", "fifoPnlUnrealized", "positionValue",
            "markPrice", "openDateTime", "underlyingSymbol", "subCategory",
            "listingExchange", "whenGenerated", "accountId",
            "CashTransactions", "OpenPositions", "SecuritiesInfo",
            "EquitySummaryInBase", "AccountInformation", "FlexStatements"}
    leaked = sorted(ibkr & used)
    assert not leaked, (
        f"ingest.py reads IBKR's own vocabulary: {leaked}. Read it in sources.py "
        f"and hand the writer a normalised shape."
    )
