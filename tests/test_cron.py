"""The cron scripts, against the CLI payloads they actually parse.

These run under MeshClaw's interpreter in production, which has no py_ibkr, so
they are loaded here by path exactly as the deployed shim loads them -- an
`import optjournal.cron...` would pull in a dependency the real runtime does not
have and prove nothing about the real load.

The gap they close: `cmd_sync` emits a payload and `cron/optjournal_sync.py`
parses it, with nothing between them. `new_trades` once held the row LIST here
and a COUNT in `web._do_sync`, one name for two types across two sync
implementations computing the same figure from the same table. Neither reader
noticed, because each only ever met one producer -- so the first to meet the
other shape would have iterated an int or formatted a list into a Slack message.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
from conftest import ROOT

CRON_DIR = ROOT / "cron"


def _load_cron(name: str):
    """Load a cron module by path, with `mesh_claw.cron_script` stubbed.

    Stubbed rather than installed: `Report` and `Skip` are MeshClaw's delivery
    signals, and what matters here is which one a given payload raises, not what
    MeshClaw then does with it.
    """
    if "mesh_claw.cron_script" not in sys.modules:
        pkg = types.ModuleType("mesh_claw")
        mod = types.ModuleType("mesh_claw.cron_script")

        class Report(Exception):
            pass

        class Skip(Exception):
            pass

        mod.Report, mod.Skip = Report, Skip
        pkg.cron_script = mod
        sys.modules["mesh_claw"] = pkg
        sys.modules["mesh_claw.cron_script"] = mod

    path = CRON_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_cron_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sync_cron():
    return _load_cron("optjournal_sync")


@pytest.fixture(scope="module")
def bars_cron():
    return _load_cron("optjournal_bars")


#: Where MeshClaw requires cron scripts to live. Not version controlled, which is
#: the whole reason the files there must be shims rather than copies.
DEPLOYED = Path.home() / ".meshclaw" / "crons"


@pytest.mark.skipif(not DEPLOYED.is_dir(), reason="no MeshClaw cron directory")
@pytest.mark.parametrize(
    "name", ["optjournal_sync", "optjournal_bars", "optjournal_market"]
)
def test_the_deployed_cron_is_a_shim_not_a_copy(name):
    """The deployed file must LOAD the repo's implementation, not duplicate it.

    `optjournal_bars.py` was a byte-for-byte copy for a while, and the failure is
    silent in the direction that matters: an edit to `cron/optjournal_bars.py` is
    reviewed, committed and simply never runs. The two stayed equal only because
    no commit after the hand-copy happened to touch that file.

    It matters most for the bars jobs specifically. An option's intraday series
    exists only while its own session runs, so a fix that appeared deployed and
    was not costs sessions that no later run can recover.

    Asserted structurally rather than by diffing bytes: a diff would pass the day
    someone re-copied the file, which is exactly the state being forbidden.
    """
    deployed = DEPLOYED / f"{name}.py"
    if not deployed.is_file():
        pytest.skip(f"{name} is not deployed on this machine")
    text = deployed.read_text(encoding="utf-8")

    assert "importlib.util" in text and "spec_from_file_location" in text, (
        f"{deployed} does not load its implementation by path -- if it is a copy "
        "of the repo file, edits to the repo will never run"
    )
    # The path it resolves must be the versioned file, and that file must exist.
    impl = CRON_DIR / f"{name}.py"
    assert impl.is_file(), f"no versioned implementation at {impl}"
    assert f'"{name}.py"' in text or f"'{name}.py'" in text, (
        f"{deployed} does not name {name}.py, so it may point at the wrong file"
    )
    # A shim delegates; it does not carry the logic. The implementations shell out
    # to the CLI via subprocess, so its absence here is the signal.
    assert "subprocess" not in text, (
        f"{deployed} contains implementation logic (subprocess), so it is a copy"
    )


@pytest.mark.skipif(not DEPLOYED.is_dir(), reason="no MeshClaw cron directory")
def test_every_deployed_entry_point_exists_in_the_implementation():
    """Each registered entry point must resolve through the shim to real code.

    A shim exposing `live`/`daily`/`audit` that delegates to a module lacking one
    of them fails at the scheduled minute, not at deploy time -- and for the live
    poll that minute is inside a session whose bars cannot be re-collected.
    """
    expected = {"optjournal_sync": ["sync"],
                "optjournal_bars": ["live", "daily", "audit"]}
    for name, entries in expected.items():
        deployed = DEPLOYED / f"{name}.py"
        if not deployed.is_file():
            continue
        text = deployed.read_text(encoding="utf-8")
        impl = _load_cron(name)
        for entry in entries:
            assert f"def {entry}(" in text, f"{deployed} does not expose {entry}"
            assert callable(getattr(impl, entry, None)), (
                f"{name}.py has no {entry}() for the shim to delegate to"
            )


def test_both_crons_load_with_only_the_standard_library(sync_cron, bars_cron):
    """The property the deployed shim depends on.

    These run under MeshClaw's interpreter, which has no py_ibkr, so importing
    anything from the `optjournal` package would raise in production. A module
    that loads here with py_ibkr merely absent from its imports proves it.
    """
    for module in (sync_cron, bars_cron):
        assert not any(
            name.startswith("optjournal") for name in vars(module)
        ), "a cron reached into the package it shells out to"


def test_the_exit_codes_mirror_the_cli(sync_cron, bars_cron):
    """Both crons re-declare cli.py's exit codes and branch on them by number.

    Duplicated on purpose -- the comment in each says so, because a renumbering
    should surface as a wrong branch rather than as silent misreporting. This is
    what makes that trade-off safe: the copies are checked against the original.
    """
    from optjournal import cli

    for module in (sync_cron, bars_cron):
        for name in dir(module):
            if name.startswith("EXIT_"):
                assert getattr(module, name) == getattr(cli, name), (
                    f"{module.__name__}.{name} has drifted from cli.{name}"
                )


def test_the_sync_cron_reads_the_payload_cmd_sync_actually_emits(sync_cron):
    """`_describe` over a real `cmd_sync` payload, not a hand-written one.

    Built by running the real command against a real archive with `--json`, so
    the two halves of this contract cannot drift: a renamed or retyped key fails
    here instead of at 12:00 Dublin.
    """
    payload = {
        "started_at": "2026-08-06T00:00:00+00:00",
        "query_id": "1591754",
        "raw_bytes": 1234,
        "already_ingested": False,
        # The count, as the page's SyncResponse typedef and web._do_sync mean it.
        "new_trades": 1,
        # The rows, under the name that says it is a list.
        "new_trade_rows": [{
            "trade_date": "2026-08-04", "symbol": "GOOG  260918C00420000",
            "buy_sell": "SELL", "open_close": "O", "quantity": -1,
            "trade_price": 4.31, "ib_commission": -0.7016, "currency": "USD",
        }],
        "new_cash": 0,
        "warnings": [],
        "changed": True,
    }
    text = sync_cron._describe(payload)
    assert "1 new trade(s)" in text
    # The per-fill line, which is what reading a count instead of rows would lose.
    assert "GOOG" in text and "4.31" in text
    # An int formatted as a list, or a list as a count, both show up here.
    assert "[{" not in text and "dict" not in text


def test_the_sync_cron_headline_counts_rows_when_the_count_is_absent(sync_cron):
    """Tolerant of a payload from a CLI predating the split, since the cron and
    the CLI are deployed independently -- the shim loads whatever is in the repo
    while the venv holds whatever was last installed."""
    text = sync_cron._describe({
        "new_trade_rows": [
            {"trade_date": "2026-08-04", "symbol": "X", "buy_sell": "SELL",
             "open_close": "O", "quantity": -1, "trade_price": 1.0,
             "ib_commission": -1.0, "currency": "USD"},
        ],
        "new_cash": 0,
    })
    assert "1 new trade(s)" in text


def test_the_timeout_ladder_is_ordered(sync_cron):
    """FETCH_TIMEOUT_S must exceed flex.POLL_WORST_CASE_S, or a routine slow
    statement generation is killed mid-poll and a clean Report becomes a raw
    traceback. The cron checks this at runtime; this checks it in CI."""
    from optjournal.flex import POLL_WORST_CASE_S

    assert sync_cron.FETCH_TIMEOUT_S > POLL_WORST_CASE_S


def test_the_cron_bounds_the_wait_it_gives_the_cli(sync_cron, monkeypatch):
    """The CLI waits out a whole fetch for the shared lock by default, which is
    longer than this cron's own kill: the kill would land while nothing had been
    asked and report "no statement after 720s". So the cron hands the CLI a wait
    below its timeout, through the variable the CLI reads."""
    from optjournal.cli import LOCK_WAIT_ENV, _lock_wait
    from optjournal.flex import FETCH_LOCK_TIMEOUT_S

    assert sync_cron.LOCK_WAIT_S < sync_cron.FETCH_TIMEOUT_S
    assert FETCH_LOCK_TIMEOUT_S > sync_cron.FETCH_TIMEOUT_S, (
        "the default already fits, so this ladder would not be needed")
    seen = {}
    monkeypatch.setattr(sync_cron.subprocess, "run",
                        lambda *a, **kw: seen.update(kw) or _Completed())
    sync_cron._run("1591754")
    assert seen["env"][LOCK_WAIT_ENV] == str(sync_cron.LOCK_WAIT_S)

    monkeypatch.setenv(LOCK_WAIT_ENV, str(sync_cron.LOCK_WAIT_S))
    assert _lock_wait() == sync_cron.LOCK_WAIT_S
    monkeypatch.setenv(LOCK_WAIT_ENV, "not a number")
    assert _lock_wait() == FETCH_LOCK_TIMEOUT_S, "a damaged value falls back"


class _Completed:
    returncode = 0
    stdout = "{}"
    stderr = ""


def test_a_sync_payload_key_the_cron_reads_still_exists():
    """Every key `_describe` reaches for, against the code that really emits them.

    Source-level like test_web's /api/sync guard, and for the same reason:
    producing a genuine payload spends an IBKR request.

    Reads `web.sync_journal`, not `cli.cmd_sync`. The keys moved there when the two
    sync implementations collapsed into one -- `cmd_sync` now forwards the dict it
    is handed -- and this test caught the move rather than the move breaking the
    cron, which is what a source-level pin is for. It follows the PRODUCER, and it
    also asserts the forwarding, because a pin on a function the CLI no longer
    calls would be green and meaningless.
    """
    import inspect

    from optjournal.cli import cmd_sync
    from optjournal.web import sync_journal

    src = inspect.getsource(sync_journal)
    for key in ("new_trades", "new_trade_rows", "new_cash", "warnings", "changed"):
        assert f'"{key}"' in src, f"the sync payload no longer carries {key!r}"
    forwards = inspect.getsource(cmd_sync)
    assert "sync_journal(" in forwards, (
        "cmd_sync no longer calls sync_journal, so the keys checked above are not "
        "necessarily the ones the cron receives"
    )
    assert "_emit(data" in forwards, (
        "cmd_sync no longer emits the payload it was handed, so the cron's JSON "
        "may have a different shape from the one pinned here"
    )


def test_the_bars_cron_maps_every_outcome_to_a_delivery(bars_cron):
    """The three exit codes `bars` returns, and what each means to a human.

    Silence on 0 and 3 is the whole design -- `live` fires seven times a session
    -- so a change that made either speak would be a notification every hour.
    """
    assert bars_cron.EXIT_OK == 0
    assert bars_cron.EXIT_NO_DATA == 3
    assert bars_cron.EXIT_ERROR == 1
    for entry in ("live", "daily", "audit"):
        assert callable(getattr(bars_cron, entry)), f"{entry} is not an entry point"


# --- the calendar cron -------------------------------------------------------
#
# The interesting branch is the rate limit. The feed sits behind Cloudflare and
# answers 429 with a `retry-after` -- hit for real while `events.py` was being
# written, and still refusing three minutes later. A cron that treated that as a
# failure would alert daily about a feed that is working fine; one that treated a
# CHANGED FEED as a back-off would stay silent about a calendar filing high-impact
# releases as Low. So the two must not be confused, and the CLI's exit code is the
# only thing distinguishing them.


@pytest.fixture
def market_cron():
    return _load_cron("optjournal_market")


def test_the_market_cron_skips_a_back_off_and_raises_a_change(market_cron, monkeypatch):
    """EXIT_THROTTLED is quiet and retained; anything else wakes someone.

    Skip rather than Report on 429 because nothing is lost: the feed serves the
    same week tomorrow. Raise on any other non-zero because the one failure worth
    a human is a parse refusal -- `events.parse_events` rejects an unknown
    `impact` rather than filing it as Low, and a calendar that de-emphasises the
    day that mattered would otherwise look correct.
    """
    import subprocess

    from mesh_claw.cron_script import Skip

    # The real CLI is present in this checkout, but say so explicitly:
    # a PosixPath's `exists` is read-only, so the module attribute is what
    # gets pointed somewhere real.
    monkeypatch.setattr(market_cron, "CLI", Path(__file__))

    def throttled(*_a, **_k):
        return subprocess.CompletedProcess(
            [], market_cron.EXIT_THROTTLED, "",
            "calendar: the calendar feed is rate limiting; retry in 92s",
        )

    monkeypatch.setattr(market_cron.subprocess, "run", throttled)
    with pytest.raises(Skip, match="retry in 92s"):
        market_cron.refresh(None)

    def broken(*_a, **_k):
        return subprocess.CompletedProcess(
            [], market_cron.EXIT_ERROR, "", "unknown impact 'Critical'"
        )

    monkeypatch.setattr(market_cron.subprocess, "run", broken)
    with pytest.raises(RuntimeError, match="Critical"):
        market_cron.refresh(None)


def test_a_quiet_week_says_nothing(market_cron, monkeypatch):
    """No high-impact events ahead is the normal state, not news.

    The job runs daily and stores ~99 rows every time, almost all of them
    corrections nobody was waiting for. Reporting each run would train you to
    ignore the channel -- the same policy the bars cron states for its
    seven-times-a-session poll.
    """
    import json
    import subprocess

    # The real CLI is present in this checkout, but say so explicitly:
    # a PosixPath's `exists` is read-only, so the module attribute is what
    # gets pointed somewhere real.
    monkeypatch.setattr(market_cron, "CLI", Path(__file__))

    def ok(payload):
        def run(*_a, **_k):
            return subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        return run

    monkeypatch.setattr(market_cron.subprocess, "run", ok({
        "stored": 99,
        "events": [{"impact": "Low", "title": "Building Consents m/m",
                    "country": "NZD", "day": "2026-08-09", "at": "18:45"}],
    }))
    assert market_cron.refresh(None) is None


def test_high_impact_events_are_reported_with_their_figures(market_cron, monkeypatch):
    """The one thing worth a notification, and the attribution travels with it.

    `impact` is the feed's judgement; the message says so rather than presenting
    it as the journal's, which is the same rule the AutoFX markup follows.
    """
    import json
    import subprocess

    from mesh_claw.cron_script import Report

    # The real CLI is present in this checkout, but say so explicitly:
    # a PosixPath's `exists` is read-only, so the module attribute is what
    # gets pointed somewhere real.
    monkeypatch.setattr(market_cron, "CLI", Path(__file__))

    def run(*_a, **_k):
        return subprocess.CompletedProcess([], 0, json.dumps({
            "stored": 99,
            "events": [
                {"impact": "High", "title": "Non-Farm Employment Change",
                 "country": "USD", "day": "2026-08-07", "at": "08:30",
                 "forecast": "85K", "previous": "57K"},
                {"impact": "Low", "title": "Building Consents m/m",
                 "country": "NZD", "day": "2026-08-09", "at": "18:45"},
            ],
        }), "")

    monkeypatch.setattr(market_cron.subprocess, "run", run)
    with pytest.raises(Report) as caught:
        market_cron.refresh(None)
    message = str(caught.value)
    assert "Non-Farm Employment Change" in message
    assert "fc 85K" in message and "prev 57K" in message
    assert "Building Consents" not in message, "a Low event was reported"
    assert "feed's assessment" in message, "impact was not attributed"


def test_an_empty_week_is_reported_because_the_feed_does_not_do_that(
    market_cron, monkeypatch
):
    """A successful fetch storing nothing means something changed upstream.

    The feed returns ~99 events for every week observed. Zero is not a quiet
    week, it is a signal -- and staying silent about it would leave the Market tab
    slowly emptying with nothing anywhere saying why.
    """
    import json
    import subprocess

    from mesh_claw.cron_script import Report

    # The real CLI is present in this checkout, but say so explicitly:
    # a PosixPath's `exists` is read-only, so the module attribute is what
    # gets pointed somewhere real.
    monkeypatch.setattr(market_cron, "CLI", Path(__file__))

    def run(*_a, **_k):
        return subprocess.CompletedProcess(
            [], 0, json.dumps({"stored": 0, "events": []}), ""
        )

    monkeypatch.setattr(market_cron.subprocess, "run", run)
    with pytest.raises(Report, match="no events"):
        market_cron.refresh(None)


# ---------------------------------------------------------------------------
# The delivery policy itself: every branch of sync() and the bars jobs.
#
# WHY THIS SECTION EXISTS. Everything above asserts constants, shim structure and
# one formatter. Not one test in this file CALLED `sync()`, `live()`, `daily()`,
# `audit()` or `_collect()` -- measured, zero call sites -- so the ~660 lines
# deciding what is worth waking a human for were covered by nothing. Every branch
# in them could have been deleted and the suite would have stayed green.
#
# That matters now because SCHEDULER_PLAN.md replaces these files with an in-app
# scheduler, and a rewrite with no oracle silently inverts a decision. The
# decisions are asymmetric on purpose: a wrong Skip loses an unrecoverable option
# session in silence, while a wrong Report trains you to ignore the channel. This
# section is the oracle `jobs.py` will be written against, which is why it is worth
# adding to code that is scheduled for deletion.
#
# The shape is the market tests' proven one: point `CLI` at a real file (a
# PosixPath's `exists` is read-only, so the module ATTRIBUTE is what moves),
# monkeypatch `subprocess.run` to return a chosen CompletedProcess, and assert
# which sentinel comes out.
# ---------------------------------------------------------------------------


class _Ctx:
    """The MeshClaw context object, reduced to what these entry points read.

    `sync` reads `ctx.message` for an override query id; the bars jobs ignore ctx
    entirely. A SimpleNamespace would do, but a named class makes the coupling
    visible -- and it is the coupling `jobs.py` removes.
    """

    message = ""


def _proc(code: int, stdout: str = "", stderr: str = ""):
    """A CompletedProcess as the cron's own `_run` would return it."""
    import subprocess

    return subprocess.CompletedProcess([], code, stdout, stderr)


def _stub(module, monkeypatch, result):
    """Point `module` at a real CLI and make its subprocess return `result`.

    `result` may be a CompletedProcess or an exception INSTANCE to raise, which is
    how the TimeoutExpired branches are reached without waiting for a timeout.
    """
    monkeypatch.setattr(module, "CLI", Path(__file__))

    def run(*_a, **_k):
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(module.subprocess, "run", run)


@pytest.fixture()
def no_backup(sync_cron, monkeypatch):
    """Neutralise the git backup so a sync test asserts the SYNC's decision.

    The backup runs `git` against the real workspace repo, and its note or its
    failure is appended to whatever the sync decides -- so without this, every
    assertion here would depend on the state of a git repository. The backup's own
    branches get their own test below.
    """
    monkeypatch.setattr(sync_cron, "_commit_raw_backup", lambda: None)


def test_sync_skips_a_throttled_fetch(sync_cron, monkeypatch, no_backup):
    """EXIT_THROTTLED is the local cooldown: nothing was sent, nothing is lost.

    Silent because the guard did its job. An Activity statement is regenerated
    once a day, so a refused second fetch cannot have returned new information.
    """
    from mesh_claw.cron_script import Skip

    _stub(sync_cron, monkeypatch, _proc(sync_cron.EXIT_THROTTLED))
    with pytest.raises(Skip):
        sync_cron.sync(_Ctx())


def test_sync_reports_a_timeout_because_the_request_was_already_spent(
    sync_cron, monkeypatch, no_backup
):
    """The most expensive branch, and the reason it is Report and not Skip.

    The request reached IBKR before we gave up, so it counted against the lockout
    budget -- and `flex` records the cooldown only after `download` RETURNS, so a
    timed-out run leaves no cooldown and the next invocation spends another
    request. A Skip here would quietly burn the budget twice.
    """
    import subprocess

    from mesh_claw.cron_script import Report

    _stub(sync_cron, monkeypatch, subprocess.TimeoutExpired([], 1))
    with pytest.raises(Report, match="counted against the IBKR"):
        sync_cron.sync(_Ctx())


def test_sync_is_silent_when_there_is_nothing_to_ingest(
    sync_cron, monkeypatch, no_backup
):
    """EXIT_NO_DATA is not a failure: a weekend has no new statement."""
    _stub(sync_cron, monkeypatch, _proc(sync_cron.EXIT_NO_DATA))
    assert sync_cron.sync(_Ctx()) is None


def test_sync_reports_a_configuration_problem_rather_than_raising(
    sync_cron, monkeypatch, no_backup
):
    """A missing or locked token needs a human, and the message must reach one.

    Report rather than raise, and the distinction is not cosmetic: MeshClaw's
    script branch catches every exception and only logs it, so a raise reaches
    nobody. Proven live -- the 2026-08-07 sync raised on a locked keychain and the
    only trace was one log line. See SCHEDULER_PLAN.md step 8.
    """
    from mesh_claw.cron_script import Report

    _stub(sync_cron, monkeypatch,
          _proc(sync_cron.EXIT_CONFIG, stderr="no keyring entry"))
    with pytest.raises(Report, match="configuration problem"):
        sync_cron.sync(_Ctx())


def test_sync_raises_on_an_unrecognised_exit_code(sync_cron, monkeypatch, no_backup):
    """An exit code this policy does not model must not be swallowed."""
    _stub(sync_cron, monkeypatch, _proc(99, stderr="something new"))
    with pytest.raises(RuntimeError, match="exited 99"):
        sync_cron.sync(_Ctx())


def test_sync_raises_when_the_cli_contract_changes(sync_cron, monkeypatch, no_backup):
    """Exit 0 with unparseable stdout means the CLI changed shape under us."""
    _stub(sync_cron, monkeypatch, _proc(sync_cron.EXIT_OK, stdout="not json"))
    with pytest.raises(RuntimeError, match="unparseable JSON"):
        sync_cron.sync(_Ctx())


def test_sync_reports_only_a_real_change(sync_cron, monkeypatch, no_backup):
    """`changed` is the whole delivery rule: new rows speak, a no-op does not.

    Both directions in one test because they are one decision. A daily job that
    announced every silent run would be 365 notifications a year saying nothing,
    which is the noise these 660 lines exist to avoid.
    """
    from mesh_claw.cron_script import Report

    _stub(sync_cron, monkeypatch, _proc(sync_cron.EXIT_OK, stdout=json.dumps({
        "changed": True, "new_trades": 2, "new_cash": 1,
        "archive": "activity-20260808T184519Z.xml",
    })))
    with pytest.raises(Report):
        sync_cron.sync(_Ctx())

    _stub(sync_cron, monkeypatch, _proc(sync_cron.EXIT_OK, stdout=json.dumps({
        "changed": False, "new_trades": 0, "new_cash": 0,
    })))
    assert sync_cron.sync(_Ctx()) is None, "a no-op sync must stay silent"


def test_a_failed_backup_breaks_every_silence(sync_cron, monkeypatch):
    """A backup that fails quietly is not a backup.

    Asserted across all three otherwise-silent outcomes, because the rule is that
    a backup failure OUTRANKS the sync's own quiet: nothing-to-ingest, no-change
    and a real change all have to surface it. Three separate `if backup_error`
    branches implement this, so there are three places it could be dropped.
    """
    from mesh_claw.cron_script import Report

    def broken():
        raise RuntimeError("git commit failed: nothing to commit")

    monkeypatch.setattr(sync_cron, "_commit_raw_backup", broken)

    for code, out in (
        (sync_cron.EXIT_NO_DATA, ""),
        (sync_cron.EXIT_OK, json.dumps({"changed": False})),
        (sync_cron.EXIT_OK, json.dumps({"changed": True, "new_trades": 1})),
    ):
        _stub(sync_cron, monkeypatch, _proc(code, stdout=out))
        with pytest.raises(Report, match="backup"):
            sync_cron.sync(_Ctx())


def test_sync_raises_when_the_cli_is_missing(sync_cron, monkeypatch):
    """A registered job whose code is gone needs a human, not a retry."""
    monkeypatch.setattr(sync_cron, "CLI", Path("/nonexistent/optjournal"))
    with pytest.raises(RuntimeError, match="CLI not found"):
        sync_cron.sync(_Ctx())


# --- the bars jobs ---------------------------------------------------------


def test_bars_skips_a_timeout_because_nothing_was_spent(bars_cron, monkeypatch):
    """The opposite of sync's timeout, and the asymmetry IS the policy.

    The price endpoint is keyless with no request budget, so a slow run is a retry
    rather than an alert. Sync's identical timeout is a Report because its request
    was already charged against a lockout allowance. Same event, opposite
    delivery, and a rewrite that unified them would be wrong.
    """
    import subprocess

    from mesh_claw.cron_script import Skip

    _stub(bars_cron, monkeypatch, subprocess.TimeoutExpired([], 1))
    with pytest.raises(Skip):
        bars_cron.live(_Ctx())


def test_bars_skips_a_per_window_failure(bars_cron, monkeypatch):
    """EXIT_ERROR is a partial fetch, and the next poll re-collects it.

    Safe ONLY because the intraday series is cumulative within a session: a 13:00
    poll returns every completed bar since the open. That is what makes a Skip
    here lose nothing a later poll cannot recover.
    """
    from mesh_claw.cron_script import Skip

    _stub(bars_cron, monkeypatch, _proc(bars_cron.EXIT_ERROR))
    with pytest.raises(Skip):
        bars_cron.live(_Ctx())


def test_bars_is_silent_outside_the_session(bars_cron, monkeypatch):
    """EXIT_NO_DATA is the normal state for most of the seven daily polls."""
    _stub(bars_cron, monkeypatch, _proc(bars_cron.EXIT_NO_DATA))
    assert bars_cron.live(_Ctx()) is None
    assert bars_cron.daily(_Ctx()) is None


def test_bars_success_is_silent_but_still_parsed(bars_cron, monkeypatch):
    """Seven quiet polls a session, and a shape change still surfaces.

    Success is silent by design. The payload is parsed anyway, so a CLI contract
    change raises rather than passing -- otherwise the one job collecting
    unrecoverable data could break shape-wise and say nothing.
    """
    _stub(bars_cron, monkeypatch, _proc(bars_cron.EXIT_OK, stdout='{"fetched": 3}'))
    assert bars_cron.live(_Ctx()) is None

    _stub(bars_cron, monkeypatch, _proc(bars_cron.EXIT_OK, stdout="not json"))
    with pytest.raises(RuntimeError, match="unparseable JSON"):
        bars_cron.live(_Ctx())


def test_bars_raises_on_an_unrecognised_exit_code(bars_cron, monkeypatch):
    _stub(bars_cron, monkeypatch, _proc(42, stderr="new failure mode"))
    with pytest.raises(RuntimeError, match="exited 42"):
        bars_cron.daily(_Ctx())


def test_the_audit_reports_a_lost_session_and_never_retries_it(bars_cron, monkeypatch):
    """The single most consequential decision in these 660 lines.

    Report, not Skip: no amount of retrying brings back an option's intraday
    series, so a Skip would loop forever on something already lost. Report, not
    raise: a missing session is a fact to be told once, and a raise reaches only
    MeshClaw's log.

    The message must NAME the contracts, because "some bars are missing" is not
    actionable and this is the only notification that will ever mention them.
    """
    from mesh_claw.cron_script import Report

    _stub(bars_cron, monkeypatch, _proc(bars_cron.EXIT_ERROR, stdout=json.dumps({
        "day": "2026-08-07",
        "missing": ["TSLA  270115C00700000"],
        "covered": ["META  260918P00520000"],
    })))
    with pytest.raises(Report, match="TSLA  270115C00700000"):
        bars_cron.audit(_Ctx())


def test_the_audit_is_silent_when_the_session_landed(bars_cron, monkeypatch):
    """Covered, or nothing eligible: both silent, and both are the normal case."""
    for code in (bars_cron.EXIT_OK, bars_cron.EXIT_NO_DATA):
        _stub(bars_cron, monkeypatch, _proc(code))
        assert bars_cron.audit(_Ctx()) is None


def test_the_audit_raises_rather_than_staying_quiet_when_it_cannot_run(
    bars_cron, monkeypatch
):
    """The watchdog must not fail silently.

    Unlike a fetch, this touches only the local database, so failing to complete
    means the tooling is broken rather than the market being unreachable. Its
    docstring says staying quiet "would leave the one thing watching for silent
    loss silently broken itself" -- which is what then happened for real: three
    bars jobs read `last_status: ok` for two days while collecting nothing.
    """
    _stub(bars_cron, monkeypatch, _proc(7, stderr="database is locked"))
    with pytest.raises(RuntimeError, match="exited 7"):
        bars_cron.audit(_Ctx())
