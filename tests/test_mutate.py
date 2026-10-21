"""Tests for the harness that measures the other tests.

`mutate.py` is the one module whose output is a claim ABOUT the suite, so a defect
here does not break a feature -- it misstates how well everything else is guarded,
in the reassuring direction. That happened: a mutant that made a test HANG was
reported as `UNCAUGHT`, and the sentinel it hung was the guard keeping an
unauthenticated brokerage dashboard off a public interface. The survey said the
suite had no test there. The suite had four.

So the classification of a run into "measured" versus "not to be trusted" is worth
its own tests, and they are unit tests against a fake `CompletedProcess` rather
than real clone runs: the real thing takes ~25s per mutant, and what needs
asserting is the parsing, not pytest.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from optjournal import mutate
from optjournal.cli import EXIT_ERROR, EXIT_OK, main
from optjournal.mutate import (
    _SUITE_TIMEOUT_S,
    MUTANTS,
    MUTATION_TIMEOUT_RETURN_CODE,
    MutationOutcome,
    format_report,
    run_mutant,
)


def _outcome(**kw) -> MutationOutcome:
    return MutationOutcome(mutant=MUTANTS[0], **kw)


def _exit_code(monkeypatch, outcomes: list[MutationOutcome]) -> int:
    """What `optjournal mutate` exits with when the survey comes back as `outcomes`."""
    monkeypatch.setattr(mutate, "run_all", lambda **_: outcomes)
    return main(["mutate"])


def _fake_clone(tmp_path, mutant):
    """A tree with just enough shape for `run_mutant` to reach the suite run."""
    module = tmp_path / mutant.key / "src" / "optjournal" / mutant.module
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(mutant.find, encoding="utf-8")
    return module


def test_run_mutant_reports_a_timed_out_suite_as_hung(tmp_path, monkeypatch):
    """The defect this file exists for, driven through `run_mutant` itself.

    Asserting on a hand-built `MutationOutcome(status="hung")` would test nothing:
    the bug was that `run_mutant` never PRODUCED that status, so the branch has to
    be reached for real. `_pytest` is scripted -- a clean baseline, then the
    timeout path's exact return -- because the thing under test is the
    classification, not pytest.
    """
    mutant = MUTANTS[0]
    _fake_clone(tmp_path, mutant)
    calls = []

    def _scripted(clone, *args):
        # `_imports_the_clone` is stubbed out, so the only whole-suite runs are
        # the baseline and then the mutated one. Sequence, not heuristics: the
        # first is clean, the second is the timeout path's exact return.
        calls.append(args)
        if len(calls) == 1:
            return subprocess.CompletedProcess(args, 0, "1 passed", "")
        return subprocess.CompletedProcess(
            args, MUTATION_TIMEOUT_RETURN_CODE, "\nTIMEOUT after 500s", "",
        )

    monkeypatch.setattr(mutate, "_prepare", lambda source, clone: None)
    monkeypatch.setattr(mutate, "_imports_the_clone", lambda clone: True)
    monkeypatch.setattr(mutate, "_pytest", _scripted)

    outcome = run_mutant(mutant, source=tmp_path, workdir=tmp_path)

    assert outcome.status == "hung", (
        f"a killed suite was classed {outcome.status!r}; as 'measured' with zero "
        f"failures it reads as UNCAUGHT, which is the defect that called the "
        f"loopback security guard untested"
    )
    assert str(_SUITE_TIMEOUT_S) in outcome.detail


@pytest.mark.parametrize("ending", ["path-leak", "dirty-baseline", "stale-mutant",
                                    "measured", "interrupted"])
def test_the_clone_is_removed_however_the_run_ends(tmp_path, monkeypatch, ending):
    """A clone is a copy of the checkout, so one left in `$TMPDIR` is a copy of
    whatever the checkout held. Every way out of `run_mutant` removes it."""
    mutant = MUTANTS[0]
    module = _fake_clone(tmp_path, mutant)
    if ending == "stale-mutant":
        module.write_text("", encoding="utf-8")

    def _scripted(clone, *args):
        if ending == "interrupted":
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(
            args, 1 if ending == "dirty-baseline" else 0, "", "")

    monkeypatch.setattr(mutate, "_prepare", lambda source, clone: None)
    monkeypatch.setattr(mutate, "_imports_the_clone", lambda clone: ending != "path-leak")
    monkeypatch.setattr(mutate, "_pytest", _scripted)

    with contextlib.suppress(KeyboardInterrupt):
        outcome = run_mutant(mutant, source=tmp_path, workdir=tmp_path)
        assert outcome.status == ending
    assert not (tmp_path / mutant.key).exists(), f"the {ending} run left its clone"


def test_a_clone_carries_the_code_but_not_the_journal(tmp_path):
    """The suite runs on tracked fixtures, so a clone needs none of the journal
    beside the code: not the database, its backups, the statements or settings.
    Only the top level is the journal's; a `demo` inside a dependency is not."""
    source, clone = tmp_path / "source", tmp_path / "clone"
    for name in ("journal.db", "journal.db-wal", "journal-before-v12-20260909.db",
                 ".optjournal.json", "raw/activity-1.xml", "logs/optjournal.log",
                 "src/optjournal/x.py", "tests/fixtures/statements/activity-1.xml",
                 ".venv/lib/pkg/demo/x.py"):
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text("x", encoding="utf-8")

    mutate._prepare(source, clone)

    assert sorted(p.relative_to(clone).as_posix()
                  for p in clone.rglob("*") if p.is_file()) == [
        ".venv/lib/pkg/demo/x.py",
        "src/optjournal/x.py",
        "tests/fixtures/statements/activity-1.xml",
    ]


def test_a_killed_suite_fails_the_survey(monkeypatch):
    """A hang measured nothing, so a scripted run must not read it as a pass."""
    hung = _outcome(status="hung", detail="the suite did not finish")
    assert _exit_code(monkeypatch, [hung]) == EXIT_ERROR, (
        "a hung suite must make `optjournal mutate` exit 1"
    )


def test_the_report_shows_a_hung_suite_as_untrustworthy_not_as_a_count():
    """`!!` is the mark for "no number here is worth reading"."""
    text = format_report([_outcome(status="hung", detail="did not finish in 500s")])
    assert "!!" in text
    assert "UNCAUGHT" not in text
    assert "did not finish in 500s" in text


def test_a_measured_zero_is_still_reported_as_uncaught(monkeypatch):
    """The other direction: the UNCAUGHT signal must survive this change.

    Suppressing hangs would be worthless if it also suppressed the real finding of
    a defect that genuinely no test notices.
    """
    zero = _outcome(status="measured", failed=0)
    assert _exit_code(monkeypatch, [zero]) == EXIT_ERROR
    assert "UNCAUGHT" in format_report([zero])


def test_a_caught_mutant_reports_its_tests(monkeypatch):
    caught = _outcome(status="measured", failed=2,
                      tests=("tests/test_x.py::test_a", "tests/test_x.py::test_b"))
    assert _exit_code(monkeypatch, [caught]) == EXIT_OK
    text = format_report([caught])
    assert "2 test(s)" in text
    assert "tests/test_x.py::test_a" in text


def test_the_timeout_is_generous_against_the_real_suite():
    """A slow machine reporting "hung" would be worse than waiting.

    Pinned so that someone shortening this has to consider the clean suite's own
    runtime, which is ~155s.
    """
    assert _SUITE_TIMEOUT_S >= 300


def test_every_mutant_has_a_plain_english_consequence():
    """`breaks` is what a reader sees. An empty one makes the survey unreadable."""
    for mutant in MUTANTS:
        assert mutant.breaks.strip(), f"{mutant.key} has no stated consequence"
        assert mutant.find != mutant.replace, f"{mutant.key} is a no-op"


def test_mutant_keys_are_unique():
    keys = [m.key for m in MUTANTS]
    assert len(keys) == len(set(keys)), "a duplicate key overwrites a result"


def _counting_run_all(monkeypatch, jobs):
    """`run_all` with `run_mutant` stubbed, recording concurrency and order.

    The real thing spends ~106s per mutant in two pytest runs; what needs
    asserting is that `jobs` fans out and that the report still reads in
    registry order, neither of which involves pytest.
    """
    import threading

    live, peak, seen = 0, 0, []
    guard = threading.Lock()

    def _fake(mutant, *, source, workdir):
        nonlocal live, peak
        with guard:
            live += 1
            peak = max(peak, live)
        # Long enough that a serial run cannot fake a peak above one.
        time.sleep(0.05)
        with guard:
            live -= 1
            seen.append(mutant.key)
        return MutationOutcome(mutant, "measured", failed=1)

    monkeypatch.setattr(mutate, "run_mutant", _fake)
    outcomes = mutate.run_all(
        source=Path("nonexistent"),
        workdir=Path(tempfile.gettempdir()) / "optjournal-test-mutants",
        jobs=jobs,
    )
    return outcomes, peak, seen


def test_jobs_runs_mutants_concurrently_and_serial_stays_the_default(monkeypatch):
    """`--jobs N` must actually overlap, and 1 must not.

    Safe to parallelise for a structural reason rather than a hopeful one: each
    mutant already gets its own clone, interpreter and database, because that
    isolation is what makes the measurement trustworthy. This pins that the fan-out
    exists at all -- a `jobs` argument silently ignored would look like a 4x
    speedup that never happened, and the surveyed NUMBERS would still be right, so
    nothing else would notice.
    """
    _, serial_peak, _ = _counting_run_all(monkeypatch, jobs=1)
    assert serial_peak == 1, f"jobs=1 overlapped {serial_peak} mutants"

    _, parallel_peak, _ = _counting_run_all(monkeypatch, jobs=4)
    assert parallel_peak > 1, (
        "jobs=4 ran one at a time, so the flag is decorative and a survey takes "
        "as long as it always did"
    )


@pytest.mark.parametrize("jobs", [0, -1])
def test_a_nonsense_job_count_runs_serially_rather_than_raising(monkeypatch, jobs):
    """`--jobs 0` is a typo, not a request for zero work.

    `ThreadPoolExecutor(max_workers=0)` raises, so the guard is `jobs <= 1` rather
    than a truthiness check -- and a survey that crashes on a mistyped flag after
    someone waited for it is worse than one that ignores the flag.
    """
    outcomes, peak, _ = _counting_run_all(monkeypatch, jobs=jobs)
    assert len(outcomes) == len(MUTANTS) and peak == 1


def test_a_parallel_report_still_reads_in_registry_order(monkeypatch):
    """Order is the reader's index into `MUTANTS`, so completion order must not leak.

    `ThreadPoolExecutor.map` preserves input order; `as_completed` would not. If a
    report were sorted by whichever suite finished first, two runs of the same
    registry would print different reports and neither would be wrong -- which
    makes them impossible to diff.
    """
    outcomes, peak, seen = _counting_run_all(monkeypatch, jobs=4)
    assert peak > 1, "not actually parallel, so this proves nothing"
    assert [o.mutant.key for o in outcomes] == [m.key for m in MUTANTS]
    # And completion order genuinely differed from registry order, or the
    # assertion above would hold trivially.
    assert seen != [m.key for m in MUTANTS] or len(MUTANTS) < 2


_POSIX_STAND_IN = pytest.mark.skipif(
    sys.platform == "win32", reason="the stand-in venv is a POSIX script")

#: A suite that blocks the way `loopback`'s does: it starts a server of its own,
#: then never finishes. It writes both pids, so a test can see whether each died.
_BLOCKING_SUITE = """\
import os, subprocess, sys, time
from pathlib import Path

def test_blocks():
    server = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    Path("pids").write_text(f"{os.getpid()} {server.pid}")
    time.sleep(30)
"""


def _stand_in_clone(tmp_path, suite: str) -> Path:
    """A clone whose `.venv` python is this interpreter, holding one test file.

    The stand-in execs this interpreter by its own path. A symlink to it is
    started as the base install it links to, outside this venv, and passed
    only where that install had pytest of its own."""
    clone = tmp_path / "clone"
    (clone / ".venv" / "bin").mkdir(parents=True)
    stand_in = clone / ".venv" / "bin" / "python"
    stand_in.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    stand_in.chmod(0o755)
    (clone / "test_one.py").write_text(suite, encoding="utf-8")
    return clone


def _pids(clone: Path) -> tuple[int, int]:
    deadline = time.monotonic() + 10
    while not (clone / "pids").exists():
        assert time.monotonic() < deadline, "the blocking suite never started"
        time.sleep(0.05)
    pytest_pid, server_pid = map(int, (clone / "pids").read_text().split())
    return pytest_pid, server_pid


def _gone(pid: int) -> bool:
    """Whether `pid` has exited, allowing its new parent a moment to reap it."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@_POSIX_STAND_IN
def test_a_suite_that_outlives_the_timeout_is_killed_with_its_server(
    tmp_path, monkeypatch,
):
    """The real timeout branch, against a suite that really blocks.

    The server matters as much as pytest: one that outlives its suite goes on
    holding the port it bound, which under `loopback` is the real app's.
    """
    clone = _stand_in_clone(tmp_path, _BLOCKING_SUITE)
    monkeypatch.setattr(mutate, "_SUITE_TIMEOUT_S", 2)

    result = mutate._pytest(clone)

    assert result.returncode == MUTATION_TIMEOUT_RETURN_CODE, result.stdout
    assert not any(
        line.startswith(("FAILED ", "ERROR ")) for line in result.stdout.splitlines()
    ), "a killed suite must leave no failure lines to count"
    pytest_pid, server_pid = _pids(clone)
    assert _gone(pytest_pid), "the timed-out pytest is still running"
    assert _gone(server_pid), "the timed-out suite's server is still running"


@_POSIX_STAND_IN
def test_an_interrupted_survey_takes_the_running_suite_with_it(tmp_path, monkeypatch):
    """Ctrl-C reaches the harness, not the suite: the suite runs in its own
    session so the timeout can kill its whole group, which also puts it beyond
    the terminal's signal. So the harness has to kill it on the way out."""
    clone = _stand_in_clone(tmp_path, _BLOCKING_SUITE)
    started: list[subprocess.Popen[str]] = []

    class _Interrupted(subprocess.Popen):
        def communicate(self, input=None, timeout=None):
            started.append(self)
            _pids(clone)
            raise KeyboardInterrupt

    monkeypatch.setattr(mutate.subprocess, "Popen", _Interrupted)
    with pytest.raises(KeyboardInterrupt):
        mutate._pytest(clone)

    _, server_pid = _pids(clone)
    assert _gone(server_pid), "the interrupted suite's server is still running"
    assert started[0].wait(timeout=5) == -signal.SIGKILL


@_POSIX_STAND_IN
def test_a_forced_colour_terminal_still_reports_failures_as_failed(tmp_path, monkeypatch):
    """`run_mutant` counts lines that START with "FAILED " or "ERROR ". With
    FORCE_COLOR set (some terminals and CI runners set it), pytest wrapped those
    words in colour codes, nothing matched, and every mutant read as uncaught.
    Run for real against a one-test suite that fails."""
    clone = _stand_in_clone(tmp_path, "def test_fails():\n    assert False\n")
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("PY_COLORS", "1")

    result = mutate._pytest(clone)

    assert any(line.startswith("FAILED ") for line in result.stdout.splitlines()), (
        result.stdout
    )


def test_the_clone_is_told_it_is_a_copy(tmp_path, monkeypatch):
    """The wire between this harness and `conftest._is_copy`.

    Two tests pin the ORIGINAL checkout's absolute paths -- the launchd plist
    execs this checkout's console script, and `raw/` holds the real archive --
    and both are right to. Neither can hold in a clone, so both skip there, and
    they skip on `mutate.CLONE_ENV` because a `copytree` of the whole checkout
    (`.git` directory and all) cannot tell it is a copy any other way.

    Asserted on the env the harness actually builds rather than on the skip
    itself, because a skip that stops firing looks like a passing suite. When
    inference was the mechanism instead, the plist test ran in every clone and
    failed, so the baseline was never green and every mutant came back
    `dirty-baseline` -- with the tool still printing its summary line.

    The environment is stripped deliberately (see `_pytest`), which is exactly
    why the marker has to be added back explicitly.
    """
    seen = {}

    class _Popen:
        def __init__(self, args, **kwargs):
            seen.update(kwargs.get("env") or {})
            self.args, self.pid = args, 0

        def communicate(self, timeout=None):
            return "", ""

        returncode = 0

    monkeypatch.setattr(mutate.subprocess, "Popen", _Popen)
    mutate._pytest(tmp_path)

    assert seen.get(mutate.CLONE_ENV), (
        f"the clone's suite is not told it is a copy; env was {sorted(seen)}"
    )


def test_the_developer_tools_run_by_name_but_stay_out_of_the_help():
    """`optjournal --help` is the list a friend reads, and neither tool is for them."""
    from optjournal.cli import build_parser, cmd_mutate, cmd_sweep

    parser = build_parser()
    listing = parser.format_help()
    assert "mutate" not in listing and "sweep" not in listing, listing
    assert "SUPPRESS" not in listing, listing
    assert parser.parse_args(["mutate", "--only", "fee-ccy"]).func is cmd_mutate
    assert parser.parse_args(["sweep"]).func is cmd_sweep
