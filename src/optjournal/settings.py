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

That is not in tension with the dev flag failing CLOSED. Failing open means
"fall back to the default", and the default for `dev` is off -- so a damaged
file reads as no preferences, `dev` finds no key, and the answer is False. One
rule, two safe outcomes: a missing query id is absence, and missing dev mode is
the shipped app.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from optjournal.config import ROOT

__all__ = [
    "DEV_ENV",
    "FILENAME",
    "HOME_ENV",
    "confirm_query_id",
    "dev",
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
#:
#: `dev` is writable HERE (a hand-edited file, or `update(dev=True)`) but is
#: deliberately absent from `web._settings_write`, which names its two keys by
#: hand and writes nothing else. So the unauthenticated HTTP surface cannot turn
#: dev mode on: it is set out-of-band, the way IAG's `is_admin` is a server
#: decision the client can only read -- see `dev` below.
_KEYS = frozenset({"query_id", "confirm_query_id", "scoring", "dev"})

#: The environment channel for the dev flag. `OPTJOURNAL_DEV=1 optjournal serve`
#: turns developer-only surfaces on for one session without touching the file.
DEV_ENV = "OPTJOURNAL_DEV"


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


def confirm_query_id(
    explicit: str | None = None, *, root: Path | None = None
) -> str | None:
    """The Trade Confirmation query id, by the same precedence as `query_id`.

    A SECOND id rather than a mode on the first, because they are two different
    saved queries in Client Portal returning two different schemas: the Activity
    Statement is T+1 and settled, a confirm is same-session and provisional. One
    id doing both would mean the journal could not hold them at once, which is the
    whole arrangement -- the daily sync keeps the statement, the intraday poll
    keeps the confirms, and `ingest.SOURCE_RANK` decides which wins per fill.

    Absent is a SUPPORTED state, and the common one: a journal with no confirm
    query configured simply has no intraday feed, and the job that polls it stays
    idle rather than failing. Only the Activity Statement is required to work.
    """
    for candidate in (
        explicit,
        os.environ.get("OPTJOURNAL_CONFIRM_QUERY_ID"),
        read(root).get("confirm_query_id"),
    ):
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    return None


def _truthy(value: Any) -> bool:
    """Only an explicit, recognised yes is True; everything else is False.

    This is the whole safety property of the dev flag. Absent, empty, a stray
    "0", a typo, a file a crash truncated -- every one of them reads as NOT dev,
    so a friend who never set it and a config that got damaged both get the
    shipped app rather than a half-built surface. It is the same spirit as IAG's
    entitlement defaulting to free on any error, one level simpler because there
    is no server here to be wrong about.
    """
    if value is True:
        return True
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def dev(*, root: Path | None = None) -> bool:
    """Whether developer-only surfaces are shown. OFF unless explicitly turned on.

    Precedence is `query_id`'s minus the argument step: the ENVIRONMENT
    (`OPTJOURNAL_DEV=1`, a per-session choice) beats the STORED setting (a
    persisted one). No argument step, because nothing takes dev mode as a command
    argument -- it is a property of who is running the journal, not of one
    invocation. An empty or whitespace env value is absence, not "off", matching
    `query_id`: an exported-but-blank variable falls through to the file.

    NOT reachable through the web API, and that is the point rather than an
    omission. `web._settings_write` writes only `query_id` and `scoring`, so a
    page open in another tab cannot flip dev mode over the unauthenticated HTTP
    surface. The privileged flag is set out-of-band -- a shell export or the
    hand-editable file -- never by the untrusted caller, which is the line IAG
    draws by computing `is_admin` server-side and letting the client only read
    it. Fails closed: the default, and the answer to any damage, is False.
    """
    env = os.environ.get(DEV_ENV)
    if env is not None and env.strip():
        return _truthy(env)
    return _truthy(read(root).get("dev"))


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
