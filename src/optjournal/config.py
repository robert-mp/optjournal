"""Filesystem defaults, in one place.

These lived in cli.py, which made the CLI module the thing everything --
web server, demo generator, tests -- had to import to learn where the
journal lives. Path policy is configuration, not command-line handling,
and a caller embedding the library (a cron, a notebook) should not have
to touch argparse machinery to find the database.

Layout policy, and why it is not obvious:

* ``raw/`` is the provenance root. Every report is ultimately derived from
  the statements in it, they are deduplicated by content hash, and
  replacing one costs an IBKR request against a lockout budget -- which
  makes them the least replaceable artefact in the tree.
* ``demo/`` holds synthetic data and is gitignored. It sits *beside* the
  real archive, never inside it: a generated statement landing in ``raw/``
  would be indistinguishable from a real one afterwards.
  ``optjournal.demo.assert_not_real`` enforces the separation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: The code directory (the parent of ``src/``). What an update replaces, and
#: where a journal lived before it had a home of its own.
ROOT = Path(__file__).resolve().parent.parent.parent

#: Points the whole journal somewhere else: the database, the archive, the
#: settings file and the logs. Tests use it to keep off the developer's data.
HOME_ENV = "OPTJOURNAL_HOME"

#: What a journal is made of, beside the code or in its home. Named once because
#: three things need the same list: finding a journal, moving one, and the
#: updater's refusal to overwrite any of them.
DATA_NAMES = (
    "journal.db", "journal.db-wal", "journal.db-shm", "raw", ".optjournal.json",
    "snapshots", "logs", "demo",
)


def platform_home() -> Path:
    """Where a journal lives when nothing says otherwise: the OS's per-user
    application data folder, which no download, unzip or update touches."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "optjournal"


def holds_journal(directory: Path) -> bool:
    """Whether `directory` holds a journal's data, as opposed to just code."""
    return any((directory / name).exists()
               for name in ("journal.db", "raw", ".optjournal.json"))


def home() -> Path:
    """Where a journal belongs: `$OPTJOURNAL_HOME`, else the per-user folder.

    What `install` moves a journal INTO. `data_home()` differs only while an old
    install still keeps its journal beside the code.
    """
    override = os.environ.get(HOME_ENV)
    return Path(override) if override else platform_home()


def data_home() -> Path:
    """The directory the journal's data lives in, resolved at call time.

    `$OPTJOURNAL_HOME` first. Then the code directory IF it already holds a
    journal: every install before this one kept its data beside the code, and a
    developer's clone still does, so nothing moves under a running launchd agent
    or a test suite. `optjournal prepare`, which the launcher runs before the
    server starts, is what moves such a journal home. Otherwise the per-user
    folder, so a download unzipped anywhere finds the same journal.
    """
    if not os.environ.get(HOME_ENV) and holds_journal(ROOT):
        return ROOT
    return home()


DATA_HOME = data_home()
DEFAULT_ARCHIVE = DATA_HOME / "raw"
DEFAULT_DB = DATA_HOME / "journal.db"

#: Synthetic data from ``optjournal demo``. See the module docstring.
DEFAULT_DEMO_DIR = DATA_HOME / "demo"
DEFAULT_DEMO_DB = DATA_HOME / "demo" / "journal.db"

__all__ = [
    "DATA_HOME",
    "DATA_NAMES",
    "DEFAULT_ARCHIVE",
    "DEFAULT_DB",
    "DEFAULT_DEMO_DB",
    "DEFAULT_DEMO_DIR",
    "HOME_ENV",
    "ROOT",
    "data_home",
    "holds_journal",
    "home",
    "platform_home",
]
