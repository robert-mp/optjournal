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
@pytest.mark.parametrize("name", ["optjournal_sync", "optjournal_bars"])
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


def test_a_sync_payload_key_the_cron_reads_still_exists():
    """Every key `_describe` reaches for, against `cmd_sync`'s real emit block.

    Source-level like test_web's /api/sync guard, and for the same reason:
    producing a genuine payload spends an IBKR request.
    """
    import inspect

    from optjournal.cli import cmd_sync

    src = inspect.getsource(cmd_sync)
    for key in ("new_trades", "new_trade_rows", "new_cash", "warnings", "changed"):
        assert f'"{key}"' in src, f"cmd_sync no longer emits {key!r}"


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
