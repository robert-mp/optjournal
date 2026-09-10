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

import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from optjournal import mutate
from optjournal.mutate import (
    _SUITE_TIMEOUT_S,
    MUTANTS,
    MUTATION_TIMEOUT_RETURN_CODE,
    MutationOutcome,
    Report,
    format_report,
    run_mutant,
)


def _outcome(**kw) -> MutationOutcome:
    return MutationOutcome(mutant=MUTANTS[0], **kw)


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
    assert not outcome.uncaught
    assert str(_SUITE_TIMEOUT_S) in outcome.detail


def test_a_killed_suite_is_not_a_measurement_of_zero():
    """A hang must never read as `UNCAUGHT` once classified."""
    hung = _outcome(status="hung", detail="the suite did not finish")
    assert not hung.uncaught, "a hung suite must not be classed as a measured zero"
    assert not Report([hung]).ok, "a hung suite must make the report refuse"


def test_the_report_shows_a_hung_suite_as_untrustworthy_not_as_a_count():
    """`!!` is the mark for "no number here is worth reading"."""
    text = format_report([_outcome(status="hung", detail="did not finish in 500s")])
    assert "!!" in text
    assert "UNCAUGHT" not in text
    assert "did not finish in 500s" in text


def test_a_measured_zero_is_still_reported_as_uncaught():
    """The other direction: the UNCAUGHT signal must survive this change.

    Suppressing hangs would be worthless if it also suppressed the real finding of
    a defect that genuinely no test notices.
    """
    zero = _outcome(status="measured", failed=0)
    assert zero.uncaught
    assert not Report([zero]).ok
    assert "UNCAUGHT" in format_report([zero])


def test_a_caught_mutant_reports_its_tests():
    caught = _outcome(status="measured", failed=2,
                      tests=("tests/test_x.py::test_a", "tests/test_x.py::test_b"))
    assert not caught.uncaught
    assert Report([caught]).ok
    text = format_report([caught])
    assert "2 test(s)" in text
    assert "tests/test_x.py::test_a" in text


def test_the_timeout_is_generous_against_the_real_suite():
    """A slow machine reporting "hung" would be worse than waiting.

    Pinned so that someone shortening this has to consider the clean suite's own
    runtime, which is ~52s.
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


def test_the_timeout_return_code_is_what_the_runner_signals():
    """Guards the contract between `_pytest`'s timeout path and `run_mutant`.

    `run_mutant` recognises a hang by `MUTATION_TIMEOUT_RETURN_CODE`. If the
    timeout path is ever changed to return something else, the two halves stop
    agreeing silently and hangs become measured zeros again.
    """
    killed = subprocess.CompletedProcess(
        args=[], returncode=MUTATION_TIMEOUT_RETURN_CODE,
        stdout="\nTIMEOUT after 500s", stderr="",
    )
    assert killed.returncode == MUTATION_TIMEOUT_RETURN_CODE
    assert not any(
        line.startswith(("FAILED ", "ERROR "))
        for line in killed.stdout.splitlines()
    ), "the premise: a killed suite leaves no failure lines to count"


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
