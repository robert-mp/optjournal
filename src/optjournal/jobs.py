"""The run ledger: what a scheduled job did, recorded where the page can see it.

SCHEDULER_PLAN.md step 4. This is the WRITE side of `job_state`/`job_runs`;
`serialize.jobs_data` reads them. Deliberately the whole module for now -- step 5
adds the registry and the runner here, and starting the file with only the ledger
keeps that commit about scheduling rather than about plumbing.

WHY THE CLI WRITES THIS AND NOT THE CRON. The plan said "the existing MeshClaw
crons get a ~10-line helper to write a job_runs row". They cannot: a cron runs under
MeshClaw's own interpreter, which has no py_ibkr -- verified, `import py_ibkr` there
is a ModuleNotFoundError -- which is the entire reason the crons shell out to the
CLI instead of importing anything. So the helper lives here, called by the CLI
commands the crons already invoke, and the cron files stay untouched. That also
means the ledger records what the WORK did rather than what the cron's delivery
policy decided, which is the more useful of the two: the delivery decision is
already visible in Slack, and the thing that was missing was any record that the
work happened at all.

THE FAILURE THIS EXISTS FOR, measured on this machine rather than imagined:
~/.meshclaw/crons.json read `last_status: "ok"` for optjournal-bars-live,
bars-daily AND bars-audit, while `price_bars.fetched_at` shows 28 hourly rows
written on 08-06, two on 08-07, and nothing after until a human ran the command by
hand. Three jobs green for two days while collecting nothing -- including the audit
job whose only purpose is to notice exactly that. A green `last_status` is not
evidence of anything, so the ledger records the WORK: what ran, when, and what it
found.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

__all__ = ["KNOWN_JOBS", "RUN_HISTORY", "prune_runs", "record_run"]

log = logging.getLogger(__name__)

#: Runs kept per job. Enough to see a week of a job that fires seven times a
#: session (bars_live) without the table growing forever. The newest row for a
#: given `fired_for` is never pruned -- see `prune_runs`.
RUN_HISTORY = 200

#: The job names the ledger accepts, matching the MeshClaw registrations they
#: stand in for today, plus `market` -- which has NEVER been registered (verified:
#: `grep -c optjournal-market ~/.meshclaw/crons.json` is 0), so 143 lines of
#: tested calendar policy have never run on a schedule. Naming it here is the first
#: step of "exists" and "registered" becoming one fact in step 5.
#:
#: A CHECKED SET rather than free text, because a typo'd job name is invisible: it
#: would write rows nothing reads and leave the real job looking as if it never ran
#: -- the exact failure this ledger exists to make visible.
KNOWN_JOBS = frozenset({"sync", "bars_live", "bars_daily", "market"})

#: Statuses a run may end in. `running` is written BEFORE the work starts (step 5),
#: so a killed process leaves evidence rather than an unclaimed slot.
_STATUSES = frozenset({
    "running", "ok", "nothing", "missed", "failed", "interrupted",
})


def record_run(
    conn: sqlite3.Connection,
    job: str,
    *,
    status: str,
    detail: str | None = None,
    done: int = 0,
    total: int = 0,
    fired_for: int | None = None,
    started_at: str | None = None,
) -> int:
    """Record one completed run, and update the job's anchor. Returns the row id.

    `fired_for` is the scheduled instant claimed, or None for a run that claims no
    slot -- which is every run today, because nothing schedules anything yet: the
    CLI is invoked by a MeshClaw cron or by a human, and neither knows which
    scheduled minute it is standing in for. None is therefore the honest value, and
    the partial unique index accepts repeats of it.

    Failures here are LOGGED AND SWALLOWED, and that is the one judgement in this
    function worth arguing with. A ledger write is bookkeeping about work that has
    already happened; letting it turn a successful `optjournal bars` into a
    non-zero exit would mean the observability broke the thing it observes, and the
    cron's delivery policy would then report a failure that did not occur. The cost
    is that a broken ledger is quiet -- which is exactly the shape of bug this
    project keeps finding, so it is bounded deliberately: the write is one INSERT
    plus one UPSERT against a table with no foreign keys, and the heartbeat in
    `job_state` is what a reader checks to see whether anything is recording at all.
    """
    if job not in KNOWN_JOBS:
        raise ValueError(f"unknown job {job!r}; expected one of {sorted(KNOWN_JOBS)}")
    if status not in _STATUSES:
        raise ValueError(f"unknown status {status!r}; expected one of {sorted(_STATUSES)}")

    now = datetime.now(UTC)
    stamp = now.isoformat(timespec="seconds")
    try:
        cursor = conn.execute(
            "INSERT INTO job_runs (job, fired_for, started_at, finished_at,"
            " status, detail, done, total) VALUES (?,?,?,?,?,?,?,?)",
            (job, fired_for, started_at or stamp, stamp, status, detail, done, total),
        )
        run_id = int(cursor.lastrowid or 0)
        conn.execute(
            "INSERT INTO job_state (job, last_fired_for, last_status,"
            " consecutive_failures, heartbeat_at) VALUES (?,?,?,?,NULL)"
            " ON CONFLICT(job) DO UPDATE SET"
            "   last_fired_for = COALESCE(excluded.last_fired_for, last_fired_for),"
            "   last_status = excluded.last_status,"
            # Reset on anything that is not a failure, so the count means
            # "consecutive", not "ever". `missed` is not a failure of the job.
            "   consecutive_failures = CASE WHEN excluded.last_status = 'failed'"
            "     THEN consecutive_failures + 1 ELSE 0 END",
            (job, fired_for, status, 1 if status == "failed" else 0),
        )
        prune_runs(conn, job)
        conn.commit()
        return run_id
    except sqlite3.Error as exc:
        # See the docstring: bookkeeping must not fail the work it describes.
        # Rolled back explicitly -- sqlite3 does NOT roll back on error, and a
        # connection left in a transaction holds the write lock for the whole
        # BUSY_TIMEOUT_MS (measured: the next writer waits 15.5s and then fails).
        log.warning("could not record %s run: %s", job, exc)
        if conn.in_transaction:
            conn.rollback()
        return 0


def prune_runs(conn: sqlite3.Connection, job: str, *, keep: int = RUN_HISTORY) -> int:
    """Trim `job`'s history to the newest `keep` rows. Returns rows deleted.

    NEVER deletes the newest row carrying a `fired_for`, because that row is what a
    catch-up reconciler reads to decide whether a scheduled instant has been
    claimed. Losing it would make the job either replay a year of instants or drop
    the schedule silently.

    `job_state` is separate precisely so this pass cannot reach the anchor -- but
    belt and braces: the newest scheduled row survives here too, so the ledger
    remains self-sufficient if the anchor is ever rebuilt from it.
    """
    keep_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM job_runs WHERE job = ? ORDER BY id DESC LIMIT ?",
            (job, keep))
    ]
    newest_scheduled = conn.execute(
        "SELECT id FROM job_runs WHERE job = ? AND fired_for IS NOT NULL"
        " ORDER BY id DESC LIMIT 1", (job,)).fetchone()
    if newest_scheduled is not None:
        keep_ids.append(newest_scheduled["id"])
    if not keep_ids:
        return 0
    placeholders = ",".join("?" * len(keep_ids))
    cursor = conn.execute(
        f"DELETE FROM job_runs WHERE job = ? AND id NOT IN ({placeholders})",
        (job, *keep_ids))
    return cursor.rowcount or 0
