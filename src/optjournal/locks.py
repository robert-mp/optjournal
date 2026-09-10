"""Cross-process locks. One primitive, because `threading.Lock` is not enough.

Every lock in this project was a `threading.Lock` until three bugs showed why that
is the wrong tool. A threading lock serialises two BROWSER TABS, because they are
two requests in one server process. It does nothing about the cron and the server,
which are two PROCESSES -- and that pairing is the normal case here, not an exotic
one: a scheduled sync at noon while a page sits open is an ordinary Tuesday.

The three bugs this exists for, each reproduced before being fixed:

1. **The IBKR request budget.** `flex.fetch` checks a cooldown, then downloads,
   then writes the stamp -- and only on success, so a failed request does not start
   a cooldown. Correct in one process, a check-then-act race across two: both read
   the old stamp, both clear the guard, both spend a request against a lockout
   allowance. The window is not narrow. `read_token` alone measured 8.2 SECONDS on
   this machine (keyring), before an IBKR round trip that retries while the
   statement generates. Three call sites can enter it: the Sync button, `optjournal
   fetch`, and `optjournal sync`.

2. **Concurrent migration.** `db.migrate` drops every view and recreates them,
   and `db.open_journal` migrates on EVERY request. Two overlapping requests and
   one drops the views the other is querying. Measured: 23 failures in 90 attempts
   with six workers, and 2 in 6 trials from nothing more than two simultaneous page
   loads -- surfacing as `HTTP 500: no such table: current_option_positions`.

3. **Whatever comes next.** A scheduler inside the app means more processes
   touching one SQLite file and one archive directory. A single audited primitive
   is what keeps that from becoming a fourth bug.

WHY AN OS FILE LOCK RATHER THAN A LOCK TABLE OR A PID FILE. Both supported
backends -- POSIX `flock` and Windows `msvcrt.locking` -- are released by the
kernel when the holder dies, so a crashed process cannot leave the journal
wedged. A lock row in SQLite or a pid file both can, and both then need a
staleness heuristic that is itself a source of bugs. The locks are advisory,
which is fine: every writer here is this project's own code.
"""

from __future__ import annotations

import contextlib
import errno
import importlib
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

# Dynamic because mypy validates this module on both platforms. A normal
# conditional import still makes Windows mypy inspect POSIX-only `fcntl` (and
# POSIX mypy inspect Windows-only `msvcrt`) against the wrong platform stubs.
_locker: Any = importlib.import_module("msvcrt" if os.name == "nt" else "fcntl")

__all__ = ["LockTimeout", "locked"]

log = logging.getLogger(__name__)

#: Long enough for the slowest thing a lock covers, short enough that a wedged
#: process surfaces as an error rather than a hang. The slowest holder is a fetch:
#: keyring (8.2s measured) plus an IBKR download that retries while the statement
#: generates. A migration is the other candidate; its table rebuilds run in
#: milliseconds on this journal (5,000 bar upserts committed in 6ms).
DEFAULT_TIMEOUT_S = 120

#: Poll interval while another process holds the lock.
_POLL_S = 0.05


class LockTimeout(RuntimeError):
    """Another process held the lock for longer than the timeout allowed.

    Raised rather than proceeding, because every caller of this module is
    protecting something that must not happen twice -- a spent IBKR request, a
    schema migration. Continuing anyway would defeat the point of asking.
    """


def _try_lock(handle: BinaryIO) -> bool:
    """Acquire the first byte without waiting; False means another holder.

    `msvcrt.locking` locks bytes from the current file position, while `flock`
    locks the file as a whole. The sidecar is created with one byte below so the
    Windows range always exists. Keeping that difference inside this module is
    what lets every caller retain the same crash-release and timeout contract.
    """
    if os.name == "nt":
        handle.seek(0)
        try:
            _locker.locking(handle.fileno(), _locker.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        return True

    try:
        _locker.flock(handle.fileno(), _locker.LOCK_EX | _locker.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(handle: BinaryIO) -> None:
    if os.name == "nt":
        handle.seek(0)
        _locker.locking(handle.fileno(), _locker.LK_UNLCK, 1)
    else:
        _locker.flock(handle.fileno(), _locker.LOCK_UN)


@contextlib.contextmanager
def locked(path: Path, *, timeout_s: int = DEFAULT_TIMEOUT_S) -> Iterator[None]:
    """Hold an exclusive cross-process lock on `path` for the block.

    `path` is a lock FILE, not the resource: locking the database or the state
    file itself would mean opening it for write just to coordinate, and an empty
    sidecar cannot be corrupted by the locking.

    Blocking with a timeout rather than `LOCK_NB`, because the callers genuinely
    want to WAIT. A sync that returns "busy, try later" because a bars fill held
    the lock for two seconds is a worse answer than one that takes two seconds.
    Implemented with signal-free polling: neither backend provides the timeout
    contract this application needs, and an alarm would not be safe on a
    non-main thread -- which the web server's handlers are.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Append mode never truncates, so two processes racing to create the file
    # cannot blank each other's. Windows locks a byte range rather than an inode,
    # so ensure byte zero exists; its value is otherwise irrelevant.
    with open(path, "a+b") as handle:  # noqa: SIM115 - closed by the with
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        waited = 0.0
        while True:
            if _try_lock(handle):
                break
            if waited >= timeout_s:
                raise LockTimeout(
                    f"another process held {path.name} for more than "
                    f"{timeout_s}s. Stop the other optjournal process or wait "
                    f"for its current operation to finish."
                )
            if waited == 0.0:
                log.debug("waiting for %s", path.name)
            # 50ms: fast enough that contention is invisible to a person,
            # slow enough not to spin a core while a fetch runs.
            time.sleep(_POLL_S)
            waited += _POLL_S
        try:
            yield
        finally:
            _unlock(handle)
