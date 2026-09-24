"""`optjournal serve` under a real signal, in a real subprocess.

WHY A SUBPROCESS AND A REAL SIGNAL. `grep signal tests/` found only the mutation
harness before this file, and that absence is exactly why the defect below shipped
invisibly: it lives in the interaction between CPython's signal delivery, which is
always on the main thread, and `socketserver.shutdown`, which waits on an event only
`serve_forever` can set. Nothing short of sending the signal to a running server can
see it.

THE DEFECT, reproduced from first principles with the bare stdlib before the fix was
written -- so the result is about `socketserver`, not about this package:

    serve_forever on the MAIN thread:  HANDLER ENTERED, still alive 3s later
                                       (shutdown() never returned)
    serve_forever on a THREAD:         SHUTDOWN RETURNED, exited in 1.0s

`socketserver`'s own docstring states it: "This must be called while serve_forever()
is running in another thread, or it will deadlock."

WHY IT MATTERS MORE NOW THAN IT WOULD HAVE BEFORE. `serve` holds a scheduler. On the
deadlocking shape `clock.stop()` never runs, so the heartbeat keeps advancing while
the listener is dead -- inverting the one honesty signal the scheduler was built to
provide. Then launchd's `ExitTimeOut` SIGKILLs, orphaning the in-flight run's
`running` row on every NORMAL stop.

Ablated: restoring the main-thread shape made a real `optjournal serve` sit alive
15s after SIGTERM. With the fix it exits in 0.53s.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import ROOT, code_only, connect_migrated

from optjournal.demo import write_demo_statement
from optjournal.ingest import ingest_file

#: The installed console script, which is what launchd will invoke. Exercising the
#: entry point rather than `python -c "serve(...)"` is deliberate: the signal
#: handling is installed by `serve`, but whether the MAIN thread reaches it is a
#: property of how the process was started.
CLI = (
    ROOT / ".venv" / "Scripts" / "optjournal.exe"
    if os.name == "nt"
    else ROOT / ".venv" / "bin" / "optjournal"
)

#: Generous. The fix exits in ~0.5s and the broken shape never exits at all, so
#: anything in between is a clear verdict rather than a flake.
EXIT_BUDGET_S = 15


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def journal(tmp_path) -> Path:
    """A demo journal in a scratch directory. Never the real archive.

    The served process starts a SCHEDULER, so its archive must be a scratch path:
    pointed at the live `raw/`, a reconciler tick could spend a real IBKR request
    during a test run.
    """
    statement = write_demo_statement(tmp_path / "demo", tmp_path / "demo.db")
    conn = connect_migrated(tmp_path / "demo.db")
    ingest_file(conn, statement)
    conn.commit()
    conn.close()
    return tmp_path / "demo.db"


def _serve(journal: Path, port: int) -> subprocess.Popen[str]:
    if os.name != "nt" and not CLI.exists():
        pytest.skip(f"{CLI} is not installed; run `uv sync`")
    # Windows console scripts are small .exe launchers which start Python as a
    # child. GenerateConsoleCtrlEvent targets a process group, and targeting the
    # launcher does not guarantee its child receives Ctrl+Break. Exercise the
    # application process directly there; on POSIX retain the installed script
    # because that is what launchd invokes.
    command = (
        [sys.executable, "-m", "optjournal.cli"]
        if os.name == "nt"
        else [str(CLI)]
    )
    return subprocess.Popen(  # noqa: S603 - a fixed argv, no shell
        [*command, "serve", "--port", str(port), "--db", str(journal),
         "--archive", str(journal.parent / "raw")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        # Unbuffered, or `print()` to a pipe is block-buffered and the startup
        # banner never arrives -- the same reason the launchd plist will need
        # PYTHONUNBUFFERED. Measured: a redirected serve log sat at 0 bytes.
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        creationflags=(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if os.name == "nt" else 0
        ),
    )


def _wait_until_bound(proc: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"serve exited before binding:\n{proc.communicate()[0]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    proc.kill()
    pytest.fail("serve never bound its port")


if os.name == "nt":
    _STOP_CASES = [
        pytest.param(signal.SIGBREAK, signal.CTRL_BREAK_EVENT, id="CTRL_BREAK")
    ]
else:
    _STOP_CASES = [
        pytest.param(signal.SIGTERM, signal.SIGTERM, id="SIGTERM"),
        pytest.param(signal.SIGINT, signal.SIGINT, id="SIGINT"),
    ]


def _stop(proc: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc.send_signal(signal.SIGTERM)


@pytest.mark.parametrize(("handled", "sent"), _STOP_CASES)
def test_serve_exits_promptly_on_a_signal(journal, handled, sent):
    """BOTH signals, because they arrive from different places and one is new.

    `SIGINT` is Ctrl-C, which `serve` has always handled through
    `KeyboardInterrupt`. `SIGTERM` is what launchd sends, and before this fix
    nothing in the suite had ever sent it -- so the handler that deadlocks was
    added by this plan and would have been exercised for the first time by launchd
    stopping the service.
    """
    port = _free_port()
    proc = _serve(journal, port)
    _wait_until_bound(proc, port)

    started = time.monotonic()
    proc.send_signal(sent)
    try:
        output = proc.communicate(timeout=EXIT_BUDGET_S)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail(
            f"serve was still alive {EXIT_BUDGET_S}s after {handled.name}. That is the "
            "socketserver deadlock: shutdown() called from a signal handler waits "
            "on an event only serve_forever can set, and serve_forever is on the "
            "main thread. Run it on a thread and block the main thread on an Event."
        )
    elapsed = time.monotonic() - started

    assert proc.returncode == 0, (
        f"serve exited {proc.returncode} on {handled.name}, not 0 -- its supervisor "
        f"reads a non-zero exit as a crash and respawns:\n{output}"
    )
    assert elapsed < EXIT_BUDGET_S, f"took {elapsed:.1f}s"


def test_the_port_is_released_so_a_respawn_can_bind(journal):
    """launchd's `KeepAlive` respawns within ~10s, into the same port.

    A listener left LISTEN-bound makes that respawn fail with `Errno 48`, and
    `ThrottleInterval` turns the failure into an invisible crash loop -- two stale
    `serve` processes were found live during the surveys that produced this plan.
    """
    port = _free_port()
    proc = _serve(journal, port)
    _wait_until_bound(proc, port)
    _stop(proc)
    try:
        proc.communicate(timeout=EXIT_BUDGET_S)
    except subprocess.TimeoutExpired:
        # THE OUTPUT, not just the fact. This failed three times on windows-latest
        # saying only "did not exit", which named neither the stage nor the cause --
        # the startup banner and the scheduler's stop line are both in here, and
        # which of them is missing is the whole diagnosis. Killed first so
        # `communicate` returns rather than blocking a second time.
        proc.kill()
        output = (proc.communicate()[0] or "").strip()
        pytest.fail(
            f"serve did not exit {EXIT_BUDGET_S}s after the stop signal; see "
            f"test_serve_exits_promptly_on_a_signal. Its output was:\n{output}"
        )

    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            pytest.fail(
                f"port {port} is still bound after a clean exit ({exc}), so "
                "launchd's respawn fails Errno 48 and crash-loops"
            )


def test_the_scheduler_is_stopped_before_the_listener_goes_away(journal):
    """ORDER, and it is the reason the heartbeat can be trusted.

    If the listener died first and the scheduler kept ticking, the heartbeat would
    keep advancing while the journal served nothing -- the page would read
    `scheduler alive` about a process that had stopped answering. That is the
    two-green-days failure this plan exists to fix, wearing a new hat.

    Checked on the source rather than by racing the shutdown: the invariant is an
    ordering, and asserting it against a 0.5s exit would be a flake generator.
    """
    import inspect

    from optjournal import web

    # STATEMENTS, not mentions: `index` on the raw source finds the first
    # occurrence, and `serve`'s own comment explains why `clock.stop()` comes first
    # -- so the naive version of this assertion compared two comment positions and
    # failed against correct code. Indented calls at the start of a line are the
    # statements themselves.
    body = inspect.getsource(web.serve)
    stop_at = body.index("\n                clock.stop()")
    shut_at = body.index("\n            httpd.shutdown()")
    assert stop_at < shut_at, (
        "the listener is shut down before the scheduler, so a tick can run against "
        "a journal that is no longer being served -- and the heartbeat would say "
        "the scheduler is alive"
    )


def test_serve_forever_runs_on_a_thread_not_the_main_one():
    """The structural half of the deadlock fix.

    Pinned in the source because the runtime symptom is a HANG: a test that only
    checked "it exits" would still pass if someone later moved `serve_forever` back
    to the main thread and removed the handler, which exits cleanly by default
    disposition -- and then the first `httpd.shutdown()` added after that would
    deadlock again, invisibly.
    """
    import inspect

    from optjournal import web

    body = inspect.getsource(web.serve)
    assert "target=httpd.serve_forever" in body, (
        "serve_forever is not being run on a thread, so any shutdown() from a "
        "signal handler will deadlock (socketserver's own docstring says so)"
    )
    # `stop.wait(` rather than `stop.wait()`: the wait is LOOPED on a short
    # timeout now, because a bare one is not interruptible on Windows -- see
    # test_the_stop_wait_is_interruptible_rather_than_bare. The claim this makes is
    # unchanged, that the main thread blocks on an event and so has something a
    # signal can interrupt.
    assert "stop.wait(" in body, (
        "the main thread no longer blocks on an event, so it has nothing to be "
        "interrupted by a signal"
    )
    assert body.index("serving.wait(") < body.index("stop.wait("), (
        "the main thread can enter shutdown before serve_forever has entered its "
        "loop; socketserver.shutdown() deadlocks in that startup interval"
    )


def test_a_signal_handler_is_installed_for_both_stop_signals():
    """And it must tolerate not being the main thread.

    `signal.signal` raises `ValueError` off the main thread, and `serve` is
    importable and callable from a test or a future embedding. A bare call would
    turn "started a server in a thread" into a crash.
    """
    import inspect

    from optjournal import web

    body = inspect.getsource(web.serve).replace(" ", "")
    assert "signal.SIGTERM" in body and "signal.SIGINT" in body, (
        "one of the two stop signals is unhandled"
    )
    assert "signal.SIGBREAK" in body, "Windows Ctrl+Break is unhandled"
    assert "suppress(ValueError)" in body, (
        "installing the handler off the main thread would raise; serve must "
        "tolerate being called from a thread"
    )


def test_the_stop_wait_is_interruptible_rather_than_bare():
    """A bare `Event.wait()` on the main thread is not interruptible on Windows.

    CPython runs signal and console-control handlers on the main thread only, so a
    main thread parked in an uninterruptible lock acquire cannot run the handler --
    and nothing but the handler sets the event it is waiting on. Whether that
    deadlocked depended on whether the signal arrived before or after the wait was
    entered, so it presented as flakiness: three consecutive windows-latest runs
    failed this file, each on a different test, while ubuntu passed.

    Pinned as SOURCE because the race is unreproducible on POSIX, where the wait is
    interruptible and every one of these tests passes either way. A platform-specific
    hazard that only one platform can demonstrate needs the guard on both.
    """
    import re

    body = code_only((ROOT / "src" / "optjournal" / "web.py").read_text(
        encoding="utf-8"))
    assert re.search(r"while not stop\.wait\(0?\.\d+\):", body), (
        "serve() waits on its stop event without a timeout. On Windows that parks "
        "the main thread where the console-control handler cannot run, so Ctrl+Break "
        "never stops the process -- loop on a short timeout instead"
    )
    assert "stop.wait()\n" not in body, "a bare stop.wait() is back"


#: How long the injected job sleeps. Comfortably past `Scheduler.stop()`'s 10s
#: join AND past `EXIT_BUDGET_S`, so a `serve` that waits for the job to finish
#: cannot pass this test by being quick.
_SLOW_JOB_S = 40

#: Bootstraps a `serve` whose scheduler has exactly one job, which sleeps.
#:
#: A SUBPROCESS WITH THE REGISTRY SWAPPED, rather than an in-process call, for the
#: reason the rest of this file is subprocesses: CPython delivers signals to the
#: main thread, and whether the main thread is reachable is a property of how the
#: process was started. Only the job's WORK is faked -- the scheduler, the tick, the
#: signal handler, the join and the listener are all the real ones.
#:
#: `_in_session` is forced open so due-ness does not depend on the wall clock. That
#: dependency is exactly what made this gap invisible: every CI run this repo had
#: ever done happened outside US market hours, so no test had ever signalled a
#: server with a job in flight.
_SLOW_SERVE = """
import time
from pathlib import Path

from optjournal import jobs, web

jobs._in_session = lambda now: True
jobs.JOBS = (
    jobs.Job(
        name="market",
        run=lambda conn, ctx: (time.sleep({sleep}), jobs.Outcome("ok", "slept"))[1],
        minute=0, hour=0, weekdays=(1, 2, 3, 4, 5, 6, 7), zone="UTC",
        catchup=jobs.Catchup.WINDOW, window_s=60, timeout_s=300,
    ),
)
web.serve(db_path=Path({db!r}), archive_dir=Path({archive!r}),
          port={port}, scheduler=True)
"""


def test_serve_exits_promptly_with_a_job_still_running(journal):
    """A BUSY SCHEDULER MUST NOT WEDGE THE STOP, and nothing asserted that.

    All three tests above signal an IDLE server: the demo fixture has no perishable
    bar windows and no confirm query, so the first tick finds nothing to do. The
    interesting case is the one that only happens during a trading session -- a tick
    mid-fetch when the signal arrives -- and this repo's CI had never once run inside
    US market hours, so it had never been exercised anywhere.

    That absence cost real debugging: when windows-latest started failing, "a job is
    in flight and eats the scheduler join" was the leading theory for a while,
    unfalsifiable because no test could produce the state. Now one can.

    `Scheduler.stop()` joins for 10s and then abandons the thread, which is safe
    because it is a daemon: the process exits and the OS reclaims it. The job here
    sleeps four times that, so a `serve` that waited for the work would blow the
    budget and fail.
    """
    port = _free_port()
    script = _SLOW_SERVE.format(
        sleep=_SLOW_JOB_S, db=str(journal),
        archive=str(journal.parent / "raw"), port=port,
    )
    proc = subprocess.Popen(  # noqa: S603 - a fixed argv, no shell
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        creationflags=(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if os.name == "nt" else 0
        ),
    )
    _wait_until_bound(proc, port)
    # Let the tick claim the job and enter its sleep, so the signal genuinely
    # arrives mid-work rather than before the scheduler has started anything.
    time.sleep(2)

    started = time.monotonic()
    _stop(proc)
    try:
        output = proc.communicate(timeout=EXIT_BUDGET_S)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        output = (proc.communicate()[0] or "").strip()
        pytest.fail(
            f"serve did not exit {EXIT_BUDGET_S}s after the stop signal while a job "
            f"was running. A busy tick must not hold the process open: the "
            f"scheduler's join is bounded and its thread is a daemon. Output:\n"
            f"{output}"
        )
    elapsed = time.monotonic() - started
    assert elapsed < EXIT_BUDGET_S, f"took {elapsed:.1f}s with a job in flight"
    # The scheduler's own stop line is a LOG record, which `serve` routes to
    # logs/, so stdout cannot carry it. The banner can, and it is what proves this
    # ran with a live scheduler rather than passing as an idle server would.
    assert "scheduler on" in output, (
        "this serve had no scheduler, so a job was never in flight and the test "
        f"proves nothing:\n{output}"
    )
    assert proc.returncode == 0, (
        f"serve exited {proc.returncode} with a job in flight, not 0 -- a supervisor "
        f"reads that as a crash and respawns:\n{output}"
    )
