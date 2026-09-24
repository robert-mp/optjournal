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

import enum
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from optjournal import settings as prefs
from optjournal.bars import backfill_bars
from optjournal.events import EventFetchError, EventRateLimited, fetch_events, store_events
from optjournal.flex import (
    FetchCooldown,
    TokenMissing,
    TokenRejected,
    fetch_confirms,
)
from optjournal.ingest import ASSET_FILTER_ALL, ingest_confirms
from optjournal.locks import LockTimeout, locked
from optjournal.sync import sync_journal

__all__ = [
    "JOBS",
    "KNOWN_JOBS",
    "RUN_HISTORY",
    "Catchup",
    "Context",
    "Due",
    "Job",
    "JobBusy",
    "Outcome",
    "UnknownJob",
    "due_jobs",
    "interrupted_runs",
    "job_by_name",
    "prune_runs",
    "record_manual_sync",
    "record_run",
    "run_job",
    "sync_outcome",
]

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
KNOWN_JOBS = frozenset({"sync", "confirm", "bars_live", "bars_daily", "market"})

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
        _upsert_state(conn, job, status, fired_for)
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


def _upsert_state(
    conn: sqlite3.Connection, job: str, status: str, fired_for: int | None
) -> None:
    """Update the job's scheduling anchor. Does NOT commit; the caller owns that.

    One function rather than the same UPSERT written at each of its three call
    sites (`record_run`, `_finish`, `interrupted_runs`), because the
    `consecutive_failures` rule is the subtle part: it must reset on anything that
    is not a failure, or "consecutive" means "ever" and a transient failure backs
    off forever. Three copies of that CASE is three chances for one to drift.

    `heartbeat_at` is deliberately untouched. It belongs to the TICK LOOP, not to
    a job outcome -- the whole point of the two signals being separate is that a
    job succeeding says nothing about whether anything is driving the schedule.
    Writing it here would make every manual run look like a live scheduler.
    """
    conn.execute(
        "INSERT INTO job_state (job, last_fired_for, last_status,"
        " consecutive_failures, heartbeat_at) VALUES (?,?,?,?,NULL)"
        " ON CONFLICT(job) DO UPDATE SET"
        "   last_fired_for = COALESCE(excluded.last_fired_for, last_fired_for),"
        "   last_status = excluded.last_status,"
        "   consecutive_failures = CASE WHEN excluded.last_status = 'failed'"
        "     THEN consecutive_failures + 1 ELSE 0 END",
        (job, fired_for, status, 1 if status == "failed" else 0),
    )


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


# ---------------------------------------------------------------------------
# The registry: what jobs exist, and when they should run.
#
# THE REGISTRY IS CODE, AND THAT IS THE FIX FOR A SPECIFIC FAILURE. MeshClaw's
# `crons.json` holds four optjournal jobs and NONE of them is the calendar
# refresh -- re-verified: `grep -c optjournal-market ~/.meshclaw/crons.json` is 0.
# So 143 lines of reviewed, tested, README-documented calendar policy have never
# run on a schedule, and `market_events` holds one fetch rather than the daily
# accumulation its docstring's argument depends on. Registration was an
# unversioned, hand-typed side channel that no test could see. Here, "exists" and
# "registered" are one fact, and `tests/test_jobs.py` can read it.
#
# The schedules below are the ones MeshClaw is running TODAY, read from
# crons.json rather than from the shims' docstrings (they disagree: the shim's
# suggested `cron_add` for market says 11:00 Dublin, and no such job exists):
#
#     optjournal-daily-sync    0 12 * * 2-6      Europe/Dublin     900s
#     optjournal-bars-live     5 10-16 * * 1-5   America/New_York  300s
#     optjournal-bars-daily    30 12 * * 2-6     Europe/Dublin     600s
#     optjournal-bars-audit    0 13 * * 2-6      Europe/Dublin     120s
#
# `bars-audit` is deliberately ABSENT from this registry: step 4 moved it from a
# job to a field computed on every page load (`serialize.audit_data`), because a
# watchdog that is itself a cron stops when the thing it watches stops -- and did.
# `market` is present for the first time.
# ---------------------------------------------------------------------------


class Catchup(enum.Enum):
    """What a job should do about an instant it slept through.

    A three-valued enum on the spec rather than a boolean plus a comment, so
    "run everything overdue" is impossible to write by accident and the one
    genuinely dangerous case is in the type. Step 6's reconciler reads this; step
    5 only records it, so that the schedule and its catch-up policy arrive
    together rather than the schedule arriving first and being guessed at later.
    """

    #: Run the most recent missed instant once, if inside `window_s`. Never a
    #: backlog: two noon syncs in a row would spend two IBKR requests to fetch
    #: the same statement twice.
    LATEST = "latest"
    #: Not a wall-clock fire at all -- due while inside a window and the last
    #: success is old enough. `bars_live` only, see its Job below.
    WINDOW = "window"
    #: A missed instant is simply missed.
    NONE = "none"


@dataclass(frozen=True)
class Context:
    """Everything a job's work needs that is not the connection.

    A frozen object rather than a widening argument list, for the reason
    `web.ServeConfig` exists: the alternative is module-level state that two
    servers in one process silently share, which the test suite creates routinely.

    `query_id` may be None -- a journal serving an already-ingested archive with no
    credentials configured is a supported state, and `sync` then reports `failed`
    with a cause rather than raising past the ledger.
    """

    archive_dir: Path
    db_path: Path
    query_id: str | None = None
    assets: tuple[str, ...] = ()


@dataclass(frozen=True)
class Job:
    """One schedulable unit of work.

    Frozen because a registry entry that could be mutated at runtime is a
    schedule nothing can be held to -- which is the `crons.json` failure in a
    different shape.

    `run` takes an open connection plus a `Context` and returns an `Outcome`. It
    calls FUNCTIONS, never the CLI, and that is load-bearing rather than tidy:
    shelling out means ~6 exit-code branches per job that flatten every cause into
    an integer, and the 2026-08-07 sync failure proves the cost. It was a locked
    keychain (`keyring.backends.macOS ... find_generic_password`), which is NOT
    `flex.TokenMissing`, so it never mapped to exit 2 -- it arrived as an uncaught
    traceback, exit 1, and a generic failure that reached nobody. A typed exception
    cannot be flattened that way.
    """

    name: str
    run: Callable[[sqlite3.Connection, Context], Outcome]
    #: Minute and hour of the scheduled instant, in `zone`. Two integers rather
    #: than a cron expression: every schedule here is "once at HH:MM on these
    #: weekdays", and a parser would be code accepting expressions nothing writes.
    minute: int
    hour: int
    #: ISO weekdays (Monday=1). `sync` runs Tue-Sat because a US Friday session
    #: settles into a statement Saturday morning European time.
    weekdays: tuple[int, ...]
    #: The job's own zone. `bars_live` is Eastern because market hours ARE an
    #: Eastern concept, so it follows US DST without being edited twice a year;
    #: the rest are the reader's local zone, where "before breakfast" is the
    #: actual requirement.
    zone: str
    catchup: Catchup
    window_s: int
    timeout_s: int
    #: Whether a run consumes one of IBKR's rate-limited Flex requests. Read by
    #: the reconciler before retrying anything, and the reason `sync` may not be
    #: made due again by a mere failure (see the plan's step 6).
    spends_broker_request: bool = False

    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.zone)


@dataclass(frozen=True)
class Outcome:
    """What one run did, in the ledger's own vocabulary.

    `status` is constrained to `_STATUSES` by `record_run`, so a job cannot
    invent one. The distinction that matters is `ok` versus `nothing`: an empty
    run is not a success and not a failure, and collapsing the two is precisely
    what let three cron jobs report health for two days while collecting nothing.
    """

    status: str
    detail: str = ""
    done: int = 0
    total: int = 0


class UnknownJob(KeyError):
    """No job by that name. Its own type so the endpoint can answer 400."""


class JobBusy(RuntimeError):
    """This job is already running, in this process or another one.

    Raised rather than queued: every job here is idempotent and cheap to retry
    on the next tick, and a queue behind a job that spends IBKR requests is a way
    to spend several at once.
    """

    def __init__(self, job: str, run_id: int | None = None) -> None:
        super().__init__(f"{job} is already running")
        self.job = job
        self.run_id = run_id


# ---------------------------------------------------------------------------
# The work each job does.
#
# Thin on purpose: the policy lives in `flex`, `bars` and `events`, and these
# translate a typed exception into a ledger status. A branch here that did real
# work would be a second implementation of something already tested.
# ---------------------------------------------------------------------------


def _bars(conn: sqlite3.Connection, _ctx: Context, *, live: bool) -> Outcome:
    """Fetch the manifest's windows. Shared by `bars_live` and `bars_daily`.

    One function with a flag rather than two, because the two differ ONLY in what
    they ask for -- which is also how `cron/optjournal_bars.py` is written, and
    the reason it is one file.

    Per-window failures are collected by `backfill_bars` rather than raised (one
    unreachable contract must not abandon the book), so the status is derived from
    the outcome rather than from an exception.
    """
    outcome = backfill_bars(conn, perishable_only=live)
    if outcome.failures and not outcome.written:
        return Outcome("failed", "; ".join(outcome.failures)[:400],
                       outcome.written, outcome.requested)
    got = f"{outcome.written} bar(s), {outcome.skipped} empty"
    if outcome.failures:
        # A PARTIAL RUN IS NOT A FAILED RUN, and the difference was expensive: one
        # index symbol the price source spells differently failed two windows out
        # of twenty-two, the run reported `failed` while writing 8,889 bars, and
        # five of those backed the whole daily job off. The book stopped being
        # collected over a symbol that was never going to work.
        #
        # Still not silently green: the failures are named in the detail the page
        # shows and the backoff warning reads, and a perishable window that did not
        # land is `audit_perishable`'s question rather than this status's.
        return Outcome(
            "ok" if outcome.written else "nothing",
            f"{got}; {len(outcome.failures)} window(s) failed: "
            + "; ".join(outcome.failures)[:300],
            outcome.written, outcome.requested,
        )
    return Outcome(
        "ok" if outcome.written else "nothing", got,
        outcome.written, outcome.requested,
    )


def _market(conn: sqlite3.Connection, _ctx: Context) -> Outcome:
    """Refresh the economic calendar.

    `EventRateLimited` is `nothing`, NOT `failed`: the feed pushed back, nothing
    was lost, and the same week is served later. Counting that as a failure would
    accumulate `consecutive_failures` for a working system and eventually back
    off a job that was never broken.
    """
    try:
        events = fetch_events()
    except EventRateLimited as exc:
        return Outcome("nothing", f"rate limited: {exc}")
    except EventFetchError as exc:
        return Outcome("failed", str(exc)[:400])
    stored = store_events(conn, events)
    return Outcome("ok" if stored else "nothing",
                   f"{len(events)} fetched, {stored} stored", stored, len(events))


def _sync(conn: sqlite3.Connection, ctx: Context) -> Outcome:
    """Fetch the newest statement and fold it in.

    THE COOLDOWN IS NOT REIMPLEMENTED HERE. `flex.fetch` owns it, holds the fetch
    OS file lock, and stamps `.fetch-state.json`; a second copy of that sequence would be
    a second thing to keep in step with the lockout budget.
    """
    if not ctx.query_id:
        # A journal with no credentials configured is a supported state, not a
        # crash: `failed` with the cause is what a reader can act on.
        return Outcome("failed", "no Flex query id configured")

    try:
        result = sync_journal(
            conn=conn, archive_dir=ctx.archive_dir, query_id=ctx.query_id,
            assets=ctx.assets,
        )
    except (FetchCooldown, TokenMissing, TokenRejected) as exc:
        return sync_outcome(exc)
    return sync_outcome(result)


#: How long a Trade Confirmation poll waits before asking again.
#:
#: Far below the Activity Statement's 15 minutes, and for a reason specific to the
#: query TYPE: a statement is regenerated once a day, so a second fetch inside the
#: window cannot return new information, where a confirm payload grows with every
#: fill. The cooldown in `flex._check_cooldown` is keyed by query id, so the two
#: budgets are independent and this does not loosen the statement's.
#:
#: Ten minutes against IBKR's published pacing of 10 requests/minute per token is
#: roughly 0.6% of the allowance, so the poll cadence is bounded by usefulness
#: rather than by the budget.
CONFIRM_COOLDOWN_S = 600


def _confirm(conn: sqlite3.Connection, ctx: Context) -> Outcome:
    """Fetch same-session fills from the Trade Confirmation query and ingest them.

    NOT CONFIGURED IS `nothing`, NOT `failed`. A journal with no confirm query is
    the normal case -- only the Activity Statement is required for this app to
    work -- so an absent id must not accumulate failures and back a job off, and
    must not colour the page's Collection card red for a feature nobody enabled.

    The id is read from settings HERE rather than carried on `Context`, so saving
    it in the page takes effect on the next tick instead of on the next restart --
    the same reason `web._effective_query_id` resolves per request.

    The base currency comes from the journal, because a confirm payload has no
    AccountInformation section to state it. With no statement ingested yet there is
    nothing to convert against, and that is reported rather than guessed at.
    """
    query_id = prefs.confirm_query_id()
    if not query_id:
        return Outcome("nothing", "no Trade Confirmation query configured")

    base = _base_currency(conn)
    if not base:
        # `nothing`, not `failed`, for the same reason an absent query id is: the
        # precondition is unmet rather than broken, and the daily sync resolves it
        # on its own. Counting it would back this job off five ticks into a fresh
        # clone's first market session -- exactly when it should be waiting.
        return Outcome(
            "nothing",
            "no ingested statement to read the base currency from; a sync has to "
            "land first",
        )
    try:
        result = fetch_confirms(
            query_id, archive_dir=ctx.archive_dir, cooldown_s=CONFIRM_COOLDOWN_S,
        )
    except FetchCooldown as exc:
        return Outcome("nothing", f"cooldown: {exc}")
    except (TokenMissing, TokenRejected) as exc:
        return Outcome("failed", f"credentials: {exc}")

    ingested = ingest_confirms(
        conn, result.raw_path, base_currency=base, assets=ctx.assets or ASSET_FILTER_ALL,
    )
    wrote = ingested.trades_inserted + ingested.trades_superseded
    detail = (f"{ingested.trades_inserted} new, {ingested.trades_superseded} "
              f"updated, {ingested.trades_skipped_existing} already known")
    if ingested.warnings:
        detail += "; " + "; ".join(ingested.warnings)[:200]
    # `total` is every execution the payload held, which is the sum of the
    # dispositions -- `IngestResult` counts what it WROTE, and a progress bar wants
    # the denominator.
    seen = (ingested.trades_inserted + ingested.trades_superseded
            + ingested.trades_skipped_existing + ingested.trades_filtered_out)
    return Outcome("ok" if wrote else "nothing", detail, wrote, seen)


def _base_currency(conn: sqlite3.Connection) -> str | None:
    """The account's base currency, from the newest ingested statement.

    A confirm payload carries no AccountInformation section, so the journal's own
    statements are the only honest source. `None` when nothing is ingested yet, and
    the caller reports that rather than defaulting: converting a USD fill into a
    currency this account may not even use would be a guess wearing a figure's
    clothes, and it would reach the scoreboard.
    """
    row = conn.execute(
        "SELECT base_currency FROM statements WHERE base_currency IS NOT NULL"
        " ORDER BY ingested_at DESC LIMIT 1"
    ).fetchone()
    return str(row["base_currency"]) if row and row["base_currency"] else None


def sync_outcome(result: dict[str, Any] | Exception) -> Outcome:
    """The ledger's reading of one sync, from its reply or from what it raised.

    ONE MAPPING, THREE CALLERS, and it is shared because the alternative was
    measured: `POST /api/sync` and `optjournal sync` did the work and wrote no
    ledger row at all, so a hand-run sync that fixed the journal left
    `consecutive_failures` where it was -- and the reconciler went on refusing to
    start a job that had been working for hours. Two "sync now" controls on one
    page, only one of which the scheduler could learn from.

    `nothing` for a cooldown: the cooldown is the system working, and retrying is
    what it exists to prevent. It DOES clear a backoff, which is deliberate -- the
    counter means "consecutive failures", and being told to wait is not one.
    """
    if isinstance(result, FetchCooldown):
        return Outcome("nothing", f"cooldown: {result}")
    if isinstance(result, TokenMissing | TokenRejected):
        return Outcome("failed", f"credentials: {result}")
    if isinstance(result, Exception):  # pragma: no cover - callers narrow first
        return Outcome("failed", str(result)[:400])
    return Outcome(
        "ok" if result["changed"] else "nothing",
        result["summary"], result["new_trades"], result["new_trades"],
    )


def record_manual_sync(
    conn: sqlite3.Connection, result: dict[str, Any] | Exception
) -> None:
    """Write the ledger row for a sync no schedule claimed.

    For the page's Sync button and for `optjournal sync`, which run the same work
    as the scheduled job and were invisible to the ledger until this existed. It
    goes through `record_run`, so it inherits that function's policy of logging and
    swallowing its own failures: bookkeeping must not fail the work it describes.
    """
    outcome = sync_outcome(result)
    record_run(conn, "sync", status=outcome.status, detail=outcome.detail,
               done=outcome.done, total=outcome.total)


#: Every job, in DECLARATION ORDER, and the order is load-bearing. Step 6's
#: worker runs due jobs sequentially down this tuple, which turns "sync before
#: bars_daily" into a real happens-before edge -- `bars_daily` derives its
#: manifest from the positions the sync ingests. Today that ordering is two
#: wall-clock guesses plus a coin flip: MeshClaw's `_compute_jitter` returns
#: `random.uniform(0, 59*60)` for these expressions, and it was observed putting a
#: 538 ms audit job 25 minutes late.
JOBS: tuple[Job, ...] = (
    Job(
        name="sync",
        run=_sync,
        minute=0, hour=12, weekdays=(2, 3, 4, 5, 6), zone="Europe/Dublin",
        # A missed noon is worth running at 18:00: the docstring records a
        # badly-timed sync missing Monday's fills twice.
        catchup=Catchup.LATEST, window_s=12 * 3600,
        timeout_s=900,
        spends_broker_request=True,
    ),
    Job(
        name="confirm",
        run=_confirm,
        # SAME SHAPE AS `bars_live`, for the same reason: due-ness is "inside the
        # session AND the last success is over 25 minutes old", so one predicate
        # replaces a row of cron slots and a laptop that slept through two hours
        # collects on its first tick awake rather than waiting for the next slot.
        #
        # US market hours, because that is when a fill can happen on this account.
        # Outside them the payload cannot change, and polling it would spend
        # requests to re-read the morning.
        minute=35, hour=9, weekdays=(1, 2, 3, 4, 5), zone="America/New_York",
        catchup=Catchup.WINDOW, window_s=25 * 60,
        timeout_s=300,
        spends_broker_request=True,
    ),
    Job(
        name="bars_daily",
        run=lambda conn, ctx: _bars(conn, ctx, live=False),
        # 12:30, thirty minutes behind the sync: a position opened yesterday is
        # only in the database once that sync has ingested it, and the manifest is
        # derived from positions. The gap clears the sync's 900s worst case.
        minute=30, hour=12, weekdays=(2, 3, 4, 5, 6), zone="Europe/Dublin",
        catchup=Catchup.LATEST, window_s=20 * 3600,   # re-fetchable by definition
        timeout_s=600,
    ),
    Job(
        name="bars_live",
        run=lambda conn, ctx: _bars(conn, ctx, live=True),
        # NOT a wall-clock fire. The minute/hour are the session's own open in ET,
        # kept so the schedule reads consistently, but `Catchup.WINDOW` means due-
        # ness is "inside the session AND the last success is over 55 minutes old"
        # -- one predicate replacing seven cron slots. Sound because the intraday
        # series is CUMULATIVE within a session: a 13:00 poll returns every
        # completed bar since the open, so one wake at 14:30 after sleeping since
        # 10:00 collects the whole session.
        minute=5, hour=10, weekdays=(1, 2, 3, 4, 5), zone="America/New_York",
        catchup=Catchup.WINDOW, window_s=55 * 60,
        timeout_s=300,
    ),
    Job(
        name="market",
        run=_market,
        # NEVER REGISTERED WITH MESHCLAW. 11:00 rather than the shim's suggestion
        # of the same hour as the sync: the calendar feed is independent of IBKR,
        # so there is no reason for the two to contend, and an hour before the
        # sync means the week's releases are on screen before the fills are.
        minute=0, hour=11, weekdays=(1, 2, 3, 4, 5), zone="Europe/Dublin",
        catchup=Catchup.LATEST, window_s=24 * 3600,   # the feed serves this week
        timeout_s=120,
    ),
)


def job_by_name(name: str) -> Job:
    """The registry entry, or `UnknownJob`.

    A linear scan over four entries rather than a dict built beside the tuple:
    two containers holding the same registry is one more thing that can disagree,
    and 4 comparisons is not a cost.
    """
    for job in JOBS:
        if job.name == name:
            return job
    raise UnknownJob(name)


#: Where a job's lock file lives, under the archive rather than beside the
#: database: `raw/` is already the provenance root and is already excluded from
#: anything that copies the journal.
JOB_LOCK_DIR = "jobs"


def job_lock_path(archive_dir: Path, name: str) -> Path:
    return archive_dir / JOB_LOCK_DIR / f"{name}.lock"


def run_job(
    conn: sqlite3.Connection,
    name: str,
    *,
    ctx: Context,
    fired_for: int | None = None,
) -> int:
    """Run one job under its own lock, recording before and after. Returns run id.

    THE CLAIM IS WRITTEN AND COMMITTED BEFORE THE WORK STARTS, and this is the
    one ordering decision in the module worth arguing with. The adversarial review
    refuted the alternative precisely here: hold the run in memory and write the
    row on a terminal state, and the window between "is this slot claimed?" and
    "this slot is claimed" spans the entire job. A SIGKILL mid-fetch -- which
    launchd `KeepAlive` makes routine, ~10s respawn -- then leaves no row and no
    stamp, because `flex._record_fetch` runs only after a successful download. The
    next reconcile finds the slot unclaimed and spends a SECOND IBKR request,
    deterministically, with no concurrency involved. So: claim, commit, then work.

    THE LOCK IS AN OS FILE LOCK, NOT A THREADING LOCK OR A TABLE, for one reason: the
    kernel releases it when the process dies. That is what makes an interrupted
    run detectable without a PID, a heartbeat or a staleness guess -- see
    `interrupted_runs`. Non-blocking (`timeout_s=0`): every job here is idempotent
    and cheap to retry on the next tick, so `JobBusy` is a better answer than a
    queue behind something that spends IBKR requests.

    Raises `UnknownJob` (no such name) or `JobBusy` (already running). Any other
    exception is recorded as `failed` and re-raised, because a caller that asked
    for a run is entitled to the traceback -- swallowing it here is what turned
    the keychain failure into a message that reached nobody.
    """
    job = job_by_name(name)
    lock = job_lock_path(ctx.archive_dir, job.name)
    try:
        with locked(lock, timeout_s=0):
            return _run_locked(conn, job, ctx=ctx, fired_for=fired_for)
    except LockTimeout:
        # Held by another runner. The row it committed before starting is what
        # tells the caller which run to watch.
        raise JobBusy(job.name, _running_id(conn, job.name)) from None


def _run_locked(
    conn: sqlite3.Connection, job: Job, *, ctx: Context, fired_for: int | None
) -> int:
    """The body of `run_job`, with the lock held. See its docstring for the why."""
    started = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        cursor = conn.execute(
            "INSERT INTO job_runs (job, fired_for, started_at, status, detail)"
            " VALUES (?,?,?,'running',NULL)",
            (job.name, fired_for, started),
        )
        run_id = int(cursor.lastrowid or 0)
        conn.commit()          # COMMITTED before the work: see run_job's docstring
    except sqlite3.IntegrityError:
        # The partial unique index refused a duplicate `fired_for` -- another
        # runner claimed this instant first. ROLLED BACK EXPLICITLY, because
        # sqlite3 does NOT roll back on error and a connection left in a
        # transaction holds the write lock for the whole BUSY_TIMEOUT_MS.
        # Measured: the next writer waits 15.55s and then fails, which would mean
        # the scheduler wedging its own database by losing a race it was DESIGNED
        # to lose.
        conn.rollback()
        raise JobBusy(job.name, _running_id(conn, job.name)) from None

    try:
        outcome = job.run(conn, ctx)
    except Exception as exc:                      # noqa: BLE001 - recorded, re-raised
        _finish(conn, run_id, Outcome("failed", f"{type(exc).__name__}: {exc}"[:400]))
        raise
    _finish(conn, run_id, outcome)
    return run_id


def _finish(conn: sqlite3.Connection, run_id: int, outcome: Outcome) -> None:
    """Stamp the terminal state onto the claim row, and update the anchor.

    Two statements in one transaction: a run whose history says `ok` while the
    anchor still says `running` would make the job look permanently in flight.
    """
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        conn.execute(
            "UPDATE job_runs SET finished_at = ?, status = ?, detail = ?,"
            " done = ?, total = ? WHERE id = ?",
            (stamp, outcome.status, outcome.detail or None,
             outcome.done, outcome.total, run_id),
        )
        row = conn.execute(
            "SELECT job, fired_for FROM job_runs WHERE id = ?", (run_id,)).fetchone()
        if row is not None:
            _upsert_state(conn, str(row["job"]), outcome.status, row["fired_for"])
            prune_runs(conn, str(row["job"]))
        conn.commit()
    except sqlite3.Error as exc:
        # Same policy as `record_run`: bookkeeping must not fail the work it
        # describes, and must not hold the write lock on the way down.
        log.warning("could not finish run %s: %s", run_id, exc)
        if conn.in_transaction:
            conn.rollback()


def _running_id(conn: sqlite3.Connection, job: str) -> int | None:
    """The newest `running` row for `job`, so a 409 can name what to watch."""
    row = conn.execute(
        "SELECT id FROM job_runs WHERE job = ? AND status = 'running'"
        " ORDER BY id DESC LIMIT 1", (job,)).fetchone()
    return None if row is None else int(row["id"])


def interrupted_runs(conn: sqlite3.Connection, *, archive_dir: Path) -> int:
    """Resolve `running` rows whose process is gone. Returns rows updated.

    THE KERNEL ANSWERS THIS, NOT A HEURISTIC. A `running` row whose per-job
    file lock can be acquired has no live holder: OS locks release on process death,
    including `SIGKILL`, so there is no PID to check, no timeout to tune, and the
    answer is correct across laptop sleep -- where every wall-clock staleness rule
    is wrong, because this machine measured 44.6 hours of sleep excluded from
    `monotonic`.

    Called on page load rather than by a scheduled job, for the same reason the
    perishable audit moved out of a cron: a watchdog that is itself scheduled stops
    when the scheduler does.

    Four sub-millisecond lock attempts. `LOCK_NB` never waits, so a job that IS
    running costs one failed syscall and keeps its row.
    """
    stale = [
        str(row["job"]) for row in conn.execute(
            "SELECT DISTINCT job FROM job_runs WHERE status = 'running'")
    ]
    updated = 0
    for name in stale:
        try:
            with locked(job_lock_path(archive_dir, name), timeout_s=0):
                pass          # acquired, so nothing holds it: the runner is gone
        except LockTimeout:
            continue          # genuinely running
        except OSError as exc:
            # An unreadable lock directory must not blank a page load.
            log.warning("could not probe %s's lock: %s", name, exc)
            continue
        try:
            cursor = conn.execute(
                "UPDATE job_runs SET status = 'interrupted', finished_at = ?,"
                " detail = COALESCE(detail, 'the process died before finishing')"
                " WHERE job = ? AND status = 'running'",
                (datetime.now(UTC).isoformat(timespec="seconds"), name),
            )
            updated += cursor.rowcount or 0
            _upsert_state(conn, name, "interrupted", None)
            conn.commit()
        except sqlite3.Error as exc:
            log.warning("could not resolve %s's interrupted run: %s", name, exc)
            if conn.in_transaction:
                conn.rollback()
    return updated


# ---------------------------------------------------------------------------
# Due-ness (SCHEDULER_PLAN.md step 6a).
#
# A PURE FUNCTION over a clock and a ledger snapshot, which is the only reason the
# whole catch-up policy is testable: every rule below is exercised by passing a
# `datetime` rather than by waiting for one. Both DST boundaries, a rebuilt
# journal, a laptop that slept through noon -- all of them are arguments here.
#
# THE STAKES. Getting this wrong in the permissive direction spends real IBKR
# requests against a hard lockout budget; getting it wrong in the other direction
# silently converts "missed, unrecoverable" into nothing at all, because an
# option's intraday series exists only while its own session runs. So the rules are
# stated as separate named predicates rather than folded into one condition.
# ---------------------------------------------------------------------------

#: The US session, in `America/New_York`, as the hours `bars_live` may poll in.
#: 09:30-16:00 is the cash session; the poll runs from the first completed hourly
#: bar (10:00) to one past the close (16:05 in the cron it replaces), so the window
#: is expressed as hours-past-the-open rather than as a second set of clock times
#: that could disagree with `clock.MARKET_TZ`.
SESSION_OPEN_H = 9
SESSION_OPEN_M = 30
SESSION_CLOSE_H = 16
#: Minutes past the close that a poll is still useful: the 16:00 bar is only on the
#: grid once the hour has completed.
SESSION_TAIL_M = 10


@dataclass(frozen=True)
class Due:
    """One job that should run now, and the instant it stands for.

    `fired_for` is what makes catch-up idempotent: it goes into `job_runs` under a
    partial unique index, so two reconcilers racing the same instant cannot both
    claim it. `None` for a `WINDOW` job, which claims no instant -- see `_window_due`.
    """

    job: Job
    fired_for: int | None
    #: Why it is due, for the log. A scheduler that fires without saying why is
    #: the thing this whole plan is replacing.
    reason: str


def _last_instant(job: Job, now: datetime) -> datetime | None:
    """The most recent scheduled instant at or before `now`, in the job's own zone.

    Walks back a bounded number of days rather than computing, because the weekday
    set makes closed-form arithmetic fiddly and eight comparisons cost nothing.

    DST IS HANDLED BY `ZoneInfo`, not by us, and both edges matter:

    * FALL BACK. 01:30 happens twice; `fold` defaults to 0, so this returns the
      FIRST occurrence and `fired_for` is the same epoch either way -- which is why
      the repeated hour cannot fire twice. The unique index is the actual guard.
    * SPRING FORWARD. A 02:30 schedule does not exist on that day. `ZoneInfo`
      normalises it to 03:30, so the job fires an hour late that once rather than
      being skipped. No job here is scheduled in the missing hour, but a future one
      might be, and silently skipping a day is worse than running late.
    """
    local = now.astimezone(job.tz())
    # 8 days back covers any weekday set: a Monday-only job asked on a Sunday is
    # six days behind its last fire.
    for back in range(9):
        day = (local - timedelta(days=back)).date()
        if day.isoweekday() not in job.weekdays:
            continue
        instant = datetime(
            day.year, day.month, day.day, job.hour, job.minute, tzinfo=job.tz()
        )
        if instant <= now:
            return instant
    return None


def _in_session(now: datetime) -> bool:
    """Whether `now` is inside the US cash session, in market time.

    Weekday only, and deliberately calendar-free: a market holiday is not
    distinguishable here, and the cost of polling on one is a request that returns
    no bars and records `nothing`. That is the correct outcome anyway -- an empty
    poll on a holiday is not a failure, and inventing a holiday calendar to avoid
    it would be a second source of truth about what the market did, which
    `bars.market_traded_on` already answers FROM THE DATA.
    """
    local = now.astimezone(ZoneInfo("America/New_York"))
    if local.isoweekday() > 5:
        return False
    opens = local.replace(hour=SESSION_OPEN_H, minute=SESSION_OPEN_M,
                          second=0, microsecond=0)
    closes = local.replace(hour=SESSION_CLOSE_H, minute=SESSION_TAIL_M,
                           second=0, microsecond=0)
    return opens <= local <= closes


def _window_due(job: Job, now: datetime, last_poll: int | None) -> Due | None:
    """`Catchup.WINDOW`: inside the session, and the last POLL is old enough.

    SEVEN CRON SLOTS COLLAPSE INTO THIS ONE PREDICATE, and it is sound only because
    the intraday series is CUMULATIVE within a session: a 13:00 poll returns every
    completed bar since the open, so one wake at 14:30 after sleeping since 10:00
    collects the whole session. That is launchd's coalescing expressed in the job
    rather than begged from the substrate.

    Outside the window it is NEVER due. A 20:00 wake must not fetch nothing and
    record `ok` -- that is precisely the inversion the perishable audit exists to
    catch, one layer down.

    Claims NO `fired_for`, because there is no instant: the run is "this session, at
    whatever moment we woke". The partial unique index accepts repeats of NULL,
    which is what lets a session be polled several times.
    """
    if not _in_session(now):
        return None
    if last_poll is not None:
        age = int(now.timestamp()) - last_poll
        if age < job.window_s:
            return None
        return Due(job, None, f"in session, last poll {age}s ago")
    return Due(job, None, "in session, no completed poll yet today")


def due_jobs(
    now: datetime,
    *,
    claimed: dict[str, set[int]],
    last_poll: dict[str, int],
    ever_ran: set[str],
    registry: tuple[Job, ...] | None = None,
) -> list[Due]:
    """Which jobs should run at `now`. Pure: no clock, no database, no I/O.

    The three ledger arguments are snapshots the caller reads once, so a tick makes
    one pass over `job_runs` rather than four queries per job:

    * `claimed` -- `fired_for` instants already recorded per job. RECORDED, not
      succeeded, and that distinction is the brake. Keying on `status='ok'` would
      make a job that failed for a real reason (the locked keychain that actually
      happened) due again on the very next tick and for its whole 12-hour window --
      roughly 48 real IBKR requests in twelve hours against a lockout budget.
      `consecutive_failures` on `job_state` is what a human reads instead.
    * `last_poll` -- newest COMPLETED (`ok` or `nothing`) epoch per job, for
      `WINDOW` jobs only. Not `ok` alone: see `_ledger_snapshot`.
    * `ever_ran` -- jobs with ANY recorded run.

    EMPTY LEDGER MEANS UNKNOWN, NOT OVERDUE. `job_runs` lives in `journal.db`,
    which a `raw/` restore rebuilds from scratch, so a rebuilt journal has no runs
    at all. Treating that as "everything is overdue" makes the first reconcile after
    a restore spend an IBKR request plus 24 bar requests, unprompted, on a machine
    whose owner was recovering from something. So a job with no recorded run waits
    for its next natural slot: it is scheduled, never caught up.

    Returned in REGISTRY ORDER, which the caller must preserve: `sync` before
    `bars_daily`, because the latter derives its manifest from the positions the
    former ingests. That ordering is a real happens-before edge here, where today it
    is two wall-clock guesses plus MeshClaw's `random.uniform(0, 59*60)` jitter.
    """
    # `None` rather than `= JOBS`, and this was a real bug rather than a style
    # preference: a default argument is evaluated at DEFINITION time, so the tuple
    # was captured once at import and `monkeypatch.setattr(jobs, "JOBS", ...)` never
    # reached this function. Every test that replaced the registry was silently
    # exercising the REAL one -- they passed because the real schedules happened to
    # agree, which is the worst way for a test to pass. Found by running the loop
    # against a stubbed one-job registry and watching zero runs happen.
    out: list[Due] = []
    for job in (JOBS if registry is None else registry):
        if job.catchup is Catchup.WINDOW:
            found = _window_due(job, now, last_poll.get(job.name))
            if found is not None:
                out.append(found)
            continue

        instant = _last_instant(job, now)
        if instant is None:
            continue
        stamp = int(instant.timestamp())
        if stamp in claimed.get(job.name, set()):
            continue                       # already recorded, by anyone
        if job.name not in ever_ran:
            # See the docstring: a journal with no history is UNKNOWN, not behind.
            continue
        if job.catchup is Catchup.NONE:
            continue
        behind = int(now.timestamp()) - stamp
        if behind > job.window_s:
            continue                       # too old to be worth catching up
        out.append(Due(job, stamp, f"scheduled {instant.isoformat()}, {behind}s late"))
    return out


# ---------------------------------------------------------------------------
# The reconciler (SCHEDULER_PLAN.md step 6b).
#
# A WALL-CLOCK CATCH-UP RECONCILER, NEVER A SLEEPING TIMER, and that is forced by
# measurement rather than chosen for elegance. On this machine:
#
#     monotonic impl: mach_absolute_time()
#     monotonic        = 138522
#     CLOCK_UPTIME_RAW = 138522     <-- identical: monotonic EXCLUDES sleep
#     CLOCK_MONOTONIC  = 298925
#     sleep excluded from monotonic: 44.6 hours
#
# So `event.wait(seconds_until_next_fire)` is not approximately right here, it is
# 55% slow: 44.3 h asleep out of 80.7 h wall, across 292 sleep/wake cycles averaging
# 7.5 minutes awake. A fixed 60 s tick is never accumulated into a deadline, so
# oversleeping costs one tick of latency and nothing else -- a noon job on a laptop
# asleep at noon runs within a minute of the lid opening.
# ---------------------------------------------------------------------------

#: How often the loop wakes. Not a schedule: due-ness is recomputed from the wall
#: clock every tick, so this only bounds LATENCY. 60 s costs one pass over
#: `job_runs` per minute (measured in microseconds on a 200-row-per-job table).
TICK_S = 60

#: Above this many consecutive failures a job stops being started by the
#: reconciler. It stays runnable BY HAND from the page, which is the point: a job
#: failing for a real reason should stop hammering the endpoint that is failing,
#: without becoming invisible or requiring a restart to retry.
FAILURE_BACKOFF = 5

#: How far the wall clock must run ahead of `monotonic` within one tick before the
#: run is stamped `slept`. Generous: a normal 60 s tick shows sub-millisecond drift,
#: so anything at this scale is a genuine suspend.
SLEPT_THRESHOLD_S = 90


def _ledger_snapshot(conn: sqlite3.Connection) -> tuple[
    dict[str, set[int]], dict[str, int], set[str], dict[str, int]
]:
    """(claimed, last_poll, ever_ran, failures) in ONE pass over the ledger.

    One query rather than four per job, because this runs every 60 seconds against
    the same database a job may be writing. `fired_for IS NOT NULL` is the only
    filter that matters: a NULL claim belongs to a WINDOW job, which is braked by
    `last_poll` instead.
    """
    claimed: dict[str, set[int]] = {}
    last_poll: dict[str, int] = {}
    ever_ran: set[str] = set()
    for row in conn.execute(
        "SELECT job, fired_for, status, finished_at FROM job_runs"
    ):
        job = str(row["job"])
        ever_ran.add(job)
        if row["fired_for"] is not None:
            claimed.setdefault(job, set()).add(int(row["fired_for"]))
        # `ok` OR `nothing`, and the distinction is the brake -- the same
        # distinction `claimed` above already draws for the instant-claiming jobs,
        # never applied here until a WINDOW job started spending IBKR requests.
        #
        # A poll that fetched cleanly and found nothing new reports `nothing`,
        # which is the honest status and must stay so: an empty run is not a
        # success. But it DID ask, so braking on `ok` alone left the job due on
        # every 60s tick for the rest of the session. Measured on the real journal:
        # 127 `confirm` runs in four hours, 125 of them `nothing`, one IBKR request
        # spent every time the 10-minute cooldown lapsed instead of every 25
        # minutes. `due_jobs`' own docstring predicted this for `claimed`: "keying
        # on status='ok' would make a job ... due again on the very next tick ...
        # roughly 48 real IBKR requests in twelve hours against a lockout budget."
        #
        # `failed` deliberately does NOT brake: a transient failure should retry
        # inside the session, and repeated ones are what FAILURE_BACKOFF is for.
        if row["status"] in ("ok", "nothing") and row["finished_at"]:
            stamp = _epoch_of(str(row["finished_at"]))
            if stamp is not None:
                last_poll[job] = max(last_poll.get(job, 0), stamp)
    failures = {
        str(r["job"]): int(r["consecutive_failures"] or 0)
        for r in conn.execute("SELECT job, consecutive_failures FROM job_state")
    }
    return claimed, last_poll, ever_ran, failures


def _epoch_of(stamp: str) -> int | None:
    """Epoch seconds from an ISO timestamp written by this package.

    Tolerant of a missing offset: every stamp this module writes carries one, but a
    row hand-inserted by a migration or a test may not, and a `ValueError` here
    would kill the tick over a formatting detail.
    """
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _last_failure(conn: sqlite3.Connection, job: str) -> str | None:
    """The detail of this job's most recent failed run, for the backoff warning.

    Its own query rather than a column on `_ledger_snapshot`, because it is needed
    only on the branch that logs -- at most once per backed-off job per tick, where
    the snapshot runs every tick for every job. Failing quietly is right here: this
    exists to enrich a log line, and a broken read of the ledger must not stop the
    reconciler from running the work.
    """
    try:
        row = conn.execute(
            "SELECT detail FROM job_runs WHERE job = ? AND status = 'failed'"
            " ORDER BY id DESC LIMIT 1",
            (job,),
        ).fetchone()
    except sqlite3.Error:  # pragma: no cover - a broken ledger must not stop work
        return None
    return str(row["detail"]) if row and row["detail"] else None


def reconcile(
    conn: sqlite3.Connection,
    *,
    ctx: Context,
    now: datetime | None = None,
    slept: bool = False,
) -> list[str]:
    """Run whatever is due, once. Returns the names of the jobs started.

    ONE PASS, SEQUENTIAL, IN REGISTRY ORDER, which is what turns "sync before
    bars_daily" into a real happens-before edge rather than two wall-clock guesses.

    EACH JOB IS CONTAINED. A due-check or a run that raises must not stop the tick,
    because a daemon thread that dies leaves the HTTP server perfectly healthy and
    the schedule dead -- the 40-hour outage this plan exists to fix, reproduced
    inside its own fix. So every job is wrapped, and the failure is RECORDED rather
    than logged and forgotten.

    `now` is injectable for the same reason `due_jobs` is pure: the tick's behaviour
    at a DST boundary is testable without waiting for October.
    """
    moment = now or datetime.now(UTC)
    claimed, last_poll, ever_ran, failures = _ledger_snapshot(conn)
    started: list[str] = []
    for due in due_jobs(moment, claimed=claimed, last_poll=last_poll,
                        ever_ran=ever_ran):
        if failures.get(due.job.name, 0) >= FAILURE_BACKOFF:
            # Backed off, not disabled: still runnable by hand from the page, and
            # the count resets on any healthy outcome.
            #
            # THE REASON IS LOGGED WITH IT, because the version that logged only
            # the count produced 344 identical lines across two weeks of a real
            # outage and named the cause in none of them -- the cause was sitting in
            # `job_runs.detail`, which takes a SQL client to read. One extra query
            # per backed-off job per tick, on a table this loop already reads.
            log.warning("%s: backed off after %d consecutive failures; last: %s",
                        due.job.name, failures[due.job.name],
                        _last_failure(conn, due.job.name) or "reason not recorded")
            continue
        log.info("%s is due (%s)", due.job.name, due.reason)
        try:
            run_id = run_job(conn, due.job.name, ctx=ctx, fired_for=due.fired_for)
            started.append(due.job.name)
            if slept and run_id:
                # Why a noon job fired at 09:14 becomes a field rather than a
                # mystery. Both clocks are already read, so this is free.
                conn.execute("UPDATE job_runs SET slept = 1 WHERE id = ?", (run_id,))
                conn.commit()
        except JobBusy:
            # Another runner has it -- the page, or a previous tick still working.
            # Not an error: the file lock and the unique index are doing their job.
            log.info("%s is already running", due.job.name)
        except Exception:                     # noqa: BLE001 - see the docstring
            # `run_job` already recorded `failed` with the cause before re-raising.
            # Swallowed HERE so one job cannot stop the others or kill the tick.
            log.exception("%s failed", due.job.name)
    return started


def heartbeat(conn: sqlite3.Connection, *, now: datetime | None = None) -> None:
    """Stamp the tick loop's own liveness onto every registered job.

    WRITTEN BY THE LOOP, NOT BY A JOB, and that separation is the whole point of
    having two signals. "Did the last run succeed" and "is anything driving the
    schedule" are different questions, and `crons.json` answered the first with `ok`
    for two days while the answer to the second was no. A heartbeat written by a job
    would collapse them again.

    On every registered job rather than one row, so `jobs_data` can take the
    freshest value across jobs without a table of its own.
    """
    stamp = int((now or datetime.now(UTC)).timestamp())
    try:
        for job in JOBS:
            conn.execute(
                "INSERT INTO job_state (job, heartbeat_at, consecutive_failures)"
                " VALUES (?,?,0) ON CONFLICT(job) DO UPDATE SET heartbeat_at = ?",
                (job.name, stamp, stamp),
            )
        conn.commit()
    except sqlite3.Error as exc:
        # A heartbeat that cannot be written must not kill the loop that writes it.
        log.warning("could not write the heartbeat: %s", exc)
        if conn.in_transaction:
            conn.rollback()


class Scheduler:
    """The tick loop, as an object so `serve` can stop it deterministically.

    A `threading.Event` for the stop signal rather than a flag, so shutdown does
    not wait out a 60-second sleep -- which matters more than it sounds: step 7 has
    to make SIGTERM work, and a loop that ignores its stop signal for a minute is
    indistinguishable from a hung one.

    NOT a daemon thread that nobody joins. `stop()` waits, so a test cannot leave a
    scheduler running against a database the next test is about to delete.
    """

    def __init__(self, *, ctx: Context, tick_s: int = TICK_S) -> None:
        self.ctx = ctx
        self.tick_s = tick_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Ticks ATTEMPTED, not completed, and the distinction is this project's
        #: whole subject. A loop counting only successes reads as dead while it is
        #: alive and failing every tick -- which is `crons.json` reporting `ok` for
        #: two days, inverted. "Is the loop alive" and "are its ticks working" are
        #: two questions, so they are two counters.
        self.ticks = 0
        #: Ticks that raised. Nonzero with `ticks` climbing means alive but broken,
        #: which is a different repair from either alone.
        self.tick_failures = 0

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("this scheduler is already running")
        # ONE LINE AT STARTUP, so the log proves the loop exists.
        #
        # Found by running it: a `serve` with a live scheduler wrote a log file of
        # ZERO BYTES, because `reconcile` only logs when something is due and
        # nothing was. A log that is empty because all is well is indistinguishable
        # from a log that is empty because the loop is dead -- which is this
        # project's signature failure, and it would be absurd to reintroduce it in
        # the logging added to prevent it. The heartbeat is the machine-readable
        # answer; this is the human-readable one.
        log.info("scheduler starting, %ss tick, %d job(s): %s",
                 self.tick_s, len(JOBS), ", ".join(j.name for j in JOBS))
        self._thread = threading.Thread(
            target=self._loop, name="optjournal-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Signal the loop and wait for it. Idempotent."""
        self._stop.set()
        if self._thread is not None:
            log.info("scheduler stopping after %d tick(s), %d failed",
                     self.ticks, self.tick_failures)
            self._thread.join(timeout)
            self._thread = None

    def _loop(self) -> None:
        """One connection for the loop's lifetime, and every tick contained.

        Its OWN connection, not the handlers': `sqlite3` objects are not safe to
        share across threads, and the server is threaded. Opened inside the thread
        so the object is created where it is used.
        """
        from optjournal.db import connect, migrate  # noqa: PLC0415 - see below

        conn = connect(self.ctx.db_path)
        migrate(conn)
        # Wall AND monotonic, so a suspend is detectable: monotonic excludes sleep
        # on this platform (measured, 44.6 hours), so the two diverging by more than
        # a tick means the machine was asleep.
        wall = datetime.now(UTC)
        mono = time.monotonic()
        try:
            while True:
                self.ticks += 1               # ATTEMPTED: see the attribute's note
                try:
                    now = datetime.now(UTC)
                    elapsed_wall = (now - wall).total_seconds()
                    elapsed_mono = time.monotonic() - mono
                    slept = elapsed_wall - elapsed_mono > SLEPT_THRESHOLD_S
                    wall, mono = now, time.monotonic()
                    heartbeat(conn, now=now)
                    reconcile(conn, ctx=self.ctx, now=now, slept=slept)
                except Exception:             # noqa: BLE001 - the point of the loop
                    self.tick_failures += 1
                    # A tick that raises must not end the schedule. Design 3 named
                    # this exactly: a daemon thread that dies leaves the HTTP server
                    # perfectly healthy and the schedule dead, which is the outage
                    # this plan exists to fix.
                    log.exception("scheduler tick failed")
                if self._stop.wait(self.tick_s):
                    return
        finally:
            conn.close()
