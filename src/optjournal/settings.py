"""Preferences that outlive a process, and the one rule for reading them.

A journal has three kinds of configuration and only this module's kind is
persisted here:

* the SECRET (the Flex token) lives in the OS keyring, never on disk in this
  tree -- see `flex.read_token`;
* the LAYOUT (where the database and archive are) is policy, not preference,
  and lives in `config.py` as constants;
* everything else is a CHOICE a person made once and should not have to make
  again -- the Flex query id, the scoreboard's unit. That is this file.

Why a file at all, when the query id already had two channels. `--query-id`
serves one command and `OPTJOURNAL_QUERY_ID` serves a shell that exported it,
and neither survives the case this exists for: someone who installed the
journal, entered their id once, and closed the terminal. The env variable in
particular is a channel a launchd agent does NOT inherit, which is how a
supervised `serve` came to log `no Flex query id configured` on every tick
while the same id worked in a shell.

FAILS OPEN, ALWAYS. A damaged or unreadable settings file must never stop the
journal reading itself: every value here has a working default, the data is in
SQLite and `raw/`, and a preference file is a convenience. `read` therefore
treats any damage as absence -- the same rule `flex._read_state` follows for
the fetch sidecar, and for the same reason.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from optjournal.config import ROOT

__all__ = [
    "FILENAME",
    "HOME_ENV",
    "path_for",
    "read",
    "query_id",
    "scoring",
    "update",
]

#: A dotfile beside the database, like `.fetch-state.json`. Gitignored: it
#: carries one person's choices, and a clone is someone else's journal.
FILENAME = ".optjournal.json"

#: Keys this module will write. A fixed set rather than a free-form dict, so a
#: typo becomes an error at the call site instead of a preference that silently
#: never applies -- the failure mode a settings file invites.
_KEYS = frozenset({"query_id", "scoring"})


#: Overrides the directory holding the settings file. Two callers need it and
#: neither is a preference:
#:
#: * the TEST SUITE, which must never read or write the developer's own
#:   `.optjournal.json`. Without this, any test exercising the query id's
#:   precedence silently picks up whatever is stored on the machine running it --
#:   which is how `test_no_query_id_anywhere_stays_none_rather_than_empty` came to
#:   pass or fail depending on whether the developer had run `optjournal setup`;
#: * a PACKAGED build, where the code directory is inside a signed application
#:   bundle and is neither writable nor preserved across an update.
HOME_ENV = "OPTJOURNAL_HOME"


def path_for(root: Path | None = None) -> Path:
    if root is not None:
        return root / FILENAME
    override = os.environ.get(HOME_ENV)
    return (Path(override) if override else ROOT) / FILENAME


def read(root: Path | None = None) -> dict[str, Any]:
    """Every stored preference, or an empty mapping if there are none.

    Absent, empty, malformed and not-an-object all answer the same way, because
    the caller's next move is identical in every case: use the default.
    """
    try:
        loaded = json.loads(path_for(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def update(root: Path | None = None, **values: Any) -> dict[str, Any]:
    """Merge `values` into the stored settings and return the result.

    MERGES rather than replaces, so writing one preference cannot drop another
    -- a settings page that saves the query id must not silently reset the
    scoreboard unit it was not asked about.

    A `None` value DELETES its key, which is how "back to the default" is
    spelled. Storing null instead would make every reader distinguish "chosen
    as empty" from "never chosen", and no caller wants that distinction.
    """
    unknown = sorted(set(values) - _KEYS)
    if unknown:
        raise ValueError(
            f"not settings this journal stores: {unknown}. Known keys: "
            f"{sorted(_KEYS)}"
        )
    merged = read(root)
    for key, value in values.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value

    target = path_for(root)
    # Written via a temporary file in the same directory and renamed, so a
    # crash mid-write cannot leave a truncated file where a valid one was.
    # `read` would fail open on the wreckage, which means the failure would be
    # SILENT: the journal would come back with every preference reset.
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, target)
    return merged


def query_id(
    explicit: str | None = None, *, root: Path | None = None
) -> str | None:
    """The Flex query id, by precedence: argument, environment, stored setting.

    That order is deliberate and each step earns its place. An ARGUMENT is what
    the person typed just now, so it wins. The ENVIRONMENT is what a cron or a
    shell session set for a reason, so it beats a file written weeks ago.
    The STORED setting is the fallback that makes the journal work at all after
    a terminal is closed -- and, being a file, it is the only one of the three a
    launchd agent can see.

    An empty string at any level is absence, not a choice: `--query-id ''` and
    an exported-but-empty variable both mean "not set", and treating either as a
    real id would send IBKR a request for a query that cannot exist.
    """
    for candidate in (
        explicit,
        os.environ.get("OPTJOURNAL_QUERY_ID"),
        read(root).get("query_id"),
    ):
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    return None


def scoring(explicit: str | None = None, *, root: Path | None = None) -> str | None:
    """The stored scoreboard unit, or `explicit` when one was passed.

    No environment step, unlike `query_id`: a scoring unit is a reading
    preference set from the page, and nothing about a cron or a shell has an
    opinion on it. Validation belongs to `stats.scoring_or_default`, which owns
    the vocabulary -- this only answers what was stored.
    """
    if explicit and explicit.strip():
        return explicit.strip()
    stored = read(root).get("scoring")
    return str(stored).strip() if stored else None
