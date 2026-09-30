"""Tests for archive maintenance.

`prune_archive` is the only operation in the project that deletes archived
source data, so the properties pinned here are safety properties: a dry run
must change nothing, provenance must never dangle, and only byte-identical
files may be removed.
"""

from __future__ import annotations

import pytest
from conftest import add_statement

from optjournal.archive import (
    duplicate_groups,
    prune_archive,
    subsumed_candidates,
)


def write_statement(archive_dir, stamp: str, body: str) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / f"activity-{stamp}.xml").write_text(body)


@pytest.fixture
def archive(tmp_path):
    return tmp_path / "raw"


def add_statement_row(conn, name: str, frm: str, to: str) -> None:
    """A statement row identified by file and period, which is what pruning
    matches on -- the rest of the columns are the shared default."""
    add_statement(conn, source_file=name, from_date=frm, to_date=to)


def add_trade_row(conn, tid: str, source_file: str) -> None:
    conn.execute(
        "INSERT INTO trades (trade_id, ib_exec_id, transaction_id, account_id,"
        " trade_date, asset_category, symbol, quantity, currency,"
        " fx_rate_to_base, raw, source_file, first_seen_at)"
        " VALUES (?,?,?, 'U1', '2026-03-01', 'OPT', 'X', 1, 'USD', 1.0, '{}',"
        " ?, 'now')",
        (tid, f"e{tid}", f"t{tid}", source_file),
    )


def add_snapshot_row(conn, conid: str, source_file: str) -> None:
    conn.execute(
        "INSERT INTO position_snapshots (report_date, conid, account_id, symbol,"
        " asset_category, position, currency, fx_rate_to_base, raw, source_file,"
        " ingested_at)"
        " VALUES ('2026-03-31', ?, 'U1', 'X', 'OPT', 1, 'USD', 1.0, '{}', ?,"
        " 'now')",
        (conid, source_file),
    )


# ----------------------------------------------------------------- grouping


def test_identical_files_are_grouped(archive):
    write_statement(archive, "20260301T000000Z", "<x>same</x>")
    write_statement(archive, "20260302T000000Z", "<x>same</x>")
    groups = duplicate_groups(archive)
    assert len(groups) == 1
    assert groups[0].keep.name == "activity-20260301T000000Z.xml"
    assert [p.name for p in groups[0].redundant] == [
        "activity-20260302T000000Z.xml"
    ]


def test_oldest_is_kept(archive):
    """Deterministic, and it is the copy first-write-wins tables reference."""
    for stamp in ("20260305T000000Z", "20260301T000000Z", "20260303T000000Z"):
        write_statement(archive, stamp, "<x>same</x>")
    assert duplicate_groups(archive)[0].keep.name.endswith("20260301T000000Z.xml")


def test_different_content_is_not_grouped(archive):
    write_statement(archive, "20260301T000000Z", "<x>one</x>")
    write_statement(archive, "20260302T000000Z", "<x>two</x>")
    assert duplicate_groups(archive) == []


def test_same_size_different_content_is_not_grouped(archive):
    """Size is only a prefilter; content must decide."""
    write_statement(archive, "20260301T000000Z", "<x>aaa</x>")
    write_statement(archive, "20260302T000000Z", "<x>bbb</x>")
    assert duplicate_groups(archive) == []


def test_missing_archive_dir_is_safe(tmp_path):
    assert duplicate_groups(tmp_path / "nope") == []


def test_bytes_reclaimed_survives_deletion(archive, conn):
    write_statement(archive, "20260301T000000Z", "<x>same</x>")
    write_statement(archive, "20260302T000000Z", "<x>same</x>")
    size = (archive / "activity-20260302T000000Z.xml").stat().st_size
    result = prune_archive(archive, conn, apply=True)
    assert result.bytes_reclaimed == size, "sizes must be captured before unlink"


# -------------------------------------------------------------- dry run safety


def test_dry_run_deletes_nothing(archive, conn):
    write_statement(archive, "20260301T000000Z", "<x>same</x>")
    write_statement(archive, "20260302T000000Z", "<x>same</x>")
    add_statement_row(conn, "activity-20260302T000000Z.xml", "2026-03-01", "2026-03-31")
    conn.commit()

    result = prune_archive(archive, conn, apply=False)
    assert result.applied is False
    assert result.files_removed == 1, "should still report what it would remove"
    assert len(list(archive.glob("*.xml"))) == 2, "dry run must not delete"
    remaining = conn.execute("SELECT COUNT(*) AS n FROM statements").fetchone()["n"]
    assert remaining == 1, "dry run must not touch the database"


def test_no_duplicates_is_a_noop(archive, conn):
    write_statement(archive, "20260301T000000Z", "<x>one</x>")
    result = prune_archive(archive, conn, apply=True)
    assert result.files_removed == 0
    assert len(list(archive.glob("*.xml"))) == 1


# --------------------------------------------------------------- apply safety


def test_apply_removes_only_redundant_copies(archive, conn):
    for stamp in ("20260301T000000Z", "20260302T000000Z", "20260303T000000Z"):
        write_statement(archive, stamp, "<x>same</x>")
    result = prune_archive(archive, conn, apply=True)
    assert result.files_removed == 2
    survivors = [p.name for p in archive.glob("*.xml")]
    assert survivors == ["activity-20260301T000000Z.xml"]


def test_provenance_is_repointed_not_orphaned(archive, conn):
    """The whole point: rows must never reference a deleted file."""
    keep = "activity-20260301T000000Z.xml"
    drop = "activity-20260302T000000Z.xml"
    write_statement(archive, "20260301T000000Z", "<x>same</x>")
    write_statement(archive, "20260302T000000Z", "<x>same</x>")
    add_statement_row(conn, keep, "2026-03-01", "2026-03-31")
    add_statement_row(conn, drop, "2026-03-01", "2026-03-31")
    add_trade_row(conn, "1", keep)
    # position_snapshots replaces on conflict, so it points at the newest copy.
    add_snapshot_row(conn, "C1", drop)
    conn.commit()

    result = prune_archive(archive, conn, apply=True)

    assert result.rows_repointed.get("position_snapshots") == 1
    assert result.statement_rows_removed == 1
    moved = conn.execute(
        "SELECT source_file FROM position_snapshots"
    ).fetchone()["source_file"]
    assert moved == keep
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_apply_without_a_database_still_prunes_files(archive):
    write_statement(archive, "20260301T000000Z", "<x>same</x>")
    write_statement(archive, "20260302T000000Z", "<x>same</x>")
    result = prune_archive(archive, None, apply=True)
    assert result.files_removed == 1
    assert result.rows_repointed == {}


# ------------------------------------------------------------------- subsumed


def test_subsumed_detects_contained_period(conn):
    add_statement_row(conn, "narrow.xml", "2026-07-02", "2026-07-31")
    add_statement_row(conn, "wide.xml", "2025-08-01", "2026-07-31")
    conn.commit()
    assert subsumed_candidates(conn) == [("narrow.xml", "wide.xml")]


def test_identical_periods_are_not_subsumed(conn):
    """Equal ranges are not evidence of redundancy in either direction."""
    add_statement_row(conn, "a.xml", "2026-07-02", "2026-07-31")
    add_statement_row(conn, "b.xml", "2026-07-02", "2026-07-31")
    conn.commit()
    assert subsumed_candidates(conn) == []


def test_subsumed_files_are_reported_not_deleted(archive, conn):
    write_statement(archive, "20260301T000000Z", "<x>narrow</x>")
    write_statement(archive, "20260302T000000Z", "<x>wide-and-longer</x>")
    add_statement_row(
        conn, "activity-20260301T000000Z.xml", "2026-07-02", "2026-07-31"
    )
    add_statement_row(
        conn, "activity-20260302T000000Z.xml", "2025-08-01", "2026-07-31"
    )
    conn.commit()

    result = prune_archive(archive, conn, apply=True)
    assert result.files_removed == 0, "subsumption alone must never delete"
    assert len(result.subsumed) == 1
    assert len(list(archive.glob("*.xml"))) == 2


def _statement_file(directory, stamp: str, from_date: str, to_date: str):
    path = directory / f"activity-{stamp}.xml"
    path.write_text(
        '<FlexQueryResponse queryName="q" type="AF">\n<FlexStatements count="1">\n'
        f'<FlexStatement accountId="U1" fromDate="{from_date}" toDate="{to_date}">'
        "</FlexStatement></FlexStatements></FlexQueryResponse>"
    )
    return path


def test_newest_statement_is_the_latest_period_not_the_latest_download(tmp_path):
    """A history import downloads 2022 today; the cost report must not read it."""
    from optjournal.archive import newest_statement  # noqa: PLC0415 - local

    current = _statement_file(tmp_path, "20260929T110034Z", "20260831", "20260928")
    _statement_file(tmp_path, "20260929T120000Z", "20220519", "20230518")

    assert newest_statement(tmp_path) == current


def test_newest_statement_breaks_a_period_tie_by_download(tmp_path):
    from optjournal.archive import newest_statement  # noqa: PLC0415 - local

    _statement_file(tmp_path, "20260803T090622Z", "20260702", "20260731")
    full_year = _statement_file(tmp_path, "20260803T091918Z", "20250801", "20260731")

    assert newest_statement(tmp_path) == full_year


# ------------------------------------------ the keeper is the ingested copy (L4)


def add_nav_row(conn, day: str, source_file: str) -> None:
    conn.execute(
        "INSERT INTO equity_summaries (report_date, account_id, currency,"
        " total_base, raw, source_file, ingested_at)"
        " VALUES (?, 'U1', 'EUR', 100.0, '{}', ?, 'now')",
        (day, source_file),
    )


def test_nav_rows_are_repointed_too(archive, conn):
    """L4: `equity_summaries` references `statements` and was not re-pointed, so
    deleting the redundant copy's statement row failed the foreign key."""
    keep = "activity-20260301T000000Z.xml"
    drop = "activity-20260302T000000Z.xml"
    write_statement(archive, "20260301T000000Z", "<x>same</x>")
    write_statement(archive, "20260302T000000Z", "<x>same</x>")
    add_statement_row(conn, keep, "2026-03-01", "2026-03-31")
    add_statement_row(conn, drop, "2026-03-01", "2026-03-31")
    add_nav_row(conn, "20260331", drop)   # replaces on conflict: the newest copy
    conn.commit()

    result = prune_archive(archive, conn, apply=True)

    assert result.rows_repointed.get("equity_summaries") == 1
    assert conn.execute(
        "SELECT source_file FROM equity_summaries").fetchone()[0] == keep
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_the_copy_the_journal_ingested_is_the_one_kept(archive, conn):
    """L4: the oldest NAME was kept even when only its newer twin was ingested.

    That happens when a copy is restored under an older stamp: the ingest skips
    it as byte-identical, so it has no `statements` row. Re-pointing every row at
    it and deleting the ingested copy's row then failed the foreign key.
    """
    ingested = "activity-20260929T110034Z.xml"
    restored = "activity-20260929T090000Z.xml"
    write_statement(archive, "20260929T110034Z", "<x>same</x>")
    write_statement(archive, "20260929T090000Z", "<x>same</x>")
    add_statement_row(conn, ingested, "2026-08-31", "2026-09-28")
    add_trade_row(conn, "1", ingested)
    add_nav_row(conn, "20260928", ingested)
    conn.commit()

    dry = prune_archive(archive, conn, apply=False)
    applied = prune_archive(archive, conn, apply=True)

    for result in (dry, applied):
        assert result.groups[0].keep.name == ingested
        assert [p.name for p in result.groups[0].redundant] == [restored]
    assert [p.name for p in archive.glob("*.xml")] == [ingested]
    assert [r[0] for r in conn.execute("SELECT source_file FROM statements")] == [
        ingested]
    assert conn.execute("SELECT source_file FROM trades").fetchone()[0] == ingested
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
