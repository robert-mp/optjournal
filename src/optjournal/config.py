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

from pathlib import Path

#: The project directory (the parent of ``src/``), where the journal's data
#: lives beside the code. This is a personal local tool, not a service: data
#: next to code keeps the whole journal one directory to back up.
ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_ARCHIVE = ROOT / "raw"
DEFAULT_DB = ROOT / "journal.db"

#: Synthetic data from ``optjournal demo``. See the module docstring.
DEFAULT_DEMO_DIR = ROOT / "demo"
DEFAULT_DEMO_DB = ROOT / "demo" / "journal.db"

__all__ = [
    "DEFAULT_ARCHIVE",
    "DEFAULT_DB",
    "DEFAULT_DEMO_DB",
    "DEFAULT_DEMO_DIR",
    "ROOT",
]
