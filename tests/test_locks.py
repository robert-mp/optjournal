"""Cross-process locking: the primitive, and the two bugs it exists to fix.

Every test here that matters uses real SUBPROCESSES rather than threads. That is
not thoroughness for its own sake -- it is the whole point. The bugs these guard
were invisible to a `threading.Lock` precisely because the racing parties are
separate processes (the cron and the web server), and a thread-based test would
have passed against the broken code.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import ROOT

from optjournal.locks import DEFAULT_TIMEOUT_S, LockTimeout, locked


def _run(body: str, *args: str, timeout: int = 60):
    """Run `body` in a child process with the project importable."""
    script = textwrap.dedent(body)
    return subprocess.run(  # noqa: S603 - fixed interpreter, no shell
        [sys.executable, "-c", script, *args],
        capture_output=True, text=True, cwd=str(ROOT), timeout=timeout, check=False,
    )


def test_the_lock_excludes_a_second_process(tmp_path):
    """The base guarantee, across a process boundary rather than a thread one."""
    lock = tmp_path / "x.lock"
    with locked(lock):
        proc = _run(
            """
            import sys
            from pathlib import Path
            from optjournal.locks import LockTimeout, locked
            try:
                with locked(Path(sys.argv[1]), timeout_s=1):
                    print("ACQUIRED")
            except LockTimeout:
                print("BLOCKED")
            """,
            str(lock),
        )
    assert proc.stdout.strip() == "BLOCKED", proc.stderr


def test_the_lock_is_released_for_the_next_process(tmp_path):
    lock = tmp_path / "x.lock"
    with locked(lock):
        pass
    proc = _run(
        """
        import sys
        from pathlib import Path
        from optjournal.locks import locked
        with locked(Path(sys.argv[1]), timeout_s=2):
            print("ACQUIRED")
        """,
        str(lock),
    )
    assert proc.stdout.strip() == "ACQUIRED", proc.stderr


def test_a_crashed_holder_does_not_wedge_the_journal():
    """The reason this is flock and not a lock table or a pid file.

    A row in SQLite or a pid file survives the process that wrote it, so a crash
    leaves the journal locked until someone works out how to clear it -- and the
    staleness heuristic that avoids that is itself a source of bugs. The KERNEL
    releases an flock when the fd closes, including on SIGKILL.
    """
    import tempfile

    d = Path(tempfile.mkdtemp())
    lock = d / "x.lock"
    child = _run(
        """
        import os, signal, sys, time
        from pathlib import Path
        from optjournal.locks import locked
        with locked(Path(sys.argv[1])):
            print("HOLDING", flush=True)
            os.kill(os.getpid(), signal.SIGKILL)   # die still holding it
        """,
        str(lock),
        timeout=30,
    )
    assert child.returncode != 0, "the child was supposed to be killed"

    # The lock must be free now, with no cleanup step in between.
    with locked(lock, timeout_s=2):
        pass


def test_a_timeout_raises_rather_than_proceeding(tmp_path):
    """Proceeding anyway would defeat the point of asking.

    Everything behind this lock must not happen twice -- a spent IBKR request, a
    schema migration. Returning "could not lock, carrying on" would turn a
    correctness guard into a log line.
    """
    lock = tmp_path / "x.lock"
    with locked(lock), pytest.raises(LockTimeout, match="held"):
        with locked(lock, timeout_s=0):
            pass


def test_the_timeout_default_covers_the_slowest_thing_it_guards():
    """Pinned so shortening it means reckoning with what a fetch actually costs.

    The slowest holder is `flex.fetch`: keyring (measured at 8.2s on the machine
    this was written on) plus an IBKR download that retries while the statement
    generates -- `flex.POLL_WORST_CASE_S` is 660.
    """
    assert DEFAULT_TIMEOUT_S >= 60


# --- the two bugs -----------------------------------------------------------


def test_two_processes_cannot_both_clear_the_fetch_cooldown(tmp_path):
    """THE REQUEST-BUDGET BUG, reproduced across processes and then fixed.

    `flex.fetch` checked the cooldown, downloaded, and recorded the stamp only on
    success -- so the window between "cleared" and "recorded" spanned the whole
    request. Two threads behind a barrier both cleared it before the fix. Three
    call sites can enter that window: the Sync button, `optjournal fetch` and
    `optjournal sync`, so a noon cron overlapping an open page is enough.

    Drives the REAL `flex.fetch`, with only the network stubbed. An earlier version
    of this test reimplemented the check-download-record sequence with its own
    `locked()` call, and passed against a completely unguarded `fetch` -- it was
    testing its own scaffolding. Ablation caught that: removing the lock from
    flex.py left it green. So the child monkeypatches `read_token` and `FlexClient`
    and then calls the real function, which means the lock under test is the one
    the application actually takes.
    """
    from optjournal.flex import FETCH_COOLDOWN_S

    stale = (datetime.now(UTC) - timedelta(seconds=FETCH_COOLDOWN_S + 60)).isoformat()
    (tmp_path / ".fetch-state.json").write_text(json.dumps({"Q": {"last_fetch": stale}}))

    body = """
        import sys, time
        from pathlib import Path
        from optjournal import flex

        # Stub the network, keep everything else: no request is spent, and the
        # download takes long enough for a second process to collide with it.
        class _Client:
            def __init__(self, *a, **k): pass
            def download(self, *a, **k):
                time.sleep(1.5)
                return b"<FlexQueryResponse></FlexQueryResponse>"

        flex.read_token = lambda account=None: "token"
        # `_client_factory`, the module's single seam: `fetch` used to construct
        # `FlexClient` directly, and when a socket-timeout subclass was introduced
        # this stub kept applying to a name nothing called -- so both subprocesses
        # went to the REAL IBKR endpoint. This test failing is what caught it.
        flex._client_factory = _Client
        flex.parse_xml_file = lambda path: object()

        try:
            flex.fetch("Q", archive_dir=Path(sys.argv[1]))
            print("FETCHED")
        except flex.FetchCooldown:
            print("REFUSED")
    """
    script = textwrap.dedent(body)
    procs = [
        subprocess.Popen(  # noqa: S603 - fixed interpreter, no shell
            [sys.executable, "-c", script, str(tmp_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(ROOT),
        )
        for _ in range(2)
    ]
    outs = [p.communicate(timeout=60)[0].strip() for p in procs]

    assert outs.count("FETCHED") == 1, (
        f"both processes spent an IBKR request against a lockout budget: {outs}"
    )
    assert outs.count("REFUSED") == 1, outs


def test_the_fetch_lock_wraps_the_whole_check_download_record_sequence():
    """Asserted over the source, because the ORDERING is the fix.

    A lock taken after the cooldown check, or released before the stamp is
    written, leaves exactly the window this closes. `fetch` must therefore be a
    thin wrapper that acquires and delegates -- if the body ever moves back inside
    it, this fails.
    """
    import inspect

    from optjournal import flex

    src = inspect.getsource(flex.fetch)
    assert "locked(" in src, "fetch no longer takes the cross-process lock"
    assert "_fetch_locked" in src, (
        "fetch no longer delegates; the lock must wrap the whole sequence"
    )
    inner = inspect.getsource(flex._fetch_locked)
    assert "_check_cooldown" in inner and "_record_fetch" in inner, (
        "the check and the record must both sit inside the locked call"
    )


def test_a_migration_does_not_drop_a_view_from_under_a_reader(tmp_path):
    """THE HTTP 500 BUG, and the test whose FIRST VERSION WAS WRONG.

    `migrate` drops every view; `open_journal` migrates per request. So an
    overlapping request could query a view another had just dropped, reaching the
    browser as `HTTP 500: no such table: current_option_positions`.

    The first fix was a cross-process lock, and the first version of this test ran
    `migrate` then `SELECT` inside each worker -- which meant every reader happened
    to hold the lock while reading, and the test passed. A REAL reader holds no
    lock: `/api/state` migrates, releases, and only then runs its SELECTs. Split
    into separate reader and migrator threads, the same code failed 4,941 times.

    So readers and migrators are DELIBERATELY separate threads here, and must stay
    that way. Recombining them is what made this test lie once already.

    The lock is still necessary -- two interleaved migrations are their own
    problem -- but the load-bearing guard is `schema_is_current`: a migration that
    does not run cannot drop a view.
    """
    import sqlite3
    import threading
    import time as _time

    from optjournal.db import connect, migrate

    path = tmp_path / "j.db"
    conn = connect(path)
    migrate(conn)
    conn.close()

    errors: list[str] = []
    stop = threading.Event()
    guard = threading.Lock()

    def reader() -> None:
        # ONE long-lived connection, taking no lock -- the shape of a real request.
        c = connect(path)
        while not stop.is_set():
            try:
                c.execute("SELECT COUNT(*) FROM trade_orders").fetchone()
                c.execute("SELECT COUNT(*) FROM current_option_positions").fetchone()
            except sqlite3.Error as exc:  # noqa: PERF203 - the failure is the point
                with guard:
                    errors.append(f"{type(exc).__name__}: {exc}")
        c.close()

    def migrator() -> None:
        while not stop.is_set():
            c = connect(path)
            migrate(c)
            c.close()

    threads = [threading.Thread(target=reader) for _ in range(3)]
    threads += [threading.Thread(target=migrator) for _ in range(2)]
    for t in threads:
        t.start()
    _time.sleep(0.75)
    stop.set()
    for t in threads:
        t.join(timeout=30)

    assert not errors, (
        f"a migration dropped a view from under a reader: {sorted(set(errors))}"
    )


def test_a_current_schema_is_not_migrated_again(tmp_path):
    """The guard that actually closes the race: don't run when nothing is needed.

    Not an optimisation. A migration that does not run cannot drop a view, and the
    steady state of any journal is that no migration is needed -- so the common
    path stops touching the schema at all.
    """
    from optjournal.db import SCHEMA_VERSION, connect, migrate, schema_is_current

    path = tmp_path / "j.db"
    conn = connect(path)
    assert not schema_is_current(conn), "an empty file cannot be current"
    migrate(conn)
    assert schema_is_current(conn)
    assert migrate(conn) == SCHEMA_VERSION, "a no-op migrate still reports the version"

    # A missing view means NOT current, however right the version stamp looks --
    # which is the state a half-run migration leaves behind.
    conn.execute("DROP VIEW trade_orders")
    assert not schema_is_current(conn), (
        "the check must look at the views, not just the version: dropping one is "
        "exactly the damage it exists to notice"
    )
    migrate(conn)
    assert schema_is_current(conn), "migrate did not repair the missing view"
    conn.close()


def test_a_brand_new_database_is_migrated_rather_than_skipped(tmp_path):
    """The first-open case, which the check must answer False for and not raise.

    `schema_version` does not exist yet on an empty file, so a naive check throws
    `no such table` and every fresh journal fails to initialise. Caught by the
    reader/migrator test above, which starts from an empty file.
    """
    from optjournal.db import connect, migrate, schema_is_current

    conn = connect(tmp_path / "fresh.db")
    assert schema_is_current(conn) is False
    migrate(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM trade_orders").fetchone()[0] == 0
    conn.close()


def test_open_journal_rolls_back_a_failed_write(tmp_path):
    """A statement that raises leaves the connection holding the write lock.

    Measured: after a refused INSERT, `in_transaction` stays True and the next
    writer blocks for the whole BUSY_TIMEOUT_MS before failing -- 15.49s.

    `close()` happens to release it, so `open_journal` was already safe; this pins
    the explicit rollback because a scheduler thread holds one connection across
    many operations and has no close() to save it.

    HONEST ABOUT ITS OWN STRENGTH: this test passes with the rollback removed,
    because close() does the work today. It is a REGRESSION guard for the
    invariant, not proof the rollback is load-bearing -- and saying so is better
    than implying an ablation it cannot survive. The test that would fail needs a
    connection that outlives the failed write, which arrives with the scheduler.
    """
    import sqlite3
    import time as _time

    from optjournal.db import connect, migrate, open_journal

    path = tmp_path / "j.db"
    conn = connect(path)
    migrate(conn)
    conn.close()

    with pytest.raises(RuntimeError), open_journal(path) as c:
        c.execute("INSERT INTO watchlist (symbol, added_at) VALUES ('X','1')")
        raise RuntimeError("a handler blew up mid-write")

    started = _time.perf_counter()
    other = connect(path)
    try:
        other.execute(
            "INSERT OR IGNORE INTO watchlist (symbol, added_at) VALUES ('Y','2')")
        other.commit()
    except sqlite3.OperationalError as exc:  # pragma: no cover - the bug
        pytest.fail(f"the write lock was never released: {exc}")
    elapsed = _time.perf_counter() - started
    assert elapsed < 1.0, (
        f"the next writer waited {elapsed:.1f}s, so a transaction was left open"
    )
    rows = [r["symbol"] for r in other.execute("SELECT symbol FROM watchlist")]
    other.close()
    assert rows == ["Y"], "the uncommitted row survived a rollback"


def test_migrate_holds_a_lock_derived_from_the_connection():
    """The lock path comes from the CONNECTION, so no caller can forget it.

    `migrate(conn)` keeps its signature and all seven call sites are protected
    without being edited. Passing the path in would have meant seven chances to
    leave a hole.
    """
    import inspect

    from optjournal import db

    src = inspect.getsource(db.migrate)
    assert "locked(" in src and "_lock_path" in src
    assert "_migrate_unlocked" in src, "migrate must delegate under the lock"


def test_an_in_memory_database_needs_no_lock():
    """No other process can see it, so locking would only cost."""
    import sqlite3

    from optjournal.db import _lock_path

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert _lock_path(conn) is None
    conn.close()


def test_busy_timeout_is_set_explicitly(tmp_path):
    """It was the inherited sqlite3 default -- a number nothing here chose.

    WAL allows one writer, so a second writer without a timeout raises "database
    is locked" instantly rather than waiting. Two processes writing is the normal
    case here: a scheduled sync while a page is open.
    """
    from optjournal.db import BUSY_TIMEOUT_MS, connect

    conn = connect(tmp_path / "j.db")
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
    conn.close()


def test_a_second_writer_waits_instead_of_failing(tmp_path):
    """The behaviour the pragma buys, asserted rather than assumed.

    Sized from measurement: 5,000 price-bar upserts commit in 6ms on this
    journal, so an ordinary write is three orders of magnitude inside the timeout.
    """
    import sqlite3
    import threading

    from optjournal.db import connect, migrate

    path = tmp_path / "j.db"
    conn = connect(path)
    migrate(conn)
    conn.close()

    first = connect(path)
    first.execute("BEGIN IMMEDIATE")
    first.execute("INSERT OR IGNORE INTO watchlist (symbol, added_at) VALUES ('AAA','x')")

    outcome: dict[str, object] = {}

    def second() -> None:
        # Opened in this thread: sqlite3 connections have thread affinity.
        other = connect(path)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute(
                "INSERT OR IGNORE INTO watchlist (symbol, added_at) VALUES ('BBB','y')")
            other.commit()
            outcome["ok"] = True
        except sqlite3.OperationalError as exc:
            outcome["error"] = str(exc)
        finally:
            other.close()

    thread = threading.Thread(target=second)
    thread.start()
    time.sleep(0.4)          # hold the write lock, then let go
    first.commit()
    first.close()
    thread.join(timeout=30)

    assert outcome.get("ok"), (
        f"the second writer failed instead of waiting: {outcome.get('error')}"
    )
    conn = connect(path)
    rows = [r["symbol"] for r in conn.execute(
        "SELECT symbol FROM watchlist ORDER BY symbol")]
    conn.close()
    assert rows == ["AAA", "BBB"], "a write was lost"
