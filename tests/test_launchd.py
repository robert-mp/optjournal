"""The launchd agent and the rotating log: two halves of "it stays running".

WHY THE PLIST IS TESTED AT ALL. It is data, so nothing imports it and nothing would
notice it going stale -- and its contents are ABSOLUTE PATHS, which is launchd's
design rather than a choice. Move the repo and the agent keeps pointing at the old
location; the service then either fails to exec or, worse, supervises a stale
checkout while the developer edits a different one. That failure is exactly the
shape this project keeps finding: a value that is well-formed and no longer true.

The move already happened once (`~/.meshclaw/workspace/optjournal` -> `~/optjournal`,
plan step 2), which is why this is a test and not a comment.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest
from conftest import ROOT, skip_if_copy

PLIST = ROOT / "launchd" / "com.optjournal.serve.plist"
pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="launchd integration is macOS-specific",
)


@pytest.fixture(scope="module")
def agent() -> dict:
    if not PLIST.is_file():
        pytest.fail(f"{PLIST} is missing; step 7 of SCHEDULER_PLAN.md adds it")
    with PLIST.open("rb") as handle:
        return plistlib.load(handle)


def test_the_plist_is_valid_and_labelled(agent):
    """A malformed plist fails at `launchctl bootstrap` with a terse message.

    Parsed with `plistlib` rather than grepped, so a missing `</dict>` is caught
    here rather than by a human reading launchctl's output.

    AND `plutil -lint` IS NOT ENOUGH, which this test found on its first run. The
    plist's comments originally used `--` as an em-dash substitute, which XML
    forbids inside a comment; `plutil -lint` reported `OK` while Python's expat
    parser rejected line 7. So the tool a reader would reach for is more lenient
    than the tool launchd uses, and only a strict parse catches it.
    """
    assert agent["Label"] == "com.optjournal.serve"


@skip_if_copy
def test_every_path_in_the_plist_points_at_this_checkout(agent):
    """THE reason this file exists: absolute paths rot when the repo moves.

    Checked against `conftest.ROOT`, so the test travels with the checkout. The
    log directory is exempt from "must exist" because launchd creates neither -- it
    is asserted to be INSIDE the checkout instead, which is what makes it findable.

    Skipped in a COPY of the checkout, where it cannot hold and would be wrong to:
    the plist still points at the original, which is correct -- launchd supervises
    the real install, not a worktree or a mutation clone. Without the skip this was
    the test that made `optjournal mutate` unable to get a green baseline, so every
    mutant came back uncounted. See `conftest.skip_if_copy`.
    """
    program = agent["ProgramArguments"][0]
    assert Path(program) == ROOT / ".venv" / "bin" / "optjournal", (
        f"the plist execs {program}, which is not this checkout's console script. "
        "launchd would supervise a different (or missing) install."
    )
    assert Path(agent["WorkingDirectory"]) == ROOT, (
        f"WorkingDirectory is {agent['WorkingDirectory']}, not {ROOT}"
    )
    for key in ("StandardOutPath", "StandardErrorPath"):
        target = Path(agent[key])
        assert ROOT in target.parents, (
            f"{key} is {target}, outside this checkout -- the log would not be "
            "beside the journal it describes"
        )


def test_the_plist_supervises_the_process_and_never_a_schedule(agent):
    """launchd keeps `serve` alive; `jobs.JOBS` decides when anything runs.

    A `StartCalendarInterval` here would put the schedule back into an
    unversioned side channel -- the exact arrangement this plan replaces, where
    four crons were registered by hand and the one that mattered was never
    registered at all.
    """
    assert agent["RunAtLoad"] is True
    assert "StartCalendarInterval" not in agent, (
        "the plist carries a schedule. The schedule is jobs.JOBS, in code, where a "
        "test can read it -- see SCHEDULER_PLAN.md on why the side channel went."
    )
    assert "StartInterval" not in agent, "same: no schedule in the plist"


def test_a_deliberate_stop_is_not_respawned_into(agent):
    """`KeepAlive: {SuccessfulExit: false}`, not `KeepAlive: true`.

    With a bare `true`, `launchctl bootout` -- which sends SIGTERM, so `serve`
    exits 0 -- would be immediately respawned, and stopping the service by hand
    would be impossible. With the dict, a clean exit stays stopped and a crash is
    restarted, which is the distinction that makes the supervision usable.
    """
    keep = agent["KeepAlive"]
    assert isinstance(keep, dict), (
        "KeepAlive is a bare boolean, so `launchctl bootout` would respawn the "
        "service it just stopped"
    )
    assert keep["SuccessfulExit"] is False


def test_the_agent_passes_a_query_id_so_the_sync_job_can_run(agent):
    """A LAUNCHD AGENT INHERITS NO SHELL ENVIRONMENT, and that is the whole bug.

    This plist deliberately carries no schedule -- the schedule is `jobs.JOBS` --
    which makes the supervised `serve` the process that runs `sync`. But `serve`
    was launched here with no query id, and launchd does not see the
    `$OPTJOURNAL_QUERY_ID` a developer exports in a terminal. So the ledger
    recorded `failed -- no Flex query id configured` on every due tick while
    `optjournal sync` run by hand worked, and the journal quietly stopped
    collecting: exactly the "looks fine, does nothing" failure the rest of this
    file exists to catch.

    Asserted on the ARGUMENTS rather than on `EnvironmentVariables`, because the
    flag is the channel that does not depend on which shell last exported what.
    `cmd_serve` reads the variable as a fallback for a hand-run serve; the agent
    should not need it.
    """
    args = agent["ProgramArguments"]
    assert "serve" in args, "this plist no longer supervises `serve`"
    assert "--query-id" in args, (
        "the supervised `serve` gets no query id, so the scheduled `sync` job "
        "fails on every tick -- see jobs._sync. launchd inherits no shell, so "
        "exporting $OPTJOURNAL_QUERY_ID does not reach it."
    )
    value = args[args.index("--query-id") + 1]
    assert value.isdigit(), f"--query-id is {value!r}, not a Flex query id"


def test_the_agent_runs_python_unbuffered(agent):
    """MEASURED, and it is the difference between a log and an empty file.

    `print()` to a FILE is block-buffered, and launchd's StandardOutPath IS a file.
    Measured on this machine: 0 bytes after 1.2s of output without the variable, 32
    bytes with it. A supervised service whose log is empty is indistinguishable
    from one that never started -- and "looks fine, does nothing" is the failure
    this whole plan exists to end.
    """
    env = agent.get("EnvironmentVariables", {})
    assert env.get("PYTHONUNBUFFERED") == "1", (
        "PYTHONUNBUFFERED is not set, so the startup banner and any traceback sit "
        "in a buffer and the log file reads as empty while the service runs"
    )


def test_a_crash_loop_is_throttled_rather_than_silent(agent):
    """A bind failure exits non-zero, and `KeepAlive` would respawn it forever.

    `cli.cmd_serve` already exits `EXIT_ERROR` loudly on `OSError`, so with a
    throttle the failure is one line every ten seconds in a log a human can read.
    Two stale `serve` processes were found live during the surveys that produced
    this plan, which is what an unthrottled invisible respawn looks like.
    """
    assert agent.get("ThrottleInterval", 10) >= 10


def test_the_plist_documents_how_to_install_and_stop_it():
    """Prose, because the commands are not guessable and getting them wrong is
    the difference between a running agent and a silent one.

    `launchctl bootstrap`/`bootout` (the modern spelling) rather than
    `load`/`unload`: the latter are deprecated and fail confusingly under a user
    domain on current macOS.
    """
    text = PLIST.read_text()
    for command in ("launchctl bootstrap", "launchctl bootout", "launchctl print"):
        assert command in text, f"the plist does not say how to {command.split()[-1]}"
    assert "load " not in text.replace("bootstrap", ""), (
        "the deprecated launchctl load/unload spelling is being suggested"
    )


# --------------------------------------------------------------------------
# The rotating log
# --------------------------------------------------------------------------


def test_the_log_rotates_because_macos_will_not(tmp_path):
    """`newsyslog` only touches files in `/etc/newsyslog.conf`, so nothing in a
    user's home directory is ever rotated. A scheduler logging every tick would
    otherwise append to one file forever.

    Driven with a tiny cap rather than by writing 2 MB: the question is whether
    rotation is CONFIGURED, not whether Python's handler works.
    """
    import logging  # noqa: PLC0415 - local to this test

    from optjournal import logs  # noqa: PLC0415 - local to this test

    target = logs.configure(tmp_path)
    assert target == logs.log_path(tmp_path)
    assert target is not None and target.parent.is_dir()

    handler = next(
        h for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
        and Path(h.baseFilename) == target
    )
    try:
        assert handler.maxBytes > 0, (
            "the handler has no size cap, so it never rotates -- which is the "
            "same as not having one on macOS"
        )
        assert handler.backupCount > 0, "rotation keeps no history"
        # It really writes.
        logging.getLogger("optjournal.test").info("a line for the log")
        handler.flush()
        assert "a line for the log" in target.read_text()
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()


def test_configuring_the_log_twice_does_not_duplicate_every_line(tmp_path):
    """`serve` is callable more than once in a process -- the suite does it
    routinely -- and stacked handlers write every line N times, which corrupts
    the one artefact a reader trusts when something has gone wrong.
    """
    import logging  # noqa: PLC0415 - local to this test

    from optjournal import logs  # noqa: PLC0415 - local to this test

    target = logs.configure(tmp_path)
    logs.configure(tmp_path)
    handlers = [
        h for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
        and Path(h.baseFilename) == target
    ]
    try:
        assert len(handlers) == 1, f"{len(handlers)} handlers on one file"
    finally:
        for handler in handlers:
            logging.getLogger().removeHandler(handler)
            handler.close()


def test_a_log_that_cannot_be_opened_does_not_stop_the_journal(tmp_path):
    """Observability must not break the thing it observes.

    A journal whose log directory is unwritable must still serve. Provoked with a
    FILE where the directory should be, which is the bluntest available version of
    "mkdir failed".
    """
    from optjournal import logs  # noqa: PLC0415 - local to this test

    (tmp_path / logs.LOG_DIR).write_text("not a directory")
    assert logs.configure(tmp_path) is None, (
        "an unopenable log raised instead of returning None, so a journal with an "
        "unwritable directory would refuse to start"
    )


def test_a_running_scheduler_leaves_evidence_in_the_log(tmp_path, monkeypatch):
    """FOUND BY RUNNING IT: the log was zero bytes with a live scheduler.

    `reconcile` logs only when something is DUE, and on a quiet tick nothing is --
    so a supervised `serve` wrote a log file of exactly 0 bytes while working
    perfectly. A log that is empty because all is well is indistinguishable from a
    log that is empty because the loop is dead, which is this project's signature
    failure. Reintroducing it in the logging added to prevent it would be absurd.

    The heartbeat in `job_state` is the machine-readable answer to "is the loop
    alive"; these two lines are the human-readable one.
    """
    import logging  # noqa: PLC0415 - local to this test

    from optjournal import jobs, logs  # noqa: PLC0415 - local to this test

    target = logs.configure(tmp_path)
    assert target is not None
    handler = next(
        h for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
        and Path(h.baseFilename) == target
    )
    clock = jobs.Scheduler(
        ctx=jobs.Context(archive_dir=tmp_path / "raw", db_path=tmp_path / "q.db"),
        tick_s=30,                       # so no tick fires during the test
    )
    try:
        clock.start()
        clock.stop()
        handler.flush()
        written = target.read_text()
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()

    assert "scheduler starting" in written, (
        "a scheduler that starts leaves no trace in the log, so an empty log "
        "cannot be told from a dead loop"
    )
    assert "scheduler stopping" in written, (
        "a scheduler that stops leaves no trace, so a log ending mid-stream cannot "
        "be told from a crash"
    )
    assert "4 job(s)" in written, (
        "the startup line does not say WHICH jobs are registered -- the one thing "
        "the crons.json arrangement could never tell anyone"
    )
