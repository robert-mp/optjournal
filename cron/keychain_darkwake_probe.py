#!/usr/bin/env python3
"""Does the keychain still refuse a token read across a sleep/wake cycle?

THE CLAIM UNDER TEST. Between 11 and 17 September this journal's scheduled sync
failed five times with `KeyringError: Can't get password from keychain: (-25320,
'Unknown Error')` -- `errSecInvalidOwnerEdit`'s neighbour `errSecInDarkWake`, which
macOS returns when a keychain operation would need to show UI and the system is in a
dark wake. Five of those hit `jobs.FAILURE_BACKOFF` and the reconciler stopped
starting `sync` at all, which is how two weeks of silence happened.

The `ibkr-flex-token` entry has since been recreated BY the interpreter that reads
it, so the read should need no prompt and therefore have no UI to fail to show. That
is reasoning, not evidence. This script is the evidence.

HOW TO RUN IT

    uv run python cron/keychain_darkwake_probe.py        # then sleep the Mac

Leave it running, close the lid or `pmset sleepnow`, wait a minute, wake the
machine, then stop it with Ctrl-C. It prints a verdict and writes every result to
`logs/darkwake-probe.log`.

It reads the token and immediately discards it -- the value is never printed, never
logged and never returned. A failure is logged with its numeric status, because that
number is the whole point: -25320 means the hazard is still live, and anything else
means the read failed for an unrelated reason worth seeing on its own.
"""

from __future__ import annotations

import contextlib
import signal
import time
from datetime import UTC, datetime
from pathlib import Path

from optjournal.flex import KEYRING_SERVICE, read_token

#: Every few seconds. Frequent enough to land inside a dark wake, which can be
#: seconds long, and idle enough to leave no mark on battery over an afternoon.
INTERVAL_S = 3.0

#: The status the journal actually saw. Named rather than inlined so the verdict can
#: say which hazard it found.
DARK_WAKE = -25320

LOG = Path("logs") / "darkwake-probe.log"


def _status_of(exc: BaseException) -> int | None:
    """The macOS status code, off the exception CHAIN rather than the message.

    `keyring.backends.macOS` raises `KeyringError(...) from api.Error(status, ...)`,
    so the number is a real attribute one link down. Parsing the rendered string
    would break on a wording change -- the same reasoning as `flex._write_refusal`.
    """
    cause = exc.__cause__
    if cause is not None and cause.args and isinstance(cause.args[0], int):
        return int(cause.args[0])
    return None


def main() -> int:
    # SIGTERM PRINTS A VERDICT TOO, not just Ctrl-C. A probe whose whole output is a
    # verdict must produce one however it is stopped: `pkill`, a `launchd` stop or a
    # terminal closing all send SIGTERM, and the default disposition would kill the
    # process with the summary unwritten. Raising KeyboardInterrupt reuses the one
    # exit path rather than adding a second.
    def _bye(_sig: int, _frame: object) -> None:
        raise KeyboardInterrupt

    with contextlib.suppress(ValueError):   # not the main thread: nothing to install
        signal.signal(signal.SIGTERM, _bye)

    LOG.parent.mkdir(parents=True, exist_ok=True)
    reads = failures = dark_wakes = 0
    gaps: list[float] = []
    last = time.monotonic()

    print(f"probing {KEYRING_SERVICE} every {INTERVAL_S}s -> {LOG}")
    print("sleep the Mac now, wake it, then stop this with Ctrl-C")
    with LOG.open("a", encoding="utf-8") as log:
        log.write(f"--- probe started {datetime.now(UTC).isoformat()}\n")
        try:
            while True:
                # A WALL-CLOCK GAP IS THE EVIDENCE THE MACHINE SLEPT. `monotonic`
                # excludes sleep on this platform (measured elsewhere in this
                # project at 44.6 hours), so a tick that took far longer than the
                # interval is the wake itself -- which is exactly the tick whose
                # result matters. Without this the probe cannot tell "no failures"
                # from "never actually slept".
                now = time.monotonic()
                gap = now - last
                last = now
                if gap > INTERVAL_S * 3:
                    gaps.append(gap)
                    log.write(f"{datetime.now(UTC).isoformat()} WOKE after {gap:.0f}s\n")

                try:
                    read_token()
                except Exception as exc:  # noqa: BLE001 - every failure is data
                    failures += 1
                    status = _status_of(exc)
                    if status == DARK_WAKE:
                        dark_wakes += 1
                    log.write(f"{datetime.now(UTC).isoformat()} FAIL status={status} "
                              f"{type(exc).__name__}\n")
                else:
                    reads += 1
                log.flush()
                time.sleep(INTERVAL_S)
        except KeyboardInterrupt:
            pass

    print(f"\n{reads} successful read(s), {failures} failure(s), "
          f"{dark_wakes} of them -25320")
    if not gaps:
        print("INCONCLUSIVE: no sleep detected. The machine never slept while this "
              "ran, so it has not tested anything -- run it again and sleep the Mac.")
        return 2
    print(f"detected {len(gaps)} wake(s), longest sleep {max(gaps):.0f}s")
    if dark_wakes:
        print(f"HAZARD STILL LIVE: {dark_wakes} read(s) refused with -25320. The "
              "keychain still refuses across a dark wake, so the scheduled sync can "
              "still accumulate failures and back itself off. See "
              "docs/trade-confirmations.md's sibling note in MEMORY.")
        return 1
    print("CLEAR: every read across the wake succeeded, so the -25320 refusals do "
          "not recur now that the entry is owned by the reader.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
