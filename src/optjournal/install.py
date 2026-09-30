"""Moving a journal into its home, and finding one a previous download left.

WHY THIS EXISTS: a journal used to live beside the code, so every download
unzipped into a new folder started empty. `config.data_home()` now puts it in
the per-user application folder, and this module gets existing journals there.

Two ways a journal arrives, and they are treated differently on purpose:

* BESIDE THIS CODE. The reader unzipped the new version over the old one, or is
  running an old install. Moved by `prepare()` without asking: it is this
  install's own journal, and the launcher runs `prepare()` before the server
  starts, so nothing has it open.
* IN ANOTHER DOWNLOAD. Found by `previous_journals()` in Downloads, Desktop and
  Documents, and moved only after the reader confirms it in the page. Someone
  may keep two journals on purpose, and guessing which one is theirs is not
  this module's decision.

NEVER OVERWRITES A JOURNAL WITH DATA. A home that already has statements is left
alone and the move is refused. A home with an empty journal (what the server
creates on its first start) is set aside, not deleted.

Stdlib and `config` only: `optjournal prepare` runs before the server, and the
fewer imports it needs the fewer ways a broken update can stop it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import sqlite3
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from optjournal import config
from optjournal.config import DATA_NAMES, holds_journal

__all__ = [
    "PENDING_IMPORT",
    "RelocateRefused",
    "choose_import",
    "prepare",
    "previous_journals",
    "relocate",
]

log = logging.getLogger(__name__)

#: The reader's confirmed choice of a previous journal, written by the page and
#: carried out by the next `prepare()`, when the server is no longer running.
PENDING_IMPORT = ".pending-import.json"

#: Where a previous download is looked for, and how deep. Two levels covers
#: `Downloads/optjournal-main` and `Downloads/old/optjournal-main`; deeper walks
#: of a Documents folder cost seconds on a page load for a case nobody has.
SEARCH_DIRS = ("Downloads", "Desktop", "Documents")
SEARCH_DEPTH = 2

#: One database, in the files SQLite keeps it in. Moved, or set aside, together.
DB_FILES = ("journal.db", "journal.db-wal", "journal.db-shm")


class RelocateRefused(RuntimeError):
    """A move would have overwritten a journal, or its source is in use."""


class _Unreadable(RelocateRefused):
    """A `journal.db` SQLite cannot read at all."""


def _statement_count(db: Path, *, foreign: bool = False) -> int:
    """How many statements `db` holds. Zero for no journal, or an unreadable one.

    `foreign` for a journal in ANOTHER folder: read as immutable, because a
    read-only open of a WAL database still creates its `-wal` and `-shm` files,
    and a scan must leave someone else's folder exactly as it found it. Not for
    this install's own journal, whose newest rows may still sit in its WAL.

    The URI comes from `as_uri()`, which escapes the path: interpolated as text,
    a `#` or `?` in a folder name cut the path short and dropped `mode=ro`, so
    SQLite opened, and created, some other file.
    """
    if not db.exists():
        return 0
    try:
        mode = "ro&immutable=1" if foreign else "ro"
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode={mode}", uri=True)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM statements").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return 0          # no statements table: a journal that never synced


def _release(directory: Path) -> None:
    """Fold the WAL into the database file, or raise if anything has it open.

    SQLite only leaves WAL mode when it holds the database alone, so the switch
    IS the in-use test, and asking the database beats guessing from files: a
    WAL file also appears when anyone merely reads the journal. On success the
    `-wal` and `-shm` files are gone and the move carries one file. The app puts
    the journal back into WAL mode when it next opens it.

    A file SQLite cannot read at all is refused too, and left where it is: it
    may still be someone's only copy of something worth recovering.
    """
    db = directory / "journal.db"
    if not db.exists():
        return
    try:
        conn = sqlite3.connect(db, timeout=0)
        try:
            conn.execute("PRAGMA journal_mode=DELETE").fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        raise RelocateRefused(
            f"{directory} is in use: close optjournal and try again ({exc})") from exc
    except sqlite3.DatabaseError as exc:
        raise _Unreadable(f"{db} is not a readable journal ({exc})") from exc


def _copy(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)


def relocate(source: Path, home: Path) -> list[str]:
    """Move a journal's data from `source` into `home`. Returns what moved.

    Raises `RelocateRefused` when `home` already holds statements, when either
    journal is in use or unreadable, or when the move fails partway. An empty
    journal in `home` is moved aside to `home/replaced-<stamp>/`, with its
    `-wal` and `-shm`: left beside the new database, SQLite would apply that
    WAL to it and report the import as malformed.

    ALL OR NOTHING, because half a move splits the journal: `journal.db` in the
    new home and `raw/` still beside the code, which is where the data home then
    resolves. So the source is COPIED into a staging folder first, the staged
    files are renamed into place, and only then are the originals renamed away
    into a folder that is deleted last. Every step up to that deletion is
    undone if any of them fails (a file held by an antivirus scan, a full disk),
    leaving both folders as they were.
    """
    if _statement_count(home / "journal.db"):
        raise RelocateRefused(f"{home} already holds a journal with statements")
    _release(source)
    # An unreadable journal in the home has no statements to protect, and is set
    # aside as it is, with its `-wal` and `-shm`. One in use is refused.
    with contextlib.suppress(_Unreadable):
        _release(home)
    home.mkdir(parents=True, exist_ok=True)

    present = [name for name in DATA_NAMES if (source / name).exists()]
    replaced = {*present, *(DB_FILES if "journal.db" in present else ())}
    clashes = [name for name in DATA_NAMES if name in replaced and (home / name).exists()]
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    staging, aside = home / f".incoming-{stamp}", home / f"replaced-{stamp}"
    moved_away = source / f".moved-{stamp}"
    undo: list[tuple[Path, Path]] = []      # renames done, as (from, to)

    def rename(src: Path, dst: Path) -> None:
        dst.parent.mkdir(exist_ok=True)
        os.rename(src, dst)
        undo.append((src, dst))

    try:
        staging.mkdir()
        for name in present:
            _copy(source / name, staging / name)
        for name in clashes:
            rename(home / name, aside / name)
        for name in present:
            rename(staging / name, home / name)
        for name in present:
            rename(source / name, moved_away / name)
    except OSError as exc:
        stuck = []
        for src, dst in reversed(undo):
            try:
                os.rename(dst, src)
            except OSError as again:
                stuck.append(f"{dst} ({again})")
        for folder in (aside, moved_away):
            with contextlib.suppress(OSError):
                folder.rmdir()                  # only if empty, as it should be
        if not stuck:
            shutil.rmtree(staging, ignore_errors=True)
        detail = f"; could not put back {', '.join(stuck)}" if stuck else ""
        raise RelocateRefused(
            f"could not move the journal from {source} to {home}, so it was "
            f"left in {source} ({exc}){detail}") from exc
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(moved_away, ignore_errors=True)
    if clashes:
        log.info("moved an empty journal's files aside to %s", aside)
    log.info("moved %s from %s to %s", ", ".join(present), source, home)
    return present


def _is_optjournal(directory: Path) -> bool:
    try:
        meta = tomllib.loads((directory / "pyproject.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return meta.get("project", {}).get("name") == "optjournal"


def previous_journals(
    home: Path | None = None, *, search_root: Path | None = None,
) -> list[Path]:
    """Other optjournal downloads holding a journal with statements, newest first.

    Empty unless this journal is empty: a reader with data of their own is not
    offered someone else's. Never lists this install's own folder, which
    `prepare()` handles, or `home` itself.
    """
    home = home or config.home()
    if _statement_count(home / "journal.db"):
        return []
    base = search_root or Path.home()
    found: list[Path] = []
    frontier = [base / name for name in SEARCH_DIRS]
    for _depth in range(SEARCH_DEPTH):
        nxt: list[Path] = []
        for directory in frontier:
            try:
                children = [c for c in directory.iterdir() if c.is_dir()]
            except OSError:
                continue
            for child in children:
                if child.resolve() in (config.ROOT.resolve(), home.resolve()):
                    continue
                if (_is_optjournal(child)
                        and _statement_count(child / "journal.db", foreign=True)):
                    found.append(child)
                else:
                    nxt.append(child)
        frontier = nxt
    return sorted(found, key=lambda p: (p / "journal.db").stat().st_mtime, reverse=True)


def choose_import(source: Path, home: Path | None = None) -> None:
    """Record the reader's choice; `prepare()` moves it when the server is down.

    `source` must be one `previous_journals()` returns. Checked here rather than
    trusted from the request, so the page cannot be used to move an arbitrary
    folder into the journal's home.
    """
    home = home or config.home()
    if source not in previous_journals(home):
        raise RelocateRefused(f"{source} is not a journal this install found")
    home.mkdir(parents=True, exist_ok=True)
    (home / PENDING_IMPORT).write_text(json.dumps({"source": str(source)}))


def prepare(home: Path | None = None, *, code_dir: Path | None = None) -> list[str]:
    """Bring a journal home before the server starts. Returns what happened.

    First a confirmed import from another download, then this install's own
    journal if it still sits beside the code (a download's, never a clone's).
    Each failure is reported as one line and left for the reader rather than
    raised: a journal that could not be moved must still open, from wherever it
    is. A confirmed import is attempted once, whatever happens, so a journal
    that cannot be imported cannot stop every later start too.
    """
    home = home or config.home()
    code_dir = code_dir or config.ROOT
    done: list[str] = []
    pending = home / PENDING_IMPORT
    if pending.exists():
        try:
            source = Path(json.loads(pending.read_text())["source"])
            moved = relocate(source, home)
            done.append(f"imported {', '.join(moved)} from {source}")
        except (RelocateRefused, OSError, ValueError, KeyError) as exc:
            done.append(f"import not done: {exc}")
        finally:
            pending.unlink(missing_ok=True)
    # A git clone is a developer's checkout, not a download: its journal stays
    # where they put it, beside the tests that read it and the launchd agent.
    if (code_dir / ".git").exists():
        return done
    if code_dir.resolve() != home.resolve() and holds_journal(code_dir):
        try:
            moved = relocate(code_dir, home)
            done.append(f"moved {', '.join(moved)} from {code_dir} to {home}")
        except (RelocateRefused, OSError) as exc:
            done.append(f"journal left beside the code: {exc}")
    return done
