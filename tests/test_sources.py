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
