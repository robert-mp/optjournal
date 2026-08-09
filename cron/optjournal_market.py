"""Economic-calendar refresh for the options journal.

One job. The feed serves the CURRENT WEEK and nothing else -- verified against the
live source: `ff_calendar_nextweek`, `_thismonth` and `_lastweek` all 404 -- so
there is exactly one thing to do, once a day, and no window to tune.

WHY DAILY RATHER THAN WEEKLY. The week's events do not change, but their
`forecast` and `previous` figures are revised between announcement and release,
and `store_events` upserts so a re-fetch corrects them in place. A weekly run
would show a stale forecast beside a released number, which reads as a miss that
never happened.

WHY THIS TABLE IS THE HISTORY. Because rows persist and the feed will not serve a
past week, a daily run is also what accumulates a calendar the source cannot give
back. Skip a fortnight and those events are simply not recoverable -- which is the
argument for the job being boring and reliable rather than clever.

A script cron rather than an LLM cron: there is no judgement here, only a
subprocess and an exit code.

Delivery policy:

* Events stored     -> return quietly. Every run stores ~99 rows and almost all
                       of them are corrections to figures nobody was waiting for.
                       `cron_list` already shows `last=ok`.
* Rate limited      -> Skip. The feed sits behind Cloudflare and answers 429 with
                       a `retry-after`; observed at 92s while this was being
                       written, and still refusing three minutes later. Quiet,
                       retained, retried tomorrow -- nothing is lost, because the
                       same week is still there. Reporting a back-off would train
                       you to ignore the channel.
* CLI missing       -> raise. The job is registered but its code is gone; that
                       needs a human, not a retry.
* Anything else      -> raise. A parse failure means the feed CHANGED SHAPE, and
                       `events.parse_events` refuses rather than guessing -- an
                       unknown `impact` would otherwise file a high-impact release
                       as Low. That is the one failure here worth waking someone
                       for, because the calendar would look right.

Register with:

    cron_add(
        name="optjournal-market",
        script="~/.meshclaw/crons/optjournal_market.py:refresh",
        cron_expr="0 11 * * *",
        timezone="Europe/Dublin",
        timeout=180,
    )

11:00 Dublin, an hour before the statement sync at 12:00 and unrelated to it: this
touches no IBKR request and shares no lock, so it has no reason to queue behind
anything. Every day including weekends, because the feed publishes the coming
week's schedule over a weekend and a Monday-only run would miss two days of
revisions to a Monday release.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from mesh_claw.cron_script import Report, Skip

#: Moved out of ~/.meshclaw/workspace by SCHEDULER_PLAN.md step 2: the journal
#: should not live inside the directory of the tool being retired.
PROJECT = Path.home() / "optjournal"
CLI = PROJECT / ".venv" / "bin" / "optjournal"

#: Mirrors optjournal.cli. Explicit so a CLI change that renumbers exit codes
#: shows up as a wrong branch here rather than as silent misreporting.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_THROTTLED = 4

#: Generous against a 20s HTTP timeout plus SQLite writes. The feed answers in
#: well under a second when it answers at all.
TIMEOUT_S = 120


def _run() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CLI), "market", "--fetch", "--json"],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        cwd=str(PROJECT),
    )


def refresh(ctx) -> None:
    """Fetch this week's calendar and store it, correcting revised figures."""
    if not CLI.exists():
        raise RuntimeError(f"optjournal CLI not found at {CLI}")

    proc = _run()

    if proc.returncode == EXIT_THROTTLED:
        # The feed's own back-off. Nothing is lost: the same week is served
        # tomorrow, and the retry-after it reported is in the stderr line.
        raise Skip((proc.stderr or "rate limited").strip().splitlines()[-1])

    if proc.returncode != EXIT_OK:
        raise RuntimeError(
            f"optjournal market --fetch exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:400]}"
        )

    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"unreadable --json from the CLI: {exc}") from exc

    stored = payload.get("stored")
    if not stored:
        # A successful run that stored nothing means the feed returned an empty
        # week, which is not a thing it does. Worth a look rather than a silence.
        raise Report("*Market calendar* — the feed returned no events this week.")

    # High-impact events still ahead are the only part worth a human's attention,
    # and only when there are some: a quiet week should say nothing at all.
    upcoming = [
        event for event in (payload.get("events") or ())
        if event.get("impact") == "High"
    ]
    if not upcoming:
        return

    lines = [f"*Market calendar* — {len(upcoming)} high-impact event(s) ahead"]
    for event in upcoming[:8]:
        figures = " ".join(
            f"{label} {event[key]}"
            for label, key in (("fc", "forecast"), ("prev", "previous"))
            if event.get(key)
        )
        lines.append(
            f"• {event.get('day')} {event.get('at')} "
            f"{event.get('country')} {event.get('title')}"
            + (f"  ({figures})" if figures else "")
        )
    if len(upcoming) > 8:
        lines.append(f"…and {len(upcoming) - 8} more")
    lines.append("")
    lines.append("impact is the feed's assessment · `optjournal market` for the week.")
    raise Report("\n".join(lines))
