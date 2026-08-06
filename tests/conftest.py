"""Shared fixtures and builders for the suite.

What belongs here: the setup that says nothing about the thing under test.
Five test modules each defined `RAW_DIR` and `STATEMENTS` with the same two
lines, four defined a `conn` fixture that opened and migrated a database, and
seven wrote out the same eight-column `INSERT INTO statements` with different
values -- so a schema change to that table meant seven edits, and the copies
had already drifted (`asset_filter` was 'OPT' in five and 'ALL' in one, for no
reason either stated).

What deliberately does NOT belong here: anything a test is asserting about.
The per-module builders that insert *trades* stay in their own files, because
what a trade row contains is the subject of those tests rather than scaffolding
for them -- `test_history` needs fills with notes and dispositions,
`test_archive` needs provenance columns, and folding those into one builder
would produce a function with a dozen parameters that no reader could follow
back to the behaviour it exercises.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from optjournal.db import connect, migrate
from optjournal.ingest import ASSET_FILTER_ALL, ingest_file

#: The real archive, which the suite uses as its fixture corpus: these are
#: statements IBKR actually served, so they are the only source of true rates,
#: mixed currencies and IBKR's own spelling of every field.
RAW_DIR = Path(__file__).resolve().parent.parent / "raw"

#: Sorted so a test that takes "the newest" gets the same file on every machine.
STATEMENTS = sorted(RAW_DIR.glob("activity-*.xml"))

#: The project root, for tests reaching source files rather than data.
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    """An empty migrated journal. No rows, so a test states its own data."""
    return connect_migrated(tmp_path / "journal.db")


@pytest.fixture
def populated_db(tmp_path) -> Path:
    """A database with every archived statement ingested, as the CLI leaves it.

    Returns the PATH, not a connection: the web layer opens its own connection
    per request (sqlite3 handles cannot cross threads), so a test that handed it
    a live handle would be testing something the server never does.
    """
    if not STATEMENTS:
        pytest.skip("needs an archived statement")
    db = tmp_path / "journal.db"
    c = connect_migrated(db)
    for path in STATEMENTS:
        ingest_file(c, path, assets=ASSET_FILTER_ALL)
    c.close()
    return db


def connect_migrated(path: Path) -> sqlite3.Connection:
    """Open a journal at `path` and bring the schema up to date."""
    c = connect(path)
    migrate(c)
    return c


def add_statement(
    conn: sqlite3.Connection,
    *,
    source_file: str = "t.xml",
    sha256: str = "x",
    account_id: str = "U1",
    from_date: str = "2025-01-01",
    to_date: str = "2026-12-31",
    base_currency: str = "EUR",
    asset_filter: str = "OPT",
) -> None:
    """Insert the statement row that trade and snapshot rows hang off.

    Every column is a keyword with a default, so a test names only what it is
    actually about -- a date range when it is testing period logic, an
    `asset_filter` when it is testing scoping -- and stays silent about the six
    it does not care about. Written once because the column list is the
    schema's, not any one test's: adding a column here used to mean editing
    seven literals.
    """
    conn.execute(
        "INSERT INTO statements (source_file, sha256, account_id, from_date,"
        " to_date, base_currency, asset_filter, ingested_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'now')",
        (source_file, sha256, account_id, from_date, to_date,
         base_currency, asset_filter),
    )
