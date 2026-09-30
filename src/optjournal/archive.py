"""Archive maintenance: collapse redundant statement files.

Why this is needed at all. IBKR regenerates an Activity Statement once per
calendar day and re-serves identical bytes for the rest of it, but `fetch`
originally wrote a fresh timestamped file per download, so any extra sync
added a byte-for-byte copy. `flex._archive` now dedupes at write time; this
module cleans up what accumulated before that existed, and stays useful for
anything that bypasses it.

Two kinds of redundancy, treated very differently:

* **Byte-identical duplicates.** Provably zero information loss -- the files
  have the same sha256. Safe to delete, and that is what `prune` does.
* **Period-subsumed files**, where one statement's date range sits entirely
  inside another's. Usually redundant, but not provably: a superset range
  from a query template with fewer sections enabled would contain less data
  despite covering more days. These are *reported* and never deleted.

Provenance is preserved rather than broken. Rows in `trades`,
`cash_transactions`, `position_snapshots` and `equity_summaries` carry the
`source_file` they came from, and the tables disagree about which duplicate that
is -- trades and cash are first-write-wins so they point at the oldest copy, while
the snapshots and NAV replace on conflict so they point at the newest. Deleting
files without fixing that would dangle a foreign key and leave rows claiming
to originate from a file that no longer exists. Because the duplicates are
byte-identical, re-pointing them at the retained copy is accurate, not a
fudge: that file contains exactly the same statement.

The retained copy is one the journal has a `statements` row for, when there is
one: a copy restored under an older name was skipped at ingest as a duplicate and
has none, and re-pointing rows at it would leave them referencing nothing.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from optjournal.flex import archive_digest

__all__ = [
    "DuplicateGroup",
    "PruneResult",
    "account_opened",
    "duplicate_groups",
    "newest_statement",
    "prune_archive",
    "subsumed_candidates",
]

#: Tables carrying a source_file provenance column, all of which must be
#: re-pointed before a retained duplicate's siblings are removed. Every table
#: with a foreign key to `statements(source_file)`.
_PROVENANCE_TABLES = ("trades", "cash_transactions", "position_snapshots",
                      "equity_summaries")


@dataclass(slots=True)
class DuplicateGroup:
    """One set of byte-identical archive files."""

    digest: str
    #: The copy to retain. The oldest filename the journal holds a `statements`
    #: row for, or the oldest filename when it holds none (or there is no
    #: journal). Deterministic either way.
    keep: Path
    redundant: list[Path] = field(default_factory=list)
    #: Captured at construction. Reading sizes lazily would report zero once
    #: the files have been unlinked, making the applied run look like a no-op.
    bytes_reclaimed: int = 0


@dataclass(slots=True)
class PruneResult:
    applied: bool
    groups: list[DuplicateGroup]
    rows_repointed: dict[str, int] = field(default_factory=dict)
    statement_rows_removed: int = 0
    subsumed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def files_removed(self) -> int:
        return sum(len(g.redundant) for g in self.groups)

    @property
    def bytes_reclaimed(self) -> int:
        return sum(g.bytes_reclaimed for g in self.groups)


def duplicate_groups(
    archive_dir: Path, conn: sqlite3.Connection | None = None,
) -> list[DuplicateGroup]:
    """Group archived statements by content, returning only real duplicates.

    With a journal, each group keeps a copy the journal ingested (see `keep`).
    """
    if not archive_dir.is_dir():
        return []

    # Hash only within same-size cohorts; distinct sizes cannot collide.
    by_size: dict[int, list[Path]] = defaultdict(list)
    for path in sorted(archive_dir.glob("activity-*.xml")):
        by_size[path.stat().st_size].append(path)

    by_digest: dict[str, list[Path]] = defaultdict(list)
    for paths in by_size.values():
        if len(paths) < 2:
            continue
        for path in paths:
            by_digest[archive_digest(path)].append(path)

    ingested = _ingested_names(conn)
    groups = []
    for digest, paths in by_digest.items():
        if len(paths) < 2:
            continue
        ordered = sorted(paths)
        keep = next((p for p in ordered if p.name in ingested), ordered[0])
        redundant = [p for p in ordered if p != keep]
        groups.append(
            DuplicateGroup(
                digest=digest,
                keep=keep,
                redundant=redundant,
                bytes_reclaimed=sum(p.stat().st_size for p in redundant),
            )
        )
    return sorted(groups, key=lambda g: g.keep.name)


def _ingested_names(conn: sqlite3.Connection | None) -> set[str]:
    if conn is None:
        return set()
    try:
        return {r[0] for r in conn.execute("SELECT source_file FROM statements")}
    except sqlite3.OperationalError:
        return set()


def subsumed_candidates(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Files whose period sits entirely inside another statement's period.

    Reported for judgement, never deleted -- see the module docstring for why
    a wider date range does not guarantee a superset of the data.
    """
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT source_file, from_date, to_date FROM statements"
            )
        ]
    except sqlite3.OperationalError:
        return []

    out: list[tuple[str, str]] = []
    for inner in rows:
        for outer in rows:
            if inner["source_file"] == outer["source_file"]:
                continue
            wider = (
                outer["from_date"] <= inner["from_date"]
                and outer["to_date"] >= inner["to_date"]
            )
            strictly = (
                outer["from_date"] < inner["from_date"]
                or outer["to_date"] > inner["to_date"]
            )
            if wider and strictly:
                out.append((inner["source_file"], outer["source_file"]))
                break
    return sorted(out)


def prune_archive(
    archive_dir: Path,
    conn: sqlite3.Connection | None = None,
    *,
    apply: bool = False,
) -> PruneResult:
    """Collapse byte-identical archive duplicates.

    Dry run unless `apply=True`, because this is the only operation in the
    project that deletes archived source data. Provenance is re-pointed to the
    retained copy before anything is removed, and the whole database change is
    one transaction so a failure cannot leave rows pointing at a deleted file.

    The groups, and so the keep and drop lists a dry run reports, are chosen
    the same way in both modes, so the dry run describes what --apply does.
    """
    groups = duplicate_groups(archive_dir, conn)
    result = PruneResult(applied=apply, groups=groups)
    if not apply or not groups:
        # Dry run: report against current state, nothing has changed.
        result.subsumed = subsumed_candidates(conn) if conn is not None else []
        return result

    if conn is not None:
        try:
            for group in groups:
                names = [p.name for p in group.redundant]
                marks = ",".join("?" for _ in names)
                for table in _PROVENANCE_TABLES:
                    cur = conn.execute(
                        f"UPDATE {table} SET source_file = ?"
                        f" WHERE source_file IN ({marks})",
                        (group.keep.name, *names),
                    )
                    if cur.rowcount:
                        result.rows_repointed[table] = (
                            result.rows_repointed.get(table, 0) + cur.rowcount
                        )
                cur = conn.execute(
                    f"DELETE FROM statements WHERE source_file IN ({marks})", names
                )
                result.statement_rows_removed += cur.rowcount
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise

    # Files are unlinked only after the database agrees, so an interrupted run
    # leaves redundant files present rather than rows pointing at nothing.
    for group in groups:
        for path in group.redundant:
            path.unlink(missing_ok=True)

    # Reported from post-prune state, so the list never names a file this run
    # has just deleted.
    result.subsumed = subsumed_candidates(conn) if conn is not None else []
    return result


#: How much of a statement to read for its header. The `FlexStatement` and
#: `AccountInformation` tags are the third and fourth lines of every archived
#: file: `toDate` sits at byte 149 and `dateOpened` well inside the first KB in
#: both a daily and a full-year statement. 4 KB leaves room for a long name.
_HEADER_BYTES = 4096
_TO_DATE = re.compile(rb'<FlexStatement [^>]*?toDate="(\d{8})"')
_DATE_OPENED = re.compile(rb'<AccountInformation [^>]*?dateOpened="(\d{8})"')


def _header_date(path: Path, pattern: re.Pattern[bytes]) -> str:
    """One YYYYMMDD attribute from a statement's header. Empty when unreadable."""
    try:
        with path.open("rb") as fh:
            head = fh.read(_HEADER_BYTES)
    except OSError:
        return ""
    found = pattern.search(head)
    return found.group(1).decode() if found else ""


def _period_end(path: Path) -> str:
    return _header_date(path, _TO_DATE)


def account_opened(archive_dir: Path) -> str | None:
    """The day the account was opened, as YYYYMMDD, from the newest statement.

    What a history import stops at: IBKR keeps four previous calendar years, but
    a younger account has nothing before its opening, and asking for it would
    spend a request to be refused.
    """
    newest = newest_statement(archive_dir)
    return (_header_date(newest, _DATE_OPENED) or None) if newest else None


def newest_statement(archive_dir: Path) -> Path | None:
    """The statement covering the latest period, or None if the archive is empty.

    Here rather than in `serialize.py`, which is where it used to sit: it
    returns a `Path`, and that module's contract is "take domain objects and
    return JSON-safe dicts". A statement-store question belongs with the
    statement store, beside the dedupe and prune that answer the others. Both
    entry points asked `serialize` for a filesystem fact, which is the sort of
    import that makes a layer look like it does more than it does.

    ORDERED BY THE PERIOD, NOT THE FETCH. The filename stamp says when a file
    was downloaded, and a history import downloads 2022 today: ordered by stamp,
    the page's cost report would read a four-year-old statement until the next
    daily sync. The stamp still breaks ties, so of two statements ending on the
    same day the later download wins, which is what the stamp order gave before.
    """
    files = sorted(archive_dir.glob("activity-*.xml"),
                   key=lambda p: (_period_end(p), p.name))
    return files[-1] if files else None
