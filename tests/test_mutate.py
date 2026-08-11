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

import signal
import subprocess

from optjournal import mutate
from optjournal.mutate import (
    _SUITE_TIMEOUT_S,
    MUTANTS,
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
            args, -signal.SIGKILL, "\nTIMEOUT after 500s", "",
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


def test_a_sigkill_return_code_is_what_the_runner_signals():
    """Guards the contract between `_pytest`'s timeout path and `run_mutant`.

    `run_mutant` recognises a hang by `returncode == -signal.SIGKILL`. If the
    timeout path is ever changed to return something else, the two halves stop
    agreeing silently and hangs become measured zeros again.
    """
    killed = subprocess.CompletedProcess(
        args=[], returncode=-signal.SIGKILL, stdout="\nTIMEOUT after 500s", stderr="",
    )
    assert killed.returncode == -signal.SIGKILL
    assert not any(
        line.startswith(("FAILED ", "ERROR "))
        for line in killed.stdout.splitlines()
    ), "the premise: a killed suite leaves no failure lines to count"
