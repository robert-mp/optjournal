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
