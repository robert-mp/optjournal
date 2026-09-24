"""The run ledger: what gets recorded, and what a green status is allowed to mean.

The failure these guard is specific and was measured on the real machine, not
imagined. `~/.meshclaw/crons.json` read `last_status: "ok"` for
optjournal-bars-live, bars-daily AND bars-audit, while `price_bars.fetched_at`
shows 28 hourly rows written on 08-06, two on 08-07, and nothing after until a
human ran the command by hand. Three jobs green for two days while collecting
nothing -- including the audit job whose only purpose is noticing exactly that.

So the tests here are mostly about the DISTINCTIONS: `ok` when work landed,
`nothing` when a run was legitimately empty, `failed` when it broke. A ledger that
collapses those is the ledger that already existed.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest
from conftest import connect_migrated

from optjournal.jobs import KNOWN_JOBS, RUN_HISTORY, prune_runs, record_run


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    return connect_migrated(tmp_path / "j.db")


def test_a_run_records_both_the_row_and_the_anchor(conn):
    """One call writes history AND updates the scheduling anchor.

    Two tables, so it would be possible to write one and not the other -- and the
    anchor is what a catch-up reconciler reads, so a missing update means the job
    looks like it never ran.
    """
    run_id = record_run(conn, "bars_live", status="ok", detail="3 windows",
                        done=3, total=3)
    assert run_id > 0

    run = conn.execute("SELECT * FROM job_runs WHERE id = ?", (run_id,)).fetchone()
    assert (run["job"], run["status"], run["done"], run["total"]) == (
        "bars_live", "ok", 3, 3)
    assert run["finished_at"], "a completed run must be stamped finished"

    state = conn.execute(
        "SELECT * FROM job_state WHERE job = 'bars_live'").fetchone()
    assert state["last_status"] == "ok"
    assert state["consecutive_failures"] == 0


def test_an_empty_run_is_nothing_not_ok(conn):
    """THE distinction this ledger exists for.

    A run that fetched no bars is not a successful run, and it is not a failure
    either. Recording it as `ok` is precisely what let three cron jobs report health
    for two days while `price_bars` gained nothing. The page can render `nothing`
    differently from `ok`; it cannot un-conflate them after the fact.
    """
    record_run(conn, "bars_live", status="nothing", detail="0 bar(s), 4 empty")
    row = conn.execute("SELECT status FROM job_runs").fetchone()
    assert row["status"] == "nothing"
    # And it is NOT counted as a failure: an empty poll outside the session is the
    # normal state six times in seven, so counting it would cry wolf permanently.
    assert conn.execute(
        "SELECT consecutive_failures FROM job_state").fetchone()[0] == 0


def test_consecutive_failures_counts_consecutively(conn):
    """"Consecutive" has to mean consecutive, or a backoff never recovers.

    The count is what step 6 uses to stop a job retrying against a rate-limited
    endpoint. If a success did not reset it, a job that failed twice in March would
    still be backing off in August.
    """
    record_run(conn, "sync", status="failed", detail="boom")
    record_run(conn, "sync", status="failed", detail="boom again")
    assert conn.execute(
        "SELECT consecutive_failures FROM job_state").fetchone()[0] == 2

    record_run(conn, "sync", status="ok", detail="2 new trades")
    assert conn.execute(
        "SELECT consecutive_failures FROM job_state").fetchone()[0] == 0, (
        "a success must reset the count, or a transient failure backs off forever"
    )


def test_a_nothing_run_also_resets_the_failure_count(conn):
    """`nothing` is a healthy outcome, so it clears a backoff too.

    A calendar fetch that was rate limited records `nothing` -- the feed pushed
    back, nothing was lost, the same week is served later. Treating that as a
    continuing failure would accumulate a backoff for a working system.
    """
    record_run(conn, "market", status="failed")
    record_run(conn, "market", status="nothing", detail="rate limited")
    assert conn.execute(
        "SELECT consecutive_failures FROM job_state").fetchone()[0] == 0


def test_an_unknown_job_name_is_refused(conn):
    """A typo'd job name is invisible, which makes it the dangerous kind.

    It would write rows nothing reads while the real job looks as though it never
    ran -- the exact failure this ledger exists to make visible. A checked set turns
    that into an exception at the call site.
    """
    with pytest.raises(ValueError, match="unknown job"):
        record_run(conn, "bars-live", status="ok")     # hyphen, not underscore
    assert conn.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0] == 0


def test_an_unknown_status_is_refused(conn):
    """The page branches on status, so an unmodelled one renders as nothing at all."""
    with pytest.raises(ValueError, match="unknown status"):
        record_run(conn, "sync", status="succeeded")   # not one of the six


def test_the_known_jobs_include_the_one_that_never_ran(conn):
    """`market` is in the set even though MeshClaw never registered it.

    Verified: `grep -c optjournal-market ~/.meshclaw/crons.json` is 0, so 143 lines
    of tested, documented calendar policy have never run on a schedule -- and
    `market_events` holds one fetch rather than the daily accumulation its docstring
    depends on. Naming it here is the first step of "exists" and "registered"
    becoming one fact.
    """
    assert "market" in KNOWN_JOBS
    record_run(conn, "market", status="ok", detail="99 fetched, 99 stored")
    assert conn.execute(
        "SELECT job FROM job_state").fetchone()["job"] == "market"


def test_a_ledger_failure_does_not_fail_the_work(conn):
    """Bookkeeping must not break the thing it describes.

    A `record_run` that raised would turn a successful `optjournal bars` into a
    non-zero exit, and the cron's delivery policy would then report a failure that
    did not happen -- observability breaking the observed. So a database error is
    logged and swallowed, returning 0.

    Simulated by dropping the table out from under it, which is the bluntest
    possible version of "the write failed".
    """
    conn.execute("DROP TABLE job_runs")
    conn.commit()
    assert record_run(conn, "sync", status="ok") == 0, (
        "a failed ledger write must return 0 rather than raising"
    )


def test_a_failed_ledger_write_does_not_hold_the_write_lock(conn):
    """And it must not wedge the database on the way down.

    sqlite3 does NOT roll back on error, so a swallowed exception can leave the
    connection in a transaction holding the write lock -- measured elsewhere (see
    tests/test_locks.py): the next writer then waits the full BUSY_TIMEOUT_MS,
    15.5s, and fails. Swallowing an error without rolling back trades a loud
    failure for a slow one.

    HONEST ABOUT ITS OWN STRENGTH: this test passes with the rollback removed,
    because the failure it provokes (`DROP TABLE`, which commits) leaves no open
    transaction to leak. It is a REGRESSION guard for the invariant, not proof the
    rollback is load-bearing -- and saying so beats implying an ablation it cannot
    survive. The failure that WOULD need it is a mid-statement IntegrityError, which
    arrives with the runner in step 5; `test_db.test_a_refused_claim_must_be_rolled
    _back` is where that mechanism is pinned today.
    """
    conn.execute("DROP TABLE job_runs")
    conn.commit()
    record_run(conn, "sync", status="ok")
    assert not conn.in_transaction, (
        "the ledger swallowed an error but kept the write lock"
    )


def test_history_is_pruned_but_the_newest_scheduled_run_survives(conn):
    """Pruning must not delete the row a reconciler reads to decide due-ness.

    `job_state` is the anchor and is never pruned, but the ledger keeps its own
    newest scheduled row too, so it stays self-sufficient if the anchor is ever
    rebuilt from it. Without that, a busy job's history could roll over the only
    record that a scheduled instant was claimed -- and the reconciler would either
    replay it or lose the schedule.
    """
    # One scheduled run, then enough manual runs to push it out of the window.
    record_run(conn, "bars_live", status="ok", fired_for=1786310000)
    for _ in range(RUN_HISTORY + 20):
        record_run(conn, "bars_live", status="nothing")

    total = conn.execute(
        "SELECT COUNT(*) FROM job_runs WHERE job = 'bars_live'").fetchone()[0]
    assert total <= RUN_HISTORY + 1, f"history grew unbounded: {total} rows"

    survived = conn.execute(
        "SELECT COUNT(*) FROM job_runs WHERE job = 'bars_live'"
        " AND fired_for = 1786310000").fetchone()[0]
    assert survived == 1, "pruning deleted the newest scheduled run"


def test_pruning_one_job_leaves_another_alone(conn):
    """The prune is per job, so a chatty job cannot evict a quiet one's history."""
    record_run(conn, "sync", status="ok")
    for _ in range(RUN_HISTORY + 5):
        record_run(conn, "bars_live", status="nothing")
    assert conn.execute(
        "SELECT COUNT(*) FROM job_runs WHERE job = 'sync'").fetchone()[0] == 1


def test_prune_is_safe_on_a_job_with_no_runs(conn):
    assert prune_runs(conn, "sync") == 0


# ---------------------------------------------------------------------------
# The audit payload, and the hole in `ok`.
# ---------------------------------------------------------------------------


def _bars_row(conn, *, conid, symbol, bar_size, ts, category="OPT"):
    conn.execute(
        "INSERT OR REPLACE INTO price_bars (conid, symbol, bar_size, ts, close,"
        " source, fetched_at) VALUES (?,?,?,?,1.0,'yahoo','2026-08-09')",
        (conid, symbol, bar_size, ts))
    conn.execute(
        "INSERT OR IGNORE INTO securities (broker, conid, symbol, asset_category)"
        " VALUES ('ibkr', ?, ?, ?)", (conid, symbol, category))


def test_a_total_blackout_is_not_reported_as_ok(conn):
    """THE DEFECT this pair of fields exists for, and it is subtle.

    `SessionAudit.ok` is `not market_traded or not missing`, and `market_traded` is
    answered by "does any UNDERLYING have hourly bars for that day" -- a
    calendar-free holiday oracle. But that oracle fails the SAME WAY as the thing
    it certifies: delete every hourly bar, as a fully dead collector would, and
    `ok` goes GREEN because "no bars for anyone" reads as a market holiday.

    Reproduced on three copies of the real journal before this was written:

        healthy           traded=True   covered=5  missing=0  ok=True
        option poll dead  traded=True   covered=0  missing=5  ok=False  caught
        TOTAL blackout    traded=False  covered=0  missing=0  ok=True   MISSED

    Same watchdog-and-watched-stop-together shape that moved this audit out of a
    cron. So the payload carries `witnesses` (how many contracts were actually
    checked) and `blackout` (nothing traded in the whole lookback), and the page
    reads those instead of `ok`.
    """
    from datetime import UTC, datetime

    from optjournal.serialize import audit_data

    # An empty journal IS a blackout: no underlying has bars for any recent day.
    data = audit_data(conn, now=datetime(2026, 8, 10, 12, tzinfo=UTC))
    assert data["ok"] is True, (
        "premise: ok is green here, which is exactly the problem being fixed"
    )
    assert data["blackout"] is True, "a blackout must be distinguishable from fine"
    assert data["witnesses"] == 0, "nothing was checked, so nothing was proved"


def test_a_healthy_session_is_not_a_blackout(populated_db):
    """The other direction, so `blackout` is not simply always true.

    Uses the REAL archive rather than a hand-built row, and that is not laziness:
    `market_traded_on` derives its underlying set from `_underlying_conids`, which
    reads TRADES, not `securities`. A fixture that inserts a bar and a security row
    but no trade yields an empty conid set and `market_traded=False` -- which is how
    the first version of this test failed, looking like a code bug when it was a
    fixture gap. Building the whole trade-plus-security-plus-bar graph by hand would
    be reimplementing the ingest to test three payload keys.
    """
    from datetime import UTC, datetime

    from optjournal.bars import last_traded_day
    from optjournal.db import connect
    from optjournal.serialize import audit_data

    conn = connect(populated_db)
    # Bars come from `optjournal bars`, which the suite never runs, so seed one
    # hourly underlying bar for a day the real book was open.
    trade = conn.execute(
        "SELECT underlying_conid, underlying_symbol FROM trades"
        " WHERE underlying_conid IS NOT NULL LIMIT 1").fetchone()
    if trade is None:
        pytest.skip("the archive holds no option trade to hang an underlying off")
    session = datetime(2026, 8, 7, 14, 30, tzinfo=UTC)      # 10:30 ET, mid-session
    _bars_row(conn, conid=str(trade["underlying_conid"]),
              symbol=str(trade["underlying_symbol"]), bar_size="1h",
              ts=int(session.timestamp()), category="STK")
    conn.commit()

    # `last_traded_day` walks back from YESTERDAY, so ask on the following day.
    now = session + timedelta(days=1)
    assert last_traded_day(conn, now=now) == "2026-08-07", (
        "the seeded underlying bar should make that day the last traded one"
    )
    data = audit_data(conn, now=now)
    assert data["market_traded"] is True
    assert data["blackout"] is False, (
        "an underlying traded, so the collector is demonstrably alive"
    )
    conn.close()


def test_witnesses_counts_what_was_examined_not_what_passed(conn):
    """`witnesses` is covered PLUS missing, so it does not fall when coverage does.

    That is the point: a count that dropped on failure would be as useless as the
    boolean. It answers "did the audit look at anything", which is a different
    question from "did it like what it saw".
    """
    from datetime import UTC, datetime

    from optjournal.serialize import audit_data

    data = audit_data(conn, now=datetime(2026, 8, 10, 12, tzinfo=UTC))
    assert data["witnesses"] == len(data["covered"]) + len(data["missing"])


def test_a_journal_that_never_collected_is_not_a_stopped_collector(conn, populated_db):
    """The second hole in the blackout signal, and it made the page cry wolf.

    `blackout` says "nothing traded in the whole ten-day lookback", which on a
    working journal means the collector died. On a journal that has NEVER stored a
    bar it means nothing at all -- and the two produce identical payloads. Measured
    rather than reasoned about: `price_bars` emptied out of a copy of the real
    journal, versus a fresh journal, gave byte-identical results.

        collection STOPPED   market_traded=False  witnesses=0  blackout=True
        NEVER collected      market_traded=False  witnesses=0  blackout=True

    So the demo journal -- and any real journal before its first `optjournal bars`
    -- rendered a red collection alarm for the absence of something that was never
    there. `ever_collected` separates them, exactly as `Scheduler.ever_ran`
    separates "the loop is dead" from "no loop has ever run here". That the
    heartbeat already drew this distinction is what makes its absence here an
    oversight rather than a decision.
    """
    from datetime import UTC, datetime

    from optjournal.db import connect
    from optjournal.serialize import audit_data

    now = datetime(2026, 8, 10, 12, tzinfo=UTC)
    fresh = audit_data(conn, now=now)
    assert fresh["blackout"] is True, "premise: an empty journal reads as a blackout"
    assert fresh["ever_collected"] is False, (
        "a journal with no bars at all must not look like a stopped collector"
    )

    # The other direction, on the real archive: a stored bar flips it, and it stays
    # flipped for a bar that is far too old to help the audit -- the field answers
    # "has this ever worked", not "is it working now", which is what makes it safe
    # to gate an alarm on.
    other = connect(populated_db)
    other.execute(
        "INSERT OR REPLACE INTO price_bars (conid, symbol, bar_size, ts, close,"
        " source, fetched_at) VALUES ('1','X','1h',946684800,1.0,'yahoo','2000-01-01')")
    other.commit()
    aged = audit_data(other, now=now)
    assert aged["ever_collected"] is True, (
        "a bar exists, so this journal has demonstrably collected before"
    )
    assert aged["blackout"] is True, (
        "and the blackout still stands -- the two fields answer different questions"
    )
    other.close()


# ---------------------------------------------------------------------------
# The registry (SCHEDULER_PLAN.md step 5a).
#
# THE REGISTRY BEING CODE IS THE POINT. MeshClaw's registration was an
# unversioned, hand-typed side channel, and the consequence is measurable: four
# optjournal jobs are registered and none is the calendar refresh, so 143 lines of
# tested, documented policy have never run on a schedule. Every test below is one
# a `crons.json` could not have.
# ---------------------------------------------------------------------------


def test_the_registry_holds_every_job_the_ledger_accepts():
    """`KNOWN_JOBS` and `JOBS` must name the same four things.

    Two containers for one registry is how "exists" and "registered" drift apart
    again -- a name in the ledger's set with no Job is a status nothing can ever
    write, and a Job whose name the ledger refuses raises at the end of a run that
    already did its work.
    """
    from optjournal.jobs import JOBS

    assert {job.name for job in JOBS} == set(KNOWN_JOBS), (
        "the registry and the ledger's accepted set disagree"
    )


def test_the_calendar_job_is_registered_here_because_it_never_was_anywhere_else():
    """`market` exists in code for the first time.

    Verified rather than assumed: `~/.meshclaw/crons.json` holds four optjournal
    jobs -- daily-sync, bars-live, bars-daily, bars-audit -- and no market job, so
    `market_events` holds a single fetch from the web UI rather than the daily
    accumulation `events.py`'s docstring argues for.
    """
    from optjournal.jobs import job_by_name

    job = job_by_name("market")
    assert job.spends_broker_request is False, (
        "the calendar feed is not IBKR; marking it as spending a broker request "
        "would make the reconciler protect a budget it does not touch"
    )


def test_the_audit_is_not_a_job_any_more():
    """It was `optjournal-bars-audit`; step 4 made it a field on every page load.

    A watchdog that is itself scheduled stops when the thing it watches stops, and
    did: bars-audit read `last_status: ok` for two days while nothing was
    collected. Registering it again would undo that, so its absence is an
    assertion rather than an omission.
    """
    from optjournal.jobs import JOBS

    assert not [job for job in JOBS if "audit" in job.name], (
        "the perishable audit is computed by serialize.audit_data on every page "
        "load -- as a job it cannot notice the outage that stops it too"
    )


def test_only_the_sync_spends_a_broker_request():
    """The flag the reconciler reads before retrying anything.

    Bars and quotes come from a public chart endpoint and the calendar from its own
    feed; only the Flex statement draws on IBKR's rate-limited budget, where the
    penalty is a lockout rather than a slow response. A job wrongly flagged would
    be needlessly throttled; one wrongly unflagged is how a retry loop spends the
    budget.
    """
    from optjournal.jobs import JOBS

    # TWO, since Trade Confirmations: the daily statement and the intraday poll
    # each spend against the same token's lockout allowance. Pinned as a SET rather
    # than loosened to "at least sync", so a third spender still has to be declared
    # here deliberately -- which is the whole point of the flag.
    assert {job.name for job in JOBS if job.spends_broker_request} == {
        "sync", "confirm"}


def test_the_live_poll_is_a_window_and_the_rest_are_wall_clock():
    """`bars_live` is the one job whose due-ness is not an instant.

    Seven cron slots collapse into one predicate -- inside the session AND the last
    success over 55 minutes old -- and it is sound only because the intraday series
    is CUMULATIVE within a session: a 13:00 poll returns every completed bar since
    the open. `Catchup.LATEST` on it would be wrong in the dangerous direction,
    firing outside the session and recording an empty fetch as a success.
    """
    from optjournal.jobs import JOBS, Catchup, job_by_name

    assert job_by_name("bars_live").catchup is Catchup.WINDOW
    assert {j.name for j in JOBS if j.catchup is Catchup.WINDOW} == {
        "bars_live", "confirm"}, (
        "the window jobs are the two intraday ones: bars_live and the Trade "
        "Confirmation poll. Both ask 'is it session time, and is the last success "
        "stale' rather than claiming a wall-clock minute"
    )
    # And nothing is NONE: a job that silently drops a missed instant would have
    # to earn that, and none of the four has.
    assert not [j for j in JOBS if j.catchup is Catchup.NONE]


def test_a_catchup_window_is_bounded_by_the_schedules_own_period():
    """A window may reach back to the previous fire, and no further.

    HONEST ABOUT WHAT THIS DOES NOT GUARD. My first version asserted
    `window_s < 24h` on the theory that a wider window makes two instants due at
    once, and `market` at exactly 24 h failed it. The theory was wrong: `LATEST`
    means the MOST RECENT missed instant only, and replay is prevented by the
    partial unique index on `(job, fired_for)` -- a database constraint, not
    arithmetic. So a wide window cannot spend two IBKR requests on one statement
    however wide it is.

    What a window wider than the period WOULD do is make a job due for an instant
    whose successor has already passed, which for a daily job means running
    yesterday's slot after today's was available. `<=` the period is the honest
    bound, and it is what these four satisfy.
    """
    from optjournal.jobs import JOBS, Catchup

    for job in JOBS:
        if job.catchup is not Catchup.LATEST:
            continue
        # Every LATEST job here is daily on the days it runs at all.
        assert job.window_s <= 24 * 3600, (
            f"{job.name}'s {job.window_s}s window reaches back past the previous "
            "fire, so it could run a slot two schedules old"
        )
        assert job.window_s > 0, f"{job.name} claims catch-up but has no window"


def test_every_schedule_names_a_real_zone_and_a_real_time():
    """A typo'd zone raises at reconcile time, in a thread, on a schedule.

    `ZoneInfo('Europe/Dublín')` is a `ZoneInfoNotFoundError` -- and step 6 resolves
    zones inside the tick loop, where the failure would be a thread dying quietly
    rather than a startup error. Cheap to check here instead.
    """
    from optjournal.jobs import JOBS

    for job in JOBS:
        job.tz()                                   # raises on an unknown zone
        assert 0 <= job.minute <= 59, f"{job.name}: minute {job.minute}"
        assert 0 <= job.hour <= 23, f"{job.name}: hour {job.hour}"
        assert job.weekdays, f"{job.name} runs on no day at all"
        assert set(job.weekdays) <= set(range(1, 8)), (
            f"{job.name}: {job.weekdays} is not ISO weekdays (Monday=1)"
        )
        assert job.timeout_s > 0


def test_the_market_hours_poll_is_scheduled_in_market_time():
    """Eastern, so it follows US DST without being edited twice a year.

    The others are the reader's own zone, where "before breakfast" is the actual
    requirement. Getting this backwards would put the live poll an hour off the
    session for half the year -- which for perishable bars means a lost hour that
    cannot be refetched.
    """
    from optjournal.jobs import job_by_name

    assert job_by_name("bars_live").zone == "America/New_York"
    assert job_by_name("sync").zone == "Europe/Dublin"


def test_the_declaration_order_puts_sync_before_the_bars_it_feeds():
    """Ordering is a real happens-before edge, not a wall-clock guess.

    `bars_daily` derives its manifest from the positions `sync` ingests, so a
    position opened yesterday is only in the manifest once the sync has landed.
    Step 6's worker runs due jobs sequentially down this tuple, which is what
    replaces today's arrangement: two cron expressions 30 minutes apart plus
    MeshClaw's `_compute_jitter`, which returns `random.uniform(0, 59*60)` and was
    observed putting a 538 ms job 25 minutes late.
    """
    from optjournal.jobs import JOBS

    order = [job.name for job in JOBS]
    assert order.index("sync") < order.index("bars_daily"), (
        "bars_daily would derive its manifest from positions the sync has not "
        "ingested yet"
    )


def test_an_unknown_job_name_raises_its_own_type():
    """So the endpoint can answer 400 rather than 500."""
    from optjournal.jobs import UnknownJob, job_by_name

    with pytest.raises(UnknownJob):
        job_by_name("bars-live")                   # hyphen, not underscore


# ---------------------------------------------------------------------------
# The runner (SCHEDULER_PLAN.md step 5a), and the two hazards the plan measured.
# ---------------------------------------------------------------------------


@pytest.fixture()
def ctx(tmp_path):
    """A Context whose archive is a scratch directory, never the real `raw/`."""
    from optjournal.jobs import Context

    return Context(archive_dir=tmp_path / "raw", db_path=tmp_path / "j.db",
                   query_id="1591754")


def _stub(monkeypatch, name, outcome):
    """Point one registry entry's `run` at a stub, leaving the rest alone.

    Patches the Job's frozen field through `object.__setattr__` on a COPY placed
    into a replacement tuple, so the real registry is restored by monkeypatch and
    a test cannot leak a stub into another.
    """
    import dataclasses

    from optjournal import jobs as mod

    replaced = tuple(
        dataclasses.replace(job, run=outcome) if job.name == name else job
        for job in mod.JOBS
    )
    monkeypatch.setattr(mod, "JOBS", replaced)


def test_the_claim_row_is_committed_before_the_work_starts(conn, ctx, monkeypatch):
    """THE ordering decision in the runner, and the review refuted the alternative.

    If the row were written on a terminal state instead, the window between "is
    this slot claimed?" and "this slot is claimed" would span the whole job. A
    SIGKILL mid-fetch -- which launchd `KeepAlive` makes routine, ~10s respawn --
    then leaves no row AND no stamp, because `flex._record_fetch` runs only after a
    successful download, so the next reconcile finds the slot unclaimed and spends
    a SECOND IBKR request. Deterministically, with no concurrency involved.

    Asserted from INSIDE the work: the job's own `run` reads the database through a
    SEPARATE connection, which can only see a committed row. That is what makes
    this a test of the commit rather than of the insert.
    """
    from optjournal.db import connect
    from optjournal.jobs import Outcome, run_job

    seen = {}

    def work(_conn, _ctx):
        other = connect(ctx.db_path)
        row = other.execute(
            "SELECT status, finished_at FROM job_runs WHERE job = 'market'"
        ).fetchone()
        seen["row"] = None if row is None else dict(row)
        other.close()
        return Outcome("ok", "did the thing")

    _stub(monkeypatch, "market", work)
    run_id = run_job(conn, "market", ctx=ctx)

    assert seen["row"] is not None, (
        "another connection could not see the claim, so it was not committed "
        "before the work began -- a killed run would leave the slot unclaimed"
    )
    assert seen["row"]["status"] == "running"
    assert seen["row"]["finished_at"] is None, (
        "the claim was stamped finished before the work ran"
    )
    final = conn.execute(
        "SELECT status, detail, finished_at FROM job_runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert (final["status"], final["detail"]) == ("ok", "did the thing")
    assert final["finished_at"], "the terminal state was never stamped"


def test_a_second_runner_is_refused_rather_than_queued(conn, ctx, monkeypatch):
    """`JobBusy`, not a wait. Two concurrent syncs would each spend a request.

    The lock is taken with `timeout_s=0` deliberately: every job here is idempotent
    and cheap to retry on the next tick, so refusing is better than queueing behind
    something that spends IBKR requests. The 409 names the run to watch.

    THE ELAPSED TIME IS PART OF THE ASSERTION, and it was added because the first
    version of this test could not see the difference. `timeout_s=1` instead of 0
    still raises `JobBusy` -- one second later -- so the ablation passed while the
    behaviour was wrong. A refusal that takes a second is not a refusal, it is a
    queue with a short patience: the browser holds a connection, and the `sync`
    job's timeout is 900s.
    """
    import time

    from optjournal.db import connect
    from optjournal.jobs import JobBusy, Outcome, run_job

    def reentrant(_conn, _ctx):
        # A second runner, in this process, on its own connection -- the same
        # situation as the page pressing Run twice.
        other = connect(ctx.db_path)
        try:
            started = time.monotonic()
            with pytest.raises(JobBusy) as caught:
                run_job(other, "market", ctx=ctx)
            waited = time.monotonic() - started
            assert caught.value.run_id is not None, (
                "a 409 must name the run already in flight, or the page has "
                "nothing to poll"
            )
            # 100ms is two `locks._POLL_S` intervals: generous enough not to be
            # flaky under load, tight enough that any real wait fails it.
            assert waited < 0.1, (
                f"the second runner WAITED {waited:.2f}s before being refused, so "
                "the lock is blocking -- callers queue behind a job that can take "
                "900s instead of getting an immediate 409"
            )
        finally:
            other.close()
        return Outcome("ok")

    _stub(monkeypatch, "market", reentrant)
    run_job(conn, "market", ctx=ctx)
    assert conn.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0] == 1, (
        "the refused runner still wrote a row"
    )


def test_a_refused_claim_is_rolled_back_and_does_not_wedge_the_database(conn, ctx):
    """(a) FROM THE PLAN, MEASURED: sqlite3 does NOT roll back on IntegrityError.

    Losing the race for a `fired_for` is a DESIGNED outcome -- the partial unique
    index is what makes catch-up idempotent -- so the loser must not leave the
    connection in a transaction holding the write lock. Measured cost if it does:

        refused: UNIQUE constraint failed: job_runs.job, job_runs.fired_for
        in_transaction AFTER refusal: True        <-- write lock retained
        other writer FAILED after 15.55s: database is locked
        after rollback() other writer OK in 0.000s

    So the scheduler would wedge its own database by losing a race it exists to
    lose, and every other writer -- including the heartbeat -- would wait the full
    BUSY_TIMEOUT_MS and fail.
    """
    from optjournal.jobs import JobBusy, run_job

    instant = 1786310000
    conn.execute(
        "INSERT INTO job_runs (job, fired_for, started_at, status)"
        " VALUES ('market', ?, '2026-08-09T11:00:00+00:00', 'running')", (instant,))
    conn.commit()

    with pytest.raises(JobBusy):
        run_job(conn, "market", ctx=ctx, fired_for=instant)
    assert not conn.in_transaction, (
        "the refused claim left the write lock held; the next writer waits "
        "BUSY_TIMEOUT_MS (15.5s measured) and then fails"
    )
    # And a real second writer proves it, rather than trusting the flag.
    from optjournal.db import connect
    other = connect(ctx.db_path)
    other.execute("INSERT OR IGNORE INTO watchlist (symbol, note, added_at)"
                  " VALUES ('ZZZ', NULL, '2026-08-09')")
    other.commit()
    other.close()


def test_a_job_that_raises_is_recorded_failed_and_the_error_still_travels(
    conn, ctx, monkeypatch
):
    """Both halves, and the second is the one the keychain failure needed.

    Recording `failed` without re-raising is how the 2026-08-07 outage became a
    message that reached nobody: a `KeyringLocked` is not `flex.TokenMissing`, so
    it never mapped to exit 2, and a runner that swallowed it would leave the
    caller believing the run merely returned nothing.
    """
    from optjournal.jobs import run_job

    def boom(_conn, _ctx):
        raise RuntimeError("the keychain is locked")

    _stub(monkeypatch, "market", boom)
    with pytest.raises(RuntimeError, match="keychain"):
        run_job(conn, "market", ctx=ctx)

    row = conn.execute("SELECT status, detail FROM job_runs").fetchone()
    assert row["status"] == "failed"
    assert "RuntimeError" in row["detail"] and "keychain" in row["detail"], (
        "the ledger must record the CAUSE, not just that something failed"
    )
    assert conn.execute(
        "SELECT consecutive_failures FROM job_state").fetchone()[0] == 1


def test_an_interrupted_run_is_resolved_by_the_kernel_not_by_a_timeout(conn, ctx):
    """(b) A `running` row whose file lock is free had its process killed.

    OS locks release on process death, including a forced kill, so this needs no PID, no
    heartbeat and no staleness threshold -- and it is correct across laptop sleep,
    where every wall-clock rule is wrong: this machine measured 44.6 hours of sleep
    excluded from `monotonic`.
    """
    from optjournal.jobs import interrupted_runs

    conn.execute(
        "INSERT INTO job_runs (job, started_at, status)"
        " VALUES ('bars_live', '2026-08-09T14:00:00+00:00', 'running')")
    conn.commit()

    assert interrupted_runs(conn, archive_dir=ctx.archive_dir) == 1
    row = conn.execute("SELECT status, finished_at, detail FROM job_runs").fetchone()
    assert row["status"] == "interrupted"
    assert row["finished_at"], "an interrupted run must be stamped finished"
    assert row["detail"], "and must say why, or it reads as a mystery"


def test_a_run_that_is_genuinely_in_flight_keeps_its_row(conn, ctx, monkeypatch):
    """The other direction, or every live run would be declared dead on page load.

    Probed from inside the work, while the runner holds the lock -- which is
    exactly when a page load happens.
    """
    from optjournal.jobs import Outcome, interrupted_runs, run_job

    seen = {}

    def work(inner_conn, _ctx):
        seen["resolved"] = interrupted_runs(inner_conn, archive_dir=ctx.archive_dir)
        seen["status"] = inner_conn.execute(
            "SELECT status FROM job_runs").fetchone()["status"]
        return Outcome("ok")

    _stub(monkeypatch, "market", work)
    run_job(conn, "market", ctx=ctx)
    assert seen["resolved"] == 0, (
        "a running job's row was resolved as interrupted while it was still "
        "holding its lock"
    )
    assert seen["status"] == "running"


def test_resolving_interrupted_runs_is_safe_with_no_rows(conn, ctx):
    """The common case: nothing running, nothing to do, no lock files needed."""
    from optjournal.jobs import interrupted_runs

    assert interrupted_runs(conn, archive_dir=ctx.archive_dir) == 0


@pytest.mark.parametrize(("changed", "status"), [(True, "ok"), (False, "nothing")])
def test_the_sync_job_reads_the_result_it_was_handed(conn, ctx, monkeypatch,
                                                     changed, status):
    """Every key `_sync` reads off `sync_journal`'s reply, exercised.

    THE GAP THIS CLOSES was measured, not guessed: renaming any of `changed`,
    `summary` or `new_trades` in the CONSUMER -- a plain typo -- passed the whole
    suite. The neighbouring test reads `_sync`'s SOURCE for what it must not call,
    which is the right shape for a negative obligation and blind to this: a
    source-text assertion cannot tell `result["summary"]` from `result["summry"]`.

    Running it costs nothing because the shared path is stubbed. That is the only
    reason this can exist as an executed test rather than another source read --
    a real sync spends an IBKR request against a lockout budget, which is why the
    job's own translation had never been run under test.

    Both branches, because `changed` decides `ok` versus `nothing` and that
    distinction is the one this ledger exists for: an empty run is not a success,
    and collapsing the two is what let three crons report health while collecting
    nothing.
    """
    from optjournal import jobs as mod

    reply = {
        "changed": changed,
        "summary": "3 new trade(s), 0 new cash row(s)",
        "new_trades": 3,
        # Present because the real reply carries them; unread here, and a
        # dataclass would not change that -- see the README on why this stayed a
        # dict.
        "new_trade_rows": [{"symbol": "SPY"}],
        "warnings": [],
    }
    monkeypatch.setattr(mod, "sync_journal", lambda **_: reply)

    outcome = mod._sync(conn, ctx)
    assert outcome.status == status, (
        f"changed={changed} must record {status!r}: an empty sync is not a "
        "success and a productive one is not silence"
    )
    assert outcome.detail == reply["summary"], (
        "the ledger shows the shared path's own summary, so a reader sees the "
        "same sentence the CLI and the page do"
    )
    assert (outcome.done, outcome.total) == (3, 3)


def test_the_sync_job_calls_the_shared_path_rather_than_reimplementing_it(ctx):
    """THE NEGATIVE OBLIGATION from the plan, and it needs an assertion.

    `flex.fetch` owns the cooldown, holds the fetch file lock and stamps
    `.fetch-state.json`. A job that reimplemented that sequence would be a second
    thing to keep in step with a lockout budget -- and this project has already
    paid for two implementations of sync drifting apart (`new_trades` was a COUNT
    in one and a row LIST in the other).

    Source-level because producing a real sync spends an IBKR request.
    """
    import inspect

    from optjournal import jobs as mod

    src = inspect.getsource(mod._sync)
    assert "sync_journal(" in src, (
        "the sync job no longer calls the shared path, so the cooldown, the fetch "
        "lock and the snapshot are now its own problem"
    )
    for forbidden in ("fetch(", "ingest_file(", "_record_fetch", "cooldown_s"):
        assert forbidden not in src, (
            f"the sync job reaches for {forbidden!r} directly, which is the "
            "second implementation this consolidation removed"
        )


def test_a_sync_with_no_credentials_reports_rather_than_raising(conn, tmp_path):
    """A journal serving an ingested archive with no query id is a supported state.

    It must record `failed` with a cause the page can show, not raise past the
    ledger -- otherwise the one job that spends an IBKR request is also the one
    whose misconfiguration leaves no trace.
    """
    from optjournal.jobs import Context, run_job

    bare = Context(archive_dir=tmp_path / "raw", db_path=tmp_path / "j.db")
    assert bare.query_id is None
    run_job(conn, "sync", ctx=bare)
    row = conn.execute("SELECT status, detail FROM job_runs").fetchone()
    assert row["status"] == "failed"
    assert "query id" in row["detail"]


def test_every_registered_job_is_in_the_payload_even_if_it_never_ran(conn):
    """THE gap that made the Run button useless for the job that needed it most.

    `jobs_data` read `job_state`, so a job with no row did not appear -- no row on
    the page, no button, no way to start it. That is the `crons.json` failure
    wearing a new hat: `market` has never run anywhere, so it would have been
    invisible on the one surface built to make it runnable. The registry says what
    EXISTS; the table only says what has HAPPENED.
    """
    from datetime import UTC, datetime

    from optjournal.jobs import JOBS
    from optjournal.serialize import jobs_data

    data = jobs_data(conn, now=datetime.now(UTC))          # a journal with no runs
    assert conn.execute("SELECT COUNT(*) FROM job_state").fetchone()[0] == 0, (
        "premise: nothing has ever run in this journal"
    )
    assert [row["job"] for row in data["jobs"]] == [job.name for job in JOBS], (
        "the payload omits registered jobs that have never run, so the page cannot "
        "offer a button for them"
    )
    for row in data["jobs"]:
        assert row["last_status"] is None, "a job that never ran has no status"
        assert row["consecutive_failures"] == 0
        assert row["last_run"] is None


def test_the_payload_says_which_jobs_spend_a_broker_request(conn):
    """So the page's confirm() reads the registry rather than holding a copy.

    A page-side list of which jobs touch IBKR is a second copy of a fact, and a
    second copy drifts -- the same reason "USD high-impact" is decided server-side
    for the calendar. Get this wrong in the permissive direction and a dialogue
    stops appearing on the one run that spends a rate-limited request.
    """
    from datetime import UTC, datetime

    from optjournal.serialize import jobs_data

    spends = {row["job"]: row["spends_request"]
              for row in jobs_data(conn, now=datetime.now(UTC))["jobs"]}
    assert spends["sync"] is True
    assert spends["confirm"] is True, (
        "the Trade Confirmation poll spends a request too, and the page has to say "
        "so -- it is the one that fires every half hour"
    )
    assert not any(v for k, v in spends.items() if k not in ("sync", "confirm")), (
        f"a job other than sync claims to spend a broker request: {spends}"
    )


def test_a_job_that_left_the_registry_keeps_its_history_and_is_marked(conn):
    """`bars_audit` is the real case: it WAS a cron and is now a page-load field.

    Its rows are still in this journal. Dropping them from the payload would make
    the panel that reports on collection health silently forget that the audit ever
    ran, and a row that vanishes reads as "this never happened". So the history is
    kept, flagged `retired`, and offered no button -- pressing one would post a name
    the registry no longer knows, which the endpoint answers 400 to.
    """
    from datetime import UTC, datetime

    from optjournal.serialize import jobs_data

    conn.execute(
        "INSERT INTO job_state (job, last_status, consecutive_failures)"
        " VALUES ('bars_audit', 'ok', 0)")
    conn.commit()

    rows = {row["job"]: row for row in jobs_data(conn, now=datetime.now(UTC))["jobs"]}
    assert "bars_audit" in rows, "a retired job's history vanished from the payload"
    assert rows["bars_audit"]["retired"] is True
    assert rows["bars_audit"]["spends_request"] is False
    # And a registered job never carries the flag, or every row would render as
    # unrunnable.
    assert "retired" not in rows["sync"]


# ---------------------------------------------------------------------------
# Due-ness (SCHEDULER_PLAN.md step 6a).
#
# `due_jobs` is pure, so the whole catch-up and sleep policy is tested by passing a
# clock rather than by waiting for one. That is the point of the shape: a scheduler
# whose rules can only be observed by living through them is a scheduler nobody can
# change safely.
#
# THE STAKES, both directions. Too permissive spends real IBKR requests against a
# hard lockout budget. Too strict silently converts "missed, unrecoverable" into
# nothing at all, because an option's intraday series exists only while its own
# session runs. So the boundary tests here are not optional garnish.
# ---------------------------------------------------------------------------

_ET = "America/New_York"


def _due(now, **kw):
    """`due_jobs` with empty ledgers unless a test says otherwise.

    `ever_ran` defaults to EVERY job, because the empty-ledger rule is so
    aggressive (nothing is ever due) that a test forgetting it would pass
    vacuously -- it would assert "not due" against a function that returns nothing
    for any input. The one test that wants the rule asks for it explicitly.
    """
    from optjournal.jobs import JOBS, due_jobs

    kw.setdefault("claimed", {})
    kw.setdefault("last_success", {})
    kw.setdefault("ever_ran", {job.name for job in JOBS})
    return due_jobs(now, **kw)


def _names(dues):
    return sorted(d.job.name for d in dues)


def test_an_empty_ledger_means_unknown_not_overdue():
    """THE SHARPEST FOOT-GUN IN THE STEP, and it is about recovery.

    `job_runs` lives in `journal.db`, which a `raw/` restore rebuilds from nothing,
    so a rebuilt journal has no recorded runs at all. Read as "everything is
    overdue", the first reconcile after a restore spends an IBKR request plus 24 bar
    requests -- unprompted, on a machine whose owner was already recovering from
    something.

    So a job with no history waits for its next natural slot. Asserted with the
    clock sitting AFTER every job's daily instant, which is exactly when the naive
    rule would fire all of them.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # A Wednesday, 22:00 Dublin = 17:00 ET: past sync (12:00), bars_daily (12:30)
    # and market (11:00), and AFTER the US close so `bars_live` is out of the
    # picture. 18:00 Dublin would be 13:00 ET -- mid-session -- and the first
    # version of this test used it and failed on a live poll that was correctly
    # due. See `test_the_live_poll_ignores_the_empty_ledger_rule` for why that is
    # right rather than an exception to be suppressed.
    now = datetime(2026, 8, 12, 22, 0, tzinfo=ZoneInfo("Europe/Dublin"))
    assert _names(_due(now, ever_ran=set())) == [], (
        "a journal with no recorded runs treated every job as overdue -- a restore "
        "would spend an IBKR request and 24 bar requests unprompted"
    )
    # And the control: with history, the same clock IS due. Without this the test
    # above passes against a function that returns nothing for every input.
    assert "sync" in _names(_due(now)), (
        "the fixture cannot distinguish the empty-ledger rule from a dead function"
    )


def test_a_claimed_instant_is_not_due_again():
    """Idempotency is a database constraint, and this is the cheap check before it.

    The partial unique index on `(job, fired_for)` is the real guard -- two
    reconcilers racing cannot both claim an instant -- but re-deriving due-ness on
    every tick means a job already run would otherwise be attempted 60 times an
    hour, each attempt losing the race. `sync` losing that race 60 times is 60
    refused claims; winning it once too often is a spent IBKR request.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # After the US close, so only instant-claiming jobs are in play: a WINDOW job
    # claims no instant at all and is braked by `last_success` instead.
    now = datetime(2026, 8, 12, 22, 0, tzinfo=ZoneInfo("Europe/Dublin"))
    first = _due(now)
    assert first, "premise: something is due at this clock"
    claimed = {d.job.name: {d.fired_for} for d in first if d.fired_for is not None}
    assert _names(_due(now, claimed=claimed)) == [], (
        "an instant already recorded came back as due, so every tick would "
        "re-attempt work that has already happened"
    )


def test_due_ness_keys_on_recorded_not_on_succeeded():
    """A FAILED run must not make the job due again on the next tick.

    Overruling the tempting rule ("no row with status ok"). A sync that fails for a
    real reason -- the locked keychain that actually happened on 2026-08-07 -- would
    then be due again 60 seconds later and stay due for its whole 12-hour window,
    leaving the 900s fetch cooldown as the only brake: roughly 48 real IBKR requests
    in twelve hours against a budget whose penalty is a lockout.

    `consecutive_failures` on `job_state` is what a human reads instead, and the
    page renders it.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 8, 12, 22, 0, tzinfo=ZoneInfo("Europe/Dublin"))
    instant = next(d.fired_for for d in _due(now) if d.job.name == "sync")
    # Recorded as FAILED -- `claimed` carries the instant regardless of outcome.
    assert "sync" not in _names(_due(now, claimed={"sync": {instant}})), (
        "a failed run left the job due again, which turns one failure into a "
        "retry loop against a rate-limited endpoint"
    )


def test_a_job_too_far_behind_is_not_caught_up():
    """The catch-up window is a bound, not a suggestion.

    `sync`'s window is 12 h: a missed noon is worth running at 18:00, because the
    docstring records a badly-timed sync missing Monday's fills twice. It is NOT
    worth running at 04:00 the next morning against a statement that the next noon
    run will fetch anyway.
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from optjournal.jobs import job_by_name

    job = job_by_name("sync")
    noon = datetime(2026, 8, 12, 12, 0, tzinfo=ZoneInfo("Europe/Dublin"))
    inside = noon + timedelta(seconds=job.window_s - 60)
    outside = noon + timedelta(seconds=job.window_s + 60)
    assert "sync" in _names(_due(inside)), "a job inside its window is not due"
    assert "sync" not in _names(_due(outside)), (
        "a job past its catch-up window was still caught up"
    )


def test_the_live_poll_is_due_inside_the_session_and_never_outside_it():
    """Both halves, and the second is the one that protects the audit's meaning.

    Outside the window `bars_live` must be NOT DUE rather than due-and-empty: a
    20:00 wake that fetches nothing and records `ok` is the exact inversion the
    perishable audit exists to catch. An empty poll recorded as success is how three
    cron jobs reported health for two days.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    et = ZoneInfo(_ET)
    mid = datetime(2026, 8, 12, 13, 0, tzinfo=et)          # Wednesday, mid-session
    assert "bars_live" in _names(_due(mid))
    for label, when in (
        ("pre-market", datetime(2026, 8, 12, 8, 0, tzinfo=et)),
        ("after the close", datetime(2026, 8, 12, 20, 0, tzinfo=et)),
        ("Saturday", datetime(2026, 8, 15, 13, 0, tzinfo=et)),
        ("Sunday", datetime(2026, 8, 16, 13, 0, tzinfo=et)),
    ):
        assert "bars_live" not in _names(_due(when)), (
            f"the live poll is due {label}, so it would fetch nothing and record "
            "a success -- which is what the audit exists to catch"
        )


def test_the_live_poll_waits_out_its_window_after_a_success():
    """Cumulative within a session, so one poll an hour is enough.

    A 13:00 poll returns every completed bar since the open, which is what lets
    seven cron slots collapse into one predicate -- and what makes polling again
    four minutes later pure waste.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from optjournal.jobs import job_by_name

    et = ZoneInfo(_ET)
    now = datetime(2026, 8, 12, 13, 0, tzinfo=et)
    window = job_by_name("bars_live").window_s
    fresh = int(now.timestamp()) - (window - 60)
    stale = int(now.timestamp()) - (window + 60)
    assert "bars_live" not in _names(_due(now, last_success={"bars_live": fresh})), (
        "the live poll fired again inside its own window"
    )
    assert "bars_live" in _names(_due(now, last_success={"bars_live": stale})), (
        "the live poll stopped firing once its window had elapsed"
    )


def test_the_live_poll_claims_no_instant():
    """`fired_for` is None for a WINDOW job, and that is load-bearing.

    The run stands for "this session, at whatever moment we woke", not for a
    scheduled minute. The partial unique index accepts repeated NULLs, which is
    exactly what lets one session be polled several times -- while a `LATEST` job's
    instant is constrained to once.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 8, 12, 13, 0, tzinfo=ZoneInfo(_ET))
    from optjournal.jobs import job_by_name

    stale = int(now.timestamp()) - (job_by_name("bars_live").window_s + 60)
    # BOTH branches of `_window_due`: no success recorded yet, and a success old
    # enough to have expired. An ablation that made only the second claim an
    # instant survived a version of this test that exercised the first alone.
    for label, ledger in (("no success yet", {}),
                          ("an expired success", {"bars_live": stale})):
        live = next(d for d in _due(now, last_success=ledger)
                    if d.job.name == "bars_live")
        assert live.fired_for is None, (
            f"with {label} the live poll claims a scheduled instant, so the unique "
            "index would let it run once per session instead of once per window"
        )


def test_the_repeated_hour_at_the_dst_fall_back_cannot_fire_twice():
    """01:30 happens twice on the fall-back day, and it must claim ONE instant.

    Not hypothetical arithmetic: if the two occurrences produced different
    `fired_for` values, a job scheduled in that hour would run twice -- and for
    `sync` that is two IBKR requests fetching the same statement.

    Ireland falls back at 02:00 on the last Sunday of October, so 2026-10-25.
    Checked on a job placed INSIDE the repeated hour rather than on the real
    registry, because no job here is scheduled at 01:30 today and the guard has to
    survive one being added.
    """
    import dataclasses
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from optjournal.jobs import due_jobs, job_by_name

    dublin = ZoneInfo("Europe/Dublin")
    nightly = dataclasses.replace(
        job_by_name("market"), name="market",
        minute=30, hour=1, weekdays=(1, 2, 3, 4, 5, 6, 7), zone="Europe/Dublin",
    )
    # The same wall-clock time, before and after the fold.
    early = datetime(2026, 10, 25, 1, 30, tzinfo=dublin, fold=0)
    late = datetime(2026, 10, 25, 1, 30, tzinfo=dublin, fold=1)
    assert int(early.timestamp()) != int(late.timestamp()), (
        "premise: this really is a repeated hour on this platform"
    )

    stamps = set()
    for probe in (early, late, datetime(2026, 10, 25, 3, 0, tzinfo=dublin)):
        found = due_jobs(probe, claimed={}, last_success={},
                         ever_ran={"market"}, registry=(nightly,))
        stamps |= {d.fired_for for d in found}
    assert len(stamps) == 1, (
        f"the repeated 01:30 produced {len(stamps)} distinct instants ({stamps}), "
        "so a job scheduled in it would fire twice -- two IBKR requests for one "
        "statement"
    )


def test_a_schedule_in_the_missing_spring_forward_hour_still_runs():
    """02:30 does not exist on the spring-forward day. It must not be SKIPPED.

    Ireland springs forward at 01:00 on the last Sunday of March, so 2026-03-29 has
    no 01:30. `ZoneInfo` normalises a nonexistent local time rather than raising, so
    the job fires late that once -- which is the right trade: running an hour late
    one day a year beats silently missing a day, and a missed session cannot be
    recollected at any price.
    """
    import dataclasses
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from optjournal.jobs import due_jobs, job_by_name

    dublin = ZoneInfo("Europe/Dublin")
    nightly = dataclasses.replace(
        job_by_name("market"), name="market",
        minute=30, hour=1, weekdays=(1, 2, 3, 4, 5, 6, 7), zone="Europe/Dublin",
    )
    # Mid-morning on the spring-forward day: the 01:30 slot is behind us.
    found = due_jobs(datetime(2026, 3, 29, 9, 0, tzinfo=dublin),
                     claimed={}, last_success={}, ever_ran={"market"},
                     registry=(nightly,))
    assert found, (
        "a schedule inside the missing hour produced no due instant, so that day "
        "is silently skipped"
    )
    from datetime import UTC
    fired = datetime.fromtimestamp(found[0].fired_for, UTC).astimezone(dublin)
    assert fired.date().isoformat() == "2026-03-29", (
        f"the instant landed on {fired.date()}, not the day it was scheduled for"
    )


def test_the_market_hours_poll_follows_us_dst_not_the_readers_clock():
    """Which is why `bars_live` is scheduled in Eastern.

    In March the US springs forward two weeks before Europe does, so for a fortnight
    the offset between them is one hour smaller. A poll scheduled in the reader's
    zone would sit an hour off the session for those two weeks -- and for perishable
    bars an hour off is an hour lost.

    Asserted at 09:45 ET on a day inside that fortnight: in-session in market time,
    while the same wall-clock hour in Dublin is before the open.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # 2026-03-16 is a Monday between the US (Mar 8) and EU (Mar 29) transitions.
    inside = datetime(2026, 3, 16, 9, 45, tzinfo=ZoneInfo(_ET))
    assert "bars_live" in _names(_due(inside)), (
        "the live poll is not due at 09:45 ET during the US/EU DST gap, so it "
        "would miss the first hour of the session for two weeks a year"
    )
    assert inside.utcoffset().total_seconds() == -4 * 3600, (
        "premise: the US has already sprung forward on this date"
    )

    # THE DISCRIMINATOR, and the test needed it: an ablation reading the session
    # hours in Europe/Dublin instead of America/New_York SURVIVED the assertion
    # above, because 09:45 ET is 13:45 Dublin and both land inside a 09:30-16:10
    # window. 15:45 ET is 20:45 Dublin -- in-session in market time and far outside
    # it in the reader's -- so only this instant can tell the two apart.
    late = datetime(2026, 8, 12, 15, 45, tzinfo=ZoneInfo(_ET))
    assert late.astimezone(ZoneInfo("Europe/Dublin")).hour == 20, (
        "premise: this instant is inside the US session and outside a same-clock "
        "window in the reader's zone"
    )
    assert "bars_live" in _names(_due(late)), (
        "the live poll is not due at 15:45 ET, so the session window is being read "
        "in the wrong zone -- the last half hour of every session would be lost, "
        "and those bars cannot be recollected at any price"
    )


def test_due_jobs_returns_registry_order_so_sync_precedes_the_bars_it_feeds():
    """Ordering is a happens-before edge, and the caller must not sort it away.

    `bars_daily` derives its manifest from the positions `sync` ingests, so a
    position opened yesterday is only in the manifest once the sync has landed.
    Today that ordering is two cron expressions 30 minutes apart plus MeshClaw's
    `_compute_jitter`, which returns `random.uniform(0, 59*60)` and was observed
    putting a 538 ms job 25 minutes late.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from optjournal.jobs import JOBS

    now = datetime(2026, 8, 12, 22, 0, tzinfo=ZoneInfo("Europe/Dublin"))
    fired = [d.job.name for d in _due(now)]
    expected = [job.name for job in JOBS if job.name in fired]
    assert fired == expected, (
        f"due_jobs returned {fired}, not registry order {expected} -- bars_daily "
        "could run before the sync that feeds its manifest"
    )


def test_every_due_job_says_why():
    """A scheduler that fires without saying why is what this plan replaces.

    `crons.json` recorded `last_status: ok` and nothing about which instant a run
    stood for, which is how three jobs looked healthy for two days. The reason
    string goes into the log beside the claim.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    for now in (datetime(2026, 8, 12, 18, 0, tzinfo=ZoneInfo("Europe/Dublin")),
                datetime(2026, 8, 12, 13, 0, tzinfo=ZoneInfo(_ET))):
        for found in _due(now):
            assert found.reason and len(found.reason) > 10, (
                f"{found.job.name} is due with no explanation: {found.reason!r}"
            )


def test_the_live_poll_ignores_the_empty_ledger_rule_and_that_is_correct():
    """A WINDOW job is exempt from "empty ledger means unknown", deliberately.

    The rule protects a RESTORE from catch-up: a rebuilt journal must not replay a
    scheduled instant and spend an IBKR request unprompted. `bars_live` catches up
    on nothing -- it asks "is the market open right now, and are my bars stale?" --
    so there is no missed instant to replay, and the answer on a rebuilt journal at
    13:00 ET is genuinely YES: that session's intraday bars are being lost while the
    question is asked, and they cannot be recollected at any price.

    It is also the cheapest job to be wrong about: a public chart endpoint, no IBKR
    request, and an empty poll records `nothing` rather than a failure.

    Worth its own test because it looks like a hole in the rule. Two tests above had
    to change their clock because of it, which is precisely when an exemption should
    be written down rather than left as behaviour someone rediscovers.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    mid = datetime(2026, 8, 12, 13, 0, tzinfo=ZoneInfo(_ET))
    assert "bars_live" in _names(_due(mid, ever_ran=set())), (
        "the live poll waits for history on a rebuilt journal, so a restore during "
        "market hours silently loses that session's perishable bars"
    )
    # And the instant-claiming jobs at the SAME clock are still held back.
    # Sorted by `_names`, so this is a SET claim rather than an execution order.
    # The Trade Confirmation poll shares the exemption and the reasoning: a rebuilt
    # journal in market hours should collect today's fills rather than wait, and the
    # statement supersedes whatever it collects. Everything that claims a
    # wall-clock instant is still held back.
    assert _names(_due(mid, ever_ran=set())) == ["bars_live", "confirm"], (
        "a job that catches up fired on an empty ledger"
    )


def test_a_claimed_instant_does_not_brake_the_live_poll():
    """The other half of the same exemption, and the reason it is safe.

    `claimed` cannot hold a WINDOW job's instants because it has none, so if the
    poll were braked by `claimed` it would be braked by nothing at all -- one poll
    per session instead of one per window. Its brake is `last_success` plus
    `window_s`, which `test_the_live_poll_waits_out_its_window_after_a_success`
    pins from the other side.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    mid = datetime(2026, 8, 12, 13, 0, tzinfo=ZoneInfo(_ET))
    # A claim that could not have come from this job, plus a NULL-ish key: neither
    # may hide the poll.
    assert "bars_live" in _names(_due(mid, claimed={"bars_live": {0}})), (
        "the live poll is gated on claimed instants, which it never has -- it "
        "would poll once per session instead of once per window"
    )


# ---------------------------------------------------------------------------
# The reconciler thread (SCHEDULER_PLAN.md step 6b).
#
# Every test here drives a SCRATCH journal and a stubbed registry. Nothing in this
# file may reach a network: the tick's whole job is to start work, and the work is
# what spends IBKR requests.
# ---------------------------------------------------------------------------


def _registry(monkeypatch, *names, outcome="ok"):
    """Replace JOBS with stubs of the named jobs, keeping their schedules.

    The SCHEDULES are real -- the point is to exercise due-ness against the actual
    zones and hours -- while the work is a stub, so a tick cannot reach a feed.
    """
    import dataclasses

    from optjournal import jobs as mod

    def stub(_c, _x, n=""):
        return mod.Outcome(outcome, f"stubbed {n}", 1, 1)

    kept = tuple(
        dataclasses.replace(job, run=lambda c, x, n=job.name: stub(c, x, n))
        for job in mod.JOBS if job.name in names
    )
    monkeypatch.setattr(mod, "JOBS", kept)
    return kept


def test_a_tick_runs_what_is_due_and_records_it(conn, ctx, monkeypatch):
    """The whole loop in one call, with the clock passed in.

    `reconcile` takes `now` for the same reason `due_jobs` is pure: the tick's
    behaviour at a particular instant is testable without waiting for that instant
    to arrive.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from optjournal.jobs import reconcile

    _registry(monkeypatch, "market")
    # `market` fires at 11:00 Dublin; ask at 12:00 with history present.
    conn.execute("INSERT INTO job_runs (job, started_at, status) VALUES"
                 " ('market', '2026-08-11T11:00:00+00:00', 'ok')")
    conn.commit()
    now = datetime(2026, 8, 12, 12, 0, tzinfo=ZoneInfo("Europe/Dublin"))

    assert reconcile(conn, ctx=ctx, now=now) == ["market"]
    row = conn.execute(
        "SELECT job, status, detail, fired_for FROM job_runs"
        " WHERE fired_for IS NOT NULL").fetchone()
    assert (row["job"], row["status"]) == ("market", "ok")
    assert row["fired_for"], "the run claimed no instant, so it can fire again"


def test_a_second_tick_does_not_rerun_the_same_instant(conn, ctx, monkeypatch):
    """Idempotency end to end, not just in the pure function.

    A 60-second tick asks 60 times an hour. If the claim did not hold, `sync` would
    spend 60 IBKR requests fetching one statement.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from optjournal.jobs import reconcile

    _registry(monkeypatch, "market")
    conn.execute("INSERT INTO job_runs (job, started_at, status) VALUES"
                 " ('market', '2026-08-11T11:00:00+00:00', 'ok')")
    conn.commit()
    now = datetime(2026, 8, 12, 12, 0, tzinfo=ZoneInfo("Europe/Dublin"))

    assert reconcile(conn, ctx=ctx, now=now) == ["market"]
    assert reconcile(conn, ctx=ctx, now=now) == [], (
        "the same instant ran twice, so a tick every 60s means 60 runs an hour"
    )


def test_one_jobs_failure_does_not_stop_the_others(conn, ctx, monkeypatch):
    """CONTAINMENT, and the failure it prevents is the outage this plan exists for.

    Design 3 named it exactly: a daemon thread that raises leaves the HTTP server
    perfectly healthy and the schedule dead -- the 40-hour outage reproduced inside
    its own fix. So a job that raises is recorded and the tick carries on.
    """
    import dataclasses
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from optjournal import jobs as mod

    def boom(_c, _x):
        raise RuntimeError("the keychain is locked")

    def fine(_c, _x):
        return mod.Outcome("ok", "still ran")

    # `sync` fires at 12:00 Dublin and `bars_daily` at 12:30, in that order.
    monkeypatch.setattr(mod, "JOBS", tuple(
        dataclasses.replace(job, run=boom if job.name == "sync" else fine)
        for job in mod.JOBS if job.name in ("sync", "bars_daily")
    ))
    for job in ("sync", "bars_daily"):
        conn.execute("INSERT INTO job_runs (job, started_at, status) VALUES"
                     " (?, '2026-08-11T12:00:00+00:00', 'ok')", (job,))
    conn.commit()
    now = datetime(2026, 8, 12, 13, 0, tzinfo=ZoneInfo("Europe/Dublin"))

    started = mod.reconcile(conn, ctx=ctx, now=now)
    assert started == ["bars_daily"], (
        f"expected the failing sync to be contained and bars_daily to run; got "
        f"{started}"
    )
    rows = {r["job"]: r["status"] for r in conn.execute(
        "SELECT job, status FROM job_runs WHERE fired_for IS NOT NULL")}
    assert rows == {"sync": "failed", "bars_daily": "ok"}, (
        "the failure was not recorded, or it stopped the tick"
    )


def test_a_job_that_keeps_failing_is_backed_off_but_stays_runnable_by_hand(
    conn, ctx, monkeypatch
):
    """A brake that does not become a black hole.

    Five consecutive failures stops the RECONCILER starting it, because a job
    failing for a real reason should not hammer the endpoint that is failing sixty
    times an hour. It stays runnable from the page, and the count resets on any
    healthy outcome -- so recovery needs no restart and no edit.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from optjournal.jobs import FAILURE_BACKOFF, reconcile, run_job

    _registry(monkeypatch, "market")
    conn.execute("INSERT INTO job_runs (job, started_at, status) VALUES"
                 " ('market', '2026-08-11T11:00:00+00:00', 'ok')")
    conn.execute("INSERT OR REPLACE INTO job_state (job, last_status,"
                 " consecutive_failures) VALUES ('market', 'failed', ?)",
                 (FAILURE_BACKOFF,))
    conn.commit()
    now = datetime(2026, 8, 12, 12, 0, tzinfo=ZoneInfo("Europe/Dublin"))

    assert reconcile(conn, ctx=ctx, now=now) == [], (
        "a job past the failure threshold was still started by the reconciler"
    )
    # By hand, though, it runs -- and succeeding clears the backoff.
    run_job(conn, "market", ctx=ctx)
    assert conn.execute(
        "SELECT consecutive_failures FROM job_state WHERE job='market'"
    ).fetchone()[0] == 0, "a successful manual run did not clear the backoff"


def test_the_heartbeat_is_written_by_the_loop_not_by_a_job(conn):
    """The two signals must stay separate, or they collapse the way crons.json did.

    "Did the last run succeed" read `ok` for two days while "is anything driving the
    schedule" was false. A heartbeat written by a job would make a manual run look
    like a live scheduler; a heartbeat written by the loop cannot, because the loop
    is the only thing that ticks.
    """
    from datetime import UTC, datetime

    from optjournal.jobs import JOBS, heartbeat, record_run

    record_run(conn, "market", status="ok", detail="a manual run")
    beat = conn.execute(
        "SELECT heartbeat_at FROM job_state WHERE job='market'").fetchone()[0]
    assert beat is None, (
        "a job outcome wrote a heartbeat, so a hand-run job would read as a "
        "running scheduler"
    )

    now = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    heartbeat(conn, now=now)
    beats = {r["job"]: r["heartbeat_at"] for r in conn.execute(
        "SELECT job, heartbeat_at FROM job_state")}
    for job in JOBS:
        assert beats.get(job.name) == int(now.timestamp()), (
            f"{job.name} has no heartbeat, so `jobs_data` cannot see the loop"
        )


def test_a_run_after_a_suspend_is_stamped_slept(conn, ctx, monkeypatch):
    """Why a noon job fired at 09:14 becomes a field rather than a mystery.

    `monotonic` EXCLUDES sleep on this platform -- measured, 44.6 hours of it -- so
    the wall clock running ahead of it within one tick is a suspend. Both clocks are
    already read, so the stamp is free.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from optjournal.jobs import reconcile

    _registry(monkeypatch, "market")
    conn.execute("INSERT INTO job_runs (job, started_at, status) VALUES"
                 " ('market', '2026-08-11T11:00:00+00:00', 'ok')")
    conn.commit()
    now = datetime(2026, 8, 12, 12, 0, tzinfo=ZoneInfo("Europe/Dublin"))

    reconcile(conn, ctx=ctx, now=now, slept=True)
    assert conn.execute(
        "SELECT slept FROM job_runs WHERE fired_for IS NOT NULL").fetchone()[0] == 1


def test_the_loop_survives_a_tick_that_raises(ctx, tmp_path, monkeypatch):
    """The containment that matters most, at the loop level rather than the job's.

    A tick that raises must not end the schedule. Provoked by making the ledger
    snapshot itself explode, which is upstream of every per-job guard.
    """
    import time

    from optjournal import jobs as mod

    calls = []

    def explode(_conn):
        calls.append(1)
        raise sqlite3.OperationalError("no such table: job_runs")

    monkeypatch.setattr(mod, "_ledger_snapshot", explode)
    clock = mod.Scheduler(
        ctx=mod.Context(archive_dir=tmp_path / "raw", db_path=tmp_path / "s.db"),
        tick_s=0.05,
    )
    clock.start()
    try:
        deadline = time.monotonic() + 5
        while clock.ticks < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        clock.stop()
    assert clock.ticks >= 3, (
        f"the loop stopped after {clock.ticks} ticks and {len(calls)} raises -- a "
        "thread that dies leaves the server healthy and the schedule dead"
    )
    # `ticks` counts ATTEMPTS, so a loop that is alive and failing is visible as
    # exactly that. Counting only successes would make this loop read as dead --
    # which is `crons.json`'s two green days, inverted, and the first version of
    # this test found it: 94 raises against a `ticks` of 0.
    assert clock.tick_failures >= 3, (
        "the failures were not counted, so an alive-but-broken loop is "
        "indistinguishable from a healthy one"
    )


def test_the_scheduler_stops_when_asked_and_does_not_wait_out_its_tick(ctx, tmp_path):
    """Shutdown must not take a full tick, and the thread must actually be joined.

    A loop that ignores its stop signal for 60 seconds is indistinguishable from a
    hung one, and step 7 has to make SIGTERM work. `threading.Event.wait` is what
    makes the wait interruptible; a `time.sleep` would not be.
    """
    import time

    from optjournal.jobs import Context, Scheduler

    clock = Scheduler(
        ctx=Context(archive_dir=tmp_path / "raw", db_path=tmp_path / "s.db"),
        tick_s=30,                       # far longer than the test may take
    )
    clock.start()
    thread = clock._thread               # captured before stop() clears it
    assert thread is not None
    time.sleep(0.2)                      # let it reach the wait
    started = time.monotonic()
    clock.stop(timeout=5)
    elapsed = time.monotonic() - started
    assert elapsed < 2, (
        f"stop() took {elapsed:.1f}s, so it waited out the tick rather than "
        "interrupting it -- SIGTERM would appear to hang"
    )
    # JOINED, not merely forgotten. Asserted on the THREAD rather than on the
    # attribute: an ablation that dropped the `join` and only cleared
    # `self._thread` passed a `_thread is None` check while leaving the loop
    # running against a database the next test is about to delete.
    assert not thread.is_alive(), (
        "stop() returned while the loop thread was still running -- it was "
        "abandoned rather than joined, so a tick can write to a journal the "
        "caller believes it has finished with"
    )


def test_starting_a_running_scheduler_is_refused(tmp_path):
    """Two loops on one journal would double every claim attempt.

    Not fatal -- the file lock and the unique index would refuse the duplicates -- but
    it is a bug that presents as mysterious 409s, so it fails at the call.
    """
    from optjournal.jobs import Context, Scheduler

    clock = Scheduler(
        ctx=Context(archive_dir=tmp_path / "raw", db_path=tmp_path / "s.db"),
        tick_s=30)
    clock.start()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            clock.start()
    finally:
        clock.stop()


def test_replacing_the_registry_actually_reaches_due_jobs(monkeypatch):
    """A REAL BUG THIS FILE COULD NOT SEE, and the tests were the reason.

    `due_jobs` was written `registry: tuple[Job, ...] = JOBS`, and a default
    argument is evaluated at DEFINITION time -- so the tuple was captured once at
    import and `monkeypatch.setattr(jobs, "JOBS", ...)` never reached the function.
    Every test above that replaced the registry was silently exercising the REAL
    one. They passed because the real schedules happened to agree with what the
    stubs asserted, which is the worst way for a test to pass: green, and measuring
    something else.

    Found by running the actual `Scheduler` against a one-job stub registry and
    watching zero runs happen across six ticks.
    """
    import dataclasses
    from datetime import UTC, datetime

    from optjournal import jobs as mod

    only = dataclasses.replace(
        mod.job_by_name("market"), name="market",
        minute=0, hour=0, weekdays=(1, 2, 3, 4, 5, 6, 7), zone="Europe/Dublin",
    )
    monkeypatch.setattr(mod, "JOBS", (only,))
    found = mod.due_jobs(datetime.now(UTC), claimed={}, last_success={},
                         ever_ran={"market"})
    assert [d.job.hour for d in found] == [0], (
        f"due_jobs ignored the replaced registry and used the import-time one: "
        f"{[(d.job.name, d.job.hour) for d in found]}"
    )


def test_the_loop_really_drives_a_job_once_and_then_stops(tmp_path, monkeypatch):
    """The whole step, exercised by the REAL Scheduler rather than by reconcile().

    Everything above tests `reconcile` with an injected clock, which is the right
    way to test the policy and cannot see the loop's own wiring -- the registry bug
    above lived exactly there. So this one starts the thread, lets it tick several
    times, and asserts the shape that matters: it fires ONCE and the claim holds for
    every subsequent tick.
    """
    import dataclasses
    import time
    from datetime import UTC, datetime, timedelta
    from zoneinfo import ZoneInfo

    from optjournal import jobs as mod
    from optjournal.db import connect, migrate

    db = tmp_path / "loop.db"
    conn = connect(db)
    migrate(conn)

    ran: list[str] = []
    now = datetime.now(UTC).astimezone(ZoneInfo("Europe/Dublin"))
    # Due at this very minute, every day, so a fast tick reaches it.
    monkeypatch.setattr(mod, "JOBS", (dataclasses.replace(
        mod.job_by_name("market"), name="market",
        run=lambda _c, _x: (ran.append("market"), mod.Outcome("ok", "stub"))[1],
        minute=now.minute, hour=now.hour, weekdays=(1, 2, 3, 4, 5, 6, 7),
        zone="Europe/Dublin",
    ),))
    conn.execute("INSERT INTO job_runs (job, started_at, status) VALUES"
                 " ('market', ?, 'ok')",
                 ((now - timedelta(days=2)).isoformat(timespec="seconds"),))
    conn.commit()
    conn.close()

    clock = mod.Scheduler(
        ctx=mod.Context(archive_dir=tmp_path / "raw", db_path=db), tick_s=0.2)
    clock.start()
    try:
        deadline = time.monotonic() + 15
        while clock.ticks < 5 and time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        clock.stop()

    assert clock.ticks >= 5 and clock.tick_failures == 0, (
        f"ticks={clock.ticks} failures={clock.tick_failures}"
    )
    assert ran == ["market"], (
        f"the job ran {len(ran)} times across {clock.ticks} ticks; it must fire "
        "once and then be held by its claim"
    )
    check = connect(db)
    claimed = check.execute(
        "SELECT COUNT(*) FROM job_runs WHERE fired_for IS NOT NULL").fetchone()[0]
    beat = check.execute(
        "SELECT heartbeat_at FROM job_state WHERE job='market'").fetchone()[0]
    check.close()
    assert claimed == 1, f"{claimed} claimed instants, expected exactly 1"
    assert beat, "the loop never wrote a heartbeat, so the page reads it as dead"


# --------------------------------------------------------------------------
# A sync nobody scheduled still belongs in the ledger.
#
# The outage this closes: `POST /api/sync` and `optjournal sync` did the work and
# wrote no row, so a hand-run sync that fixed the journal left
# `consecutive_failures` untouched and the reconciler went on refusing to start a
# job that had been working for hours. Two "sync now" controls on one page, only
# one of which the scheduler could learn from.
# --------------------------------------------------------------------------

def test_a_manual_sync_clears_the_backoff_the_scheduler_is_holding(conn):
    """The whole point, asserted end to end over the ledger.

    A journal sitting at the failure threshold is the state a real one reached and
    stayed in for two weeks. Recording a manual sync is what lets it out, and
    without a restart -- which matters, because the person fixing it is looking at
    a page, not at a process.
    """
    from optjournal.jobs import FAILURE_BACKOFF, record_manual_sync

    conn.execute("INSERT OR REPLACE INTO job_state (job, last_status,"
                 " consecutive_failures) VALUES ('sync', 'failed', ?)",
                 (FAILURE_BACKOFF,))
    conn.commit()

    record_manual_sync(conn, {
        "changed": True, "summary": "2 new trade(s), 0 new cash row(s)",
        "new_trades": 2,
    })

    state = conn.execute(
        "SELECT last_status, consecutive_failures FROM job_state WHERE job='sync'"
    ).fetchone()
    assert state["consecutive_failures"] == 0, (
        "a successful manual sync left the scheduler's backoff in place"
    )
    assert state["last_status"] == "ok"
    row = conn.execute(
        "SELECT job, fired_for, status, detail FROM job_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert (row["job"], row["status"]) == ("sync", "ok")
    assert row["fired_for"] is None, (
        "a manual run claimed a scheduled slot, which would make the schedule "
        "think that minute had been served"
    )
    assert "2 new trade(s)" in row["detail"]


def test_a_manual_sync_that_failed_on_credentials_counts_as_a_failure(conn):
    """Recording must not be a way to launder a failure into a reset.

    A token IBKR rejects is the case that matters: it arrives at the same call
    site as a success, and reporting it as anything but `failed` would clear the
    backoff on a sync that fetched nothing -- turning the brake off precisely when
    it is right.
    """
    from optjournal.flex import TokenRejected
    from optjournal.jobs import record_manual_sync

    record_manual_sync(conn, TokenRejected("IBKR says your Flex token is expired"))

    row = conn.execute(
        "SELECT status, detail FROM job_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "failed"
    assert "credentials:" in row["detail"], "the detail does not name the cause"
    assert conn.execute(
        "SELECT consecutive_failures FROM job_state WHERE job='sync'"
    ).fetchone()[0] == 1


def test_a_cooldown_is_nothing_rather_than_a_failure(conn):
    """`nothing`, and it DOES clear a backoff -- both deliberate.

    The counter means consecutive FAILURES, and being told to wait is not one:
    the cooldown is this journal's own guard working, so counting it would let the
    guard eventually disable the job it is protecting.
    """
    from datetime import UTC, datetime

    from optjournal.flex import FetchCooldown
    from optjournal.jobs import record_manual_sync, sync_outcome

    cooldown = FetchCooldown("1591754", datetime(2026, 9, 24, tzinfo=UTC), 480)
    assert sync_outcome(cooldown).status == "nothing"

    record_manual_sync(conn, cooldown)
    row = conn.execute(
        "SELECT status, detail FROM job_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "nothing"
    assert "cooldown:" in row["detail"]


def test_the_scheduled_and_manual_paths_read_a_sync_the_same_way():
    """One mapping, not two that agree today.

    `_sync` (scheduled, recorded by `run_job`) and `record_manual_sync` (the page
    and the CLI) must agree about what `ok`, `nothing` and `failed` mean, or the
    same sync reads two ways depending on who started it -- which is how the
    ledger stops describing the work.
    """
    import inspect

    from optjournal import jobs

    assert "sync_outcome" in inspect.getsource(jobs._sync), (
        "the scheduled sync no longer shares the ledger's mapping"
    )
    assert "sync_outcome" in inspect.getsource(jobs.record_manual_sync), (
        "the manual sync no longer shares the ledger's mapping"
    )
    empty = jobs.sync_outcome({"changed": False, "summary": "nothing new",
                               "new_trades": 0})
    assert empty.status == "nothing", "an empty sync is not a success"


def test_the_backoff_warning_names_the_reason_not_just_the_count(conn, ctx,
                                                                monkeypatch, caplog):
    """344 identical lines over a two-week outage, naming the cause in none.

    The reason was in `job_runs.detail` the whole time, which takes a SQL client to
    read -- so the log said a job was backed off and never why. A warning a human
    cannot act on is the same as no warning.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from optjournal.jobs import FAILURE_BACKOFF, reconcile

    _registry(monkeypatch, "market")
    conn.execute("INSERT INTO job_runs (job, started_at, status, detail) VALUES"
                 " ('market', '2026-08-11T11:00:00+00:00', 'failed',"
                 " 'FlexAuthError: Token has expired.')")
    conn.execute("INSERT OR REPLACE INTO job_state (job, last_status,"
                 " consecutive_failures) VALUES ('market', 'failed', ?)",
                 (FAILURE_BACKOFF,))
    conn.commit()
    now = datetime(2026, 8, 12, 12, 0, tzinfo=ZoneInfo("Europe/Dublin"))

    with caplog.at_level("WARNING"):
        assert reconcile(conn, ctx=ctx, now=now) == []
    assert "Token has expired" in caplog.text, (
        "the backoff warning still does not say why the job is backed off"
    )


def test_a_bars_run_that_collected_most_of_the_book_is_not_a_failure(conn, monkeypatch):
    """The asymmetry that disabled bar collection over one bad symbol.

    Measured: one index root the price source spells differently failed two windows
    out of twenty-two, the run reported `failed` while writing 8,889 bars, and five
    of those backed the whole daily job off -- so the book stopped being collected
    because of a symbol that was never going to work.

    `ok`, therefore, when anything landed. NOT silently green: the count and the
    failures are in the detail, which is what the page shows and what the backoff
    warning now reads back.
    """
    from optjournal import jobs

    class _Result:
        written, skipped, requested = 8889, 20, 22
        failures = ["SPX 1d: HTTPError: HTTP Error 404: Not Found",
                    "SPX 1h: HTTPError: HTTP Error 404: Not Found"]

    monkeypatch.setattr(jobs, "backfill_bars", lambda conn, perishable_only: _Result())
    outcome = jobs._bars(conn, None, live=False)
    assert outcome.status == "ok", (
        "a run that wrote 8,889 bars still reports failed, which backs the job off"
    )
    assert "8889 bar(s)" in outcome.detail, "the work done is not reported"
    assert "2 window(s) failed" in outcome.detail, "the failures are hidden"
    assert "SPX 1d" in outcome.detail, "the failing symbol is not named"


def test_a_bars_run_that_collected_nothing_at_all_still_fails(conn, monkeypatch):
    """The other side of the same line, which is what stops this being a whitewash.

    Nothing written and something broken is the state the ledger exists to catch --
    three cron jobs read `ok` for two days while collecting nothing, which is why
    `ok` may never mean "we tried".
    """
    from optjournal import jobs

    class _Result:
        written, skipped, requested = 0, 0, 3
        failures = ["TSLA 1d: URLError: nodename nor servname provided"]

    monkeypatch.setattr(jobs, "backfill_bars", lambda conn, perishable_only: _Result())
    outcome = jobs._bars(conn, None, live=False)
    assert outcome.status == "failed"
    assert "TSLA 1d" in outcome.detail


# --------------------------------------------------------------------------
# The Trade Confirmation poll. Same-session fills, every 25 minutes in session.
# --------------------------------------------------------------------------

def test_no_confirm_query_configured_is_nothing_not_a_failure(conn, monkeypatch):
    """The common case, and it must not accumulate failures.

    Only the Activity Statement is required for this journal to work, so a reader
    who never creates a confirm query has an idle job -- not a red Collection card
    and not a job backed off after five ticks of a feature nobody enabled.
    """
    from pathlib import Path  # noqa: PLC0415 - local to this test

    from optjournal import jobs

    monkeypatch.setattr(jobs.prefs, "confirm_query_id", lambda *a, **k: None)
    ctx = jobs.Context(archive_dir=Path("."), db_path=Path("."))
    outcome = jobs._confirm(conn, ctx)
    assert outcome.status == "nothing"
    assert "no Trade Confirmation query configured" in outcome.detail


def test_a_journal_with_no_statement_yet_waits_rather_than_failing(conn, monkeypatch):
    """A confirm carries no base currency, so the journal has to supply it.

    `nothing`, because the precondition is unmet rather than broken and the daily
    sync resolves it on its own. Counting it would back the poll off five ticks into
    a fresh clone's first market session -- exactly when it should be waiting.
    """
    from pathlib import Path  # noqa: PLC0415 - local to this test

    from optjournal import jobs

    monkeypatch.setattr(jobs.prefs, "confirm_query_id", lambda *a, **k: "1621016")
    ctx = jobs.Context(archive_dir=Path("."), db_path=Path("."))
    outcome = jobs._confirm(conn, ctx)
    assert outcome.status == "nothing"
    assert "sync has to land first" in outcome.detail


def test_the_confirm_poll_has_its_own_cooldown_well_under_the_statements(conn):
    """Two query types, two cadences, and the budgets do not share.

    A statement is regenerated once a day, so its 15-minute cooldown costs nothing;
    a confirm payload grows with every fill, so the same window would make an
    intraday feed pointless. `flex._check_cooldown` is keyed by query id, which is
    what lets these differ without the shorter one loosening the longer.
    """
    from optjournal.flex import FETCH_COOLDOWN_S
    from optjournal.jobs import CONFIRM_COOLDOWN_S, job_by_name

    assert CONFIRM_COOLDOWN_S < FETCH_COOLDOWN_S
    job = job_by_name("confirm")
    assert job.window_s <= 30 * 60, (
        "the poll window is wider than half an hour, which is not an hourly feed"
    )
    assert job.window_s * 60 >= CONFIRM_COOLDOWN_S, (
        "the cooldown is longer than the poll window, so every scheduled run would "
        "be refused before it sent anything"
    )
    assert job.spends_broker_request is True
    assert job.zone == "America/New_York", (
        "the poll is scheduled off the session, not off the reader's wall clock"
    )
