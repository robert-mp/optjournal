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
`cash_transactions` and `position_snapshots` carry the `source_file` they came
from, and the three tables disagree about which duplicate that is -- trades
and cash are first-write-wins so they point at the oldest copy, while
position_snapshots replaces on conflict so it points at the newest. Deleting
files without fixing that would dangle a foreign key and leave rows claiming
to originate from a file that no longer exists. Because the duplicates are
byte-identical, re-pointing them at the retained copy is accurate, not a
fudge: that file contains exactly the same statement.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from optjournal.flex import archive_digest

__all__ = [
    "DuplicateGroup",
    "PruneResult",
    "duplicate_groups",
    "prune_archive",
    "subsumed_candidates",
]

#: Tables carrying a source_file provenance column, all of which must be
#: re-pointed before a retained duplicate's siblings are removed.
_PROVENANCE_TABLES = ("trades", "cash_transactions", "position_snapshots")


@dataclass(slots=True)
class DuplicateGroup:
    """One set of byte-identical archive files."""

    digest: str
    #: The copy to retain. Deterministically the oldest filename, which is
    #: also the one first-write-wins tables already reference.
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


def duplicate_groups(archive_dir: Path) -> list[DuplicateGroup]:
    """Group archived statements by content, returning only real duplicates."""
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

    groups = []
    for digest, paths in by_digest.items():
        if len(paths) < 2:
            continue
        ordered = sorted(paths)
        redundant = ordered[1:]
        groups.append(
            DuplicateGroup(
                digest=digest,
                keep=ordered[0],
                redundant=redundant,
                bytes_reclaimed=sum(p.stat().st_size for p in redundant),
            )
        )
    return sorted(groups, key=lambda g: g.keep.name)


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
    """
    groups = duplicate_groups(archive_dir)
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
