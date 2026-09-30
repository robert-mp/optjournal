"""Where the application's log goes, and why it rotates itself.

MACOS ROTATES NOTHING for a launchd agent's `StandardOutPath`. `newsyslog` only
touches files listed in `/etc/newsyslog.conf` (plus `/etc/newsyslog.d/`), and
nothing there matches a user's home directory -- so a supervised long-running
process appends to one file forever. That is fine for a startup banner and fatal
for a scheduler logging every tick.

TWO FILES, TWO QUESTIONS, and they are deliberately not merged:

* `serve.out.log` / `serve.err.log` -- launchd's own capture of stdout/stderr.
  Answers "did the process start, and did it die saying anything?". Small, and
  written by `print` before any logging is configured.
* `optjournal.log` -- this module's rotating handler. Answers "what did the jobs
  do?". Every `log.info` from the reconciler and every recorded failure.

A LEAF: imports nothing from the package, so any layer may configure it. That is
also what keeps `tests/test_layering.py` happy about a module the CLI calls first.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

__all__ = ["LOG_DIR", "MAX_BYTES", "BACKUPS", "configure", "log_path"]

#: Beside the journal rather than in `~/Library/Logs`, for the same reason the
#: data lives beside the code: one directory is the whole application. It is also
#: where the launchd plist points its stdout capture, so a reader finds both
#: halves in one place.
LOG_DIR = "logs"

#: 2 MB per file. A tick logs nothing when nothing is due, so the realistic rate
#: is a handful of lines a day plus a burst per run; this is weeks of history.
MAX_BYTES = 2 * 1024 * 1024

#: Five rotations, so ~10 MB total worst case. Enough to cover the window in which
#: someone notices a job stopped working -- which, measured on the arrangement this
#: replaces, was two days.
BACKUPS = 5


def log_path(root: Path) -> Path:
    return root / LOG_DIR / "optjournal.log"


class _RotatingHandler(logging.handlers.RotatingFileHandler):
    """A `RotatingFileHandler` that keeps writing when the rename is refused.

    WINDOWS WILL NOT RENAME A FILE ANOTHER PROCESS HAS OPEN, and the stdlib
    handler does not survive that. Its `doRollover` closes the stream, shifts the
    backups, then fails on the last rename: the record is dropped, and so is every
    later record past `MAX_BYTES`, each attempt shifting the backups once more.

    So the live file is moved aside FIRST, before any backup is touched. If that
    is refused nothing has moved, the same file is reopened, the record is
    written, and the next record past the cap tries again. The file runs over its
    cap while it is held, which is the right trade for a log that exists to be
    read after something went wrong.
    """

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        live = self.baseFilename
        aside = f"{live}.rotating"
        try:
            os.replace(live, aside)
        except OSError:
            self.stream = self._open()
            return
        for n in range(self.backupCount - 1, 0, -1):
            if os.path.exists(f"{live}.{n}"):
                os.replace(f"{live}.{n}", f"{live}.{n + 1}")
        os.replace(aside, f"{live}.1")
        self.stream = self._open()


def configure(root: Path, *, level: int = logging.INFO) -> Path | None:
    """Add a rotating file handler beside the journal. Returns the path, or None.

    ADDITIVE, not a `basicConfig` replacement: the CLI already configures a stderr
    handler, and stderr is what launchd captures into `serve.err.log`. Replacing it
    would silence the console for anyone running `serve` in a terminal.

    Returns None and logs a warning rather than raising if the directory cannot be
    made. A journal that cannot write its log must still serve -- observability
    breaking the observed is the failure this whole plan is about, and it would be
    absurd to reintroduce it here.

    Idempotent: called twice, it does not stack handlers. `serve` is callable more
    than once in a process (the test suite does it routinely), and duplicated
    handlers mean every line written N times.
    """
    target = log_path(root)
    existing = [
        h for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
        and Path(getattr(h, "baseFilename", "")) == target
    ]
    if existing:
        return target
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = _RotatingHandler(
            target, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8",
        )
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "could not open %s, continuing without a log file: %s", target, exc)
        return None
    # A timestamp and a level, unlike the CLI's bare "%(message)s": this file is
    # read days later, when "when did it stop" is the actual question.
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    handler.setLevel(level)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    # The root logger gates before handlers see anything, so a WARNING-level root
    # would drop the reconciler's INFO lines however this handler is set.
    if root_logger.level > level:
        root_logger.setLevel(level)
    return target
