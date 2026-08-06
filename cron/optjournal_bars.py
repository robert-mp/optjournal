"""Price-bar collection crons for the options journal.

Two jobs, one file, because they share a runner and differ only in what they
ask for -- and the difference between them is the whole point:

``live``   Polls during the US session for the bars that CANNOT be collected
           later: an option's intraday series. Measured on this book, the
           source serves hourly option bars only while the session is running.
           Pre-market on 2026-08-06 every contract returned zero hourly bars
           while its underlying still returned five days of them, so the
           retention is asymmetric rather than merely short. Miss a session and
           it is gone -- there is no backfill for it at any price.

``daily``  Runs the full manifest once, pre-market, for everything that IS
           re-fetchable: daily option closes and the underlying series. Without
           it the hourly option bars this file collects would have nothing to
           pair against -- the expected-move band solves implied vol from an
           option's daily close against its underlying's daily close for the
           same session, so a stale daily series silently empties the band.

A script cron rather than an LLM cron: there is no judgement here, only a
subprocess and an exit code.

Delivery policy, which matters more than usual because ``live`` fires seven
times a session -- roughly 1,800 times a year:

* Bars written        -> return quietly. A notification per poll would be
                         thirty-five a week and would train you to ignore the
                         channel. `cron_list` already shows `last=ok`.
* Nothing to fetch    -> return quietly. This is the normal state outside the
                         session, on a holiday, and whenever the book holds no
                         short-dated open option. Not a fault.
* A fetch failed      -> Skip. Quiet, retained, retried on the next tick. Safe
                         *because the intraday series is cumulative within a
                         session*: a 13:00 poll returns every completed bar
                         since the open, so one lost poll costs nothing as long
                         as a later one lands. Reporting a transient network
                         blip seven times a day would be noise.
* CLI missing         -> raise. The job is registered but its code is gone;
                         that needs a human, not a retry.
* Anything else       -> raise. MeshClaw's failure dedup suppresses repeats.

The consequence worth stating plainly: a session in which EVERY poll fails is
lost silently. That is the accepted cost of not alerting on transient failure,
and the honest place to catch it is a once-daily audit asking whether
yesterday's session produced any hourly option bars at all -- one signal about
the thing that actually matters, rather than seven a day about things that do
not.

Register with:

    cron_add(
        name="optjournal-bars-live",
        script="~/.meshclaw/crons/optjournal_bars.py:live",
        cron_expr="5 10-16 * * 1-5",
        timezone="America/New_York",
        timeout=300,
    )
    cron_add(
        name="optjournal-bars-daily",
        script="~/.meshclaw/crons/optjournal_bars.py:daily",
        cron_expr="30 12 * * 2-6",
        timezone="Europe/Dublin",
        timeout=600,
    )

`live` is expressed in America/New_York because market hours are an Eastern
concept and the schedule then follows US DST without being edited twice a year.
It runs at :05 past each hour so the bar that just closed is already on the
grid; the 16:05 run is what reaches for the final hour of the session.

`daily` runs at 12:30 Dublin, thirty minutes behind the statement sync at
12:00, deliberately: a position opened yesterday is only in the database once
that sync has ingested it, and the manifest is derived from positions. The gap
clears the sync's own 720s worst case with margin.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from mesh_claw.cron_script import Skip

PROJECT = Path.home() / ".meshclaw" / "workspace" / "optjournal"
CLI = PROJECT / ".venv" / "bin" / "optjournal"

#: Mirrors optjournal.cli. Kept explicit so a CLI change that renumbers exit
#: codes shows up as a wrong branch here rather than as silent misreporting.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NO_DATA = 3

#: Generous for four keyless HTTP requests, but the CLI's own per-request
#: timeout is 25s and a full daily run asks for around fifteen windows, so the
#: worst realistic case is minutes rather than seconds.
FETCH_TIMEOUT_S = 240


def _run(*flags: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CLI), "bars", "--json", *flags],
        capture_output=True,
        text=True,
        timeout=FETCH_TIMEOUT_S,
        cwd=str(PROJECT),
    )


def _collect(*flags: str) -> None:
    """Run `optjournal bars`, staying silent unless a human is needed."""
    if not CLI.exists():
        raise RuntimeError(f"optjournal CLI not found at {CLI}")

    try:
        proc = _run(*flags)
    except subprocess.TimeoutExpired:
        # Nothing was spent that matters: the endpoint is keyless and there is
        # no request budget to protect, so a slow run is a retry, not an alert.
        raise Skip() from None

    if proc.returncode == EXIT_NO_DATA:
        return  # nothing to fetch; the normal state outside the session
    if proc.returncode == EXIT_ERROR:
        # Per-window fetch failures. The intraday series is cumulative within a
        # session, so the next poll re-collects whatever this one missed.
        raise Skip()
    if proc.returncode != EXIT_OK:
        raise RuntimeError(
            f"optjournal bars exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:500]}"
        )
    # Success is silent. Parsed anyway, so a malformed payload -- which would
    # mean the CLI contract changed under us -- surfaces instead of passing.
    try:
        json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"bars produced unparseable JSON: {exc}") from exc


def live(ctx) -> None:
    """Collect the intraday option bars that exist only during the session."""
    _collect("--live")


def daily(ctx) -> None:
    """Collect everything re-fetchable: daily closes and underlying series."""
    _collect()
