"""The CLI's own argument decoding, which nothing tested.

`cli.py` is wiring, so its logic is thin -- but the thin parts sit between the
user's intent and what gets stored, and a defect there is silent by construction:
argparse succeeds, the command runs, the exit code is 0, and the database is
empty.

That is not hypothetical. Mutation-testing found that dropping the `"ALL"`
sentinel from `_asset_filter` -- so `"ALL"` is decoded as a literal asset category
rather than as "every category" -- makes `optjournal ingest` report
`trades +0 (filtered 9)` per statement, store **zero** trades, positions and
securities, and exit **0**. All 597 tests passed. They passed because every one of
them calls `ingest_file(assets=ASSET_FILTER_ALL)` directly, handing over the
already-decoded constant and never exercising the decoder that turns a
command-line string into it.

So these tests are deliberately at the boundary the suite was blind to: the
string the user types, and the tuple the ingest receives.
"""

from __future__ import annotations

import argparse
import json

import pytest
from conftest import STATEMENTS, connect_migrated

from optjournal import cli
from optjournal.cli import _asset_filter, main
from optjournal.ingest import ASSET_FILTER_ALL


def test_all_decodes_to_the_every_category_sentinel():
    """`ASSET_FILTER_ALL` is the empty tuple, and that is the whole trap.

    `assets=()` reads as "store nothing" but means "store everything" -- the
    ingest treats an empty filter as no filter. So decoding `"ALL"` to a literal
    `("ALL",)` does not fail loudly; it matches no category and silently filters
    every row out. Asserted by identity to the constant, not just by emptiness,
    so the intent is unmistakable.
    """
    assert _asset_filter("ALL") == ASSET_FILTER_ALL
    assert _asset_filter("ALL") == ()
    # Case and whitespace are the user's, not the decoder's.
    assert _asset_filter("all") == ASSET_FILTER_ALL
    assert _asset_filter("  All  ") == ASSET_FILTER_ALL
    # An omitted value means the same as ALL rather than "nothing".
    assert _asset_filter("") == ASSET_FILTER_ALL


def test_a_narrow_filter_is_upper_cased_and_split():
    """IBKR's categories are upper-case, so a lower-case `opt` must not silently
    match nothing -- the same failure mode as the sentinel, one layer down."""
    assert _asset_filter("OPT") == ("OPT",)
    assert _asset_filter("opt") == ("OPT",)
    assert _asset_filter("OPT,STK") == ("OPT", "STK")
    assert _asset_filter(" opt , stk ") == ("OPT", "STK")
    # A trailing or doubled comma is a typo, not an empty category.
    assert _asset_filter("OPT,,STK") == ("OPT", "STK")
    assert _asset_filter("OPT,") == ("OPT",)


@pytest.mark.skipif(not STATEMENTS, reason="needs an archived statement")
def test_the_default_ingest_stores_trades_rather_than_filtering_them_all_out(tmp_path):
    """End to end through `main`, because that is where the blindness was.

    The unit assertions above would have caught the sentinel defect, but only
    because they exist now. This one closes the more general hole: it drives the
    real command with its real default and asserts rows LANDED, so any future
    argument-decoding change that quietly stores nothing fails here rather than at
    a terminal a week later.

    Asserts a positive count, not merely exit 0 -- exit 0 with an empty database
    is precisely what the defect produced.
    """
    db = tmp_path / "cli.db"
    code = main(["ingest", "--archive", str(STATEMENTS[0].parent), "--db", str(db)])
    assert code == 0, "the default ingest should succeed"

    conn = connect_migrated(db)
    trades = conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"]
    assert trades > 0, (
        "the default --assets stored no trades at all; the ALL sentinel is being "
        "decoded as a literal category and filtering every row out"
    )
    # Categories beyond options prove nothing was narrowed: the archive holds
    # stock and FX too, and storing only OPT would also read as "it worked".
    categories = {
        r["asset_category"]
        for r in conn.execute("SELECT DISTINCT asset_category FROM trades")
    }
    assert len(categories) > 1, (
        f"only {categories} stored -- the default must not narrow, since a row "
        "dropped at ingest costs a re-ingest to recover"
    )


@pytest.mark.skipif(not STATEMENTS, reason="needs an archived statement")
def test_an_explicit_narrow_filter_really_narrows(tmp_path):
    """The other direction: `--assets OPT` must filter, or the flag is a no-op.

    Without this, a decoder that returned `ASSET_FILTER_ALL` for everything would
    pass the test above and silently ignore the user's narrowing.
    """
    db = tmp_path / "narrow.db"
    assert main(["ingest", "--archive", str(STATEMENTS[0].parent),
                 "--db", str(db), "--assets", "OPT"]) == 0
    conn = connect_migrated(db)
    categories = {
        r["asset_category"]
        for r in conn.execute("SELECT DISTINCT asset_category FROM trades")
    }
    assert categories == {"OPT"}, f"--assets OPT stored {categories}"


# --------------------------------------------------------- the wire vocabulary
#
# `optjournal show` is the only consumer of `summary_data`, and neither had a
# test -- so `_wire`, which turns py_ibkr's Enum members into the short codes
# the payload and the page both speak, was unguarded end to end.
#
# The failure is quiet in the worst way: py_ibkr's Enum members stringify as
# 'AssetClass.STOCK', which is a perfectly good string. Nothing raises. The
# summary just reports its categories under names no other layer uses, and
# `by_asset` stops joining to the 'STK' the database stores.


@pytest.mark.skipif(not STATEMENTS, reason="needs an archived statement")
def test_the_summary_reports_ibkrs_codes_not_python_enum_names(capsys):
    """Over a real statement, through the real command.

    Asserted on the JSON rather than the text, because the payload is what a
    consumer joins on -- and asserted by ABSENCE of the class name as well as
    presence of the code, since a mapping that emitted both would satisfy only
    half of this.
    """
    assert main(["show", str(STATEMENTS[-1]), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    stmt = data["statements"][0]

    for field in ("by_asset", "by_open_close", "by_buy_sell", "cash_by_type"):
        keys = set(stmt[field])
        assert keys, f"{field} is empty, so it proves nothing"
        assert not any("." in k for k in keys), (
            f"{field} leaked a dotted Python name: {keys}"
        )
        assert not any(k.startswith(("AssetClass", "BuySell", "OpenClose",
                                     "CashAction")) for k in keys), (
            f"{field} leaked an enum class name: {keys}"
        )

    # The specific codes every other layer speaks. STK because the archive holds
    # stock; O/C because that is what the schema and the page's filters store.
    assert "STK" in stmt["by_asset"], stmt["by_asset"]
    assert {"O", "C"} & set(stmt["by_open_close"]), stmt["by_open_close"]
    assert {"BUY", "SELL"} & set(stmt["by_buy_sell"]), stmt["by_buy_sell"]


def test_a_value_less_enum_falls_back_to_its_last_component():
    """The fallback branch, which real data cannot reach.

    py_ibkr gives every member a `.value`, so the archive always takes the first
    path. The fallback exists for a member that only implements __str__ -- a
    plausible shape for a future broker's parser -- and without it that member
    would reach the payload as 'AssetClass.STOCK'.
    """
    from optjournal.serialize import _wire

    class Bare:
        def __str__(self) -> str:
            return "AssetClass.STOCK"

    assert _wire(Bare()) == "STOCK"
    # An undotted string passes through, and None is the dash the reports show.
    assert _wire("STK") == "STK"
    assert _wire(None) == "-"


# --- friction: the journal's costs, from the database -------------------------


@pytest.mark.skipif(not STATEMENTS, reason="needs an archived statement")
def test_friction_reports_the_journal_not_one_statement(tmp_path, capsys):
    """The command exists because `costs` answers a different question.

    `costs` reads one statement -- the newest archive covers 30 calendar days --
    while this reads every ingested fill. Asserted as a SPAN comparison rather
    than against a literal, so it stays true as the archive grows.
    """
    db = tmp_path / "friction.db"
    assert main(["ingest", "--archive", str(STATEMENTS[0].parent), "--db", str(db)]) == 0
    capsys.readouterr()
    assert main(["friction", "--db", str(db), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["from_date"] and out["to_date"]
    assert out["from_date"] <= out["to_date"]
    assert out["totals"]["fills"] > 0


@pytest.mark.skipif(not STATEMENTS, reason="needs an archived statement")
def test_friction_scopes_to_the_categories_asked_for(tmp_path, capsys):
    """`--assets` is a multi-select, and it must really narrow.

    Both directions, because a decoder that ignored the flag would pass a test
    that only checked the wide case: options alone must be less than options plus
    stock, and the payload must echo what it measured.
    """
    db = tmp_path / "scope.db"
    assert main(["ingest", "--archive", str(STATEMENTS[0].parent), "--db", str(db)]) == 0

    def friction(*assets):
        argv = ["friction", "--db", str(db), "--json"]
        if assets:
            argv += ["--assets", *assets]
        capsys.readouterr()
        assert main(argv) == 0
        return json.loads(capsys.readouterr().out)

    opt = friction("OPT")
    both = friction("OPT", "STK")
    assert opt["scope"]["categories"] == ["OPT"]
    assert both["scope"]["categories"] == ["OPT", "STK"]
    assert (opt["totals"]["attributable"]["base"]
            < both["totals"]["attributable"]["base"])
    # And an unscoped run is the widest of the three.
    assert (both["totals"]["attributable"]["base"]
            <= friction()["totals"]["attributable"]["base"])


@pytest.mark.skipif(not STATEMENTS, reason="needs an archived statement")
def test_friction_prints_the_estimate_as_a_range(tmp_path, capsys):
    """A terminal report that collapsed the range would be the one surface where
    the AutoFX uncertainty disappears -- roughly a quarter of this account's
    friction, from a constant IBKR itself hedges."""
    db = tmp_path / "range.db"
    assert main(["ingest", "--archive", str(STATEMENTS[0].parent), "--db", str(db)]) == 0
    capsys.readouterr()
    assert main(["friction", "--db", str(db), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    if not data["totals"]["friction"]["is_estimated"]:
        pytest.skip("no auto-conversions in this archive")
    capsys.readouterr()
    assert main(["friction", "--db", str(db)]) == 0
    text = capsys.readouterr().out
    assert "range" in text, "the human report hides the estimate's range"
    assert "ESTIMATE" in text, "the markup column is not labelled as estimated"


def _run_update(monkeypatch, tmp_path, *, remote, dirty="", extra=None):
    """Drive `cmd_update` against a scripted git, recording what it ran.

    Scripted rather than driven against a real clone: the refusals are the whole
    point of this command, and provoking a diverged history and a dirty tree for
    real costs more setup than it buys. What matters is that a refusal happens
    BEFORE any command that writes -- which is a claim about the call order, and
    the recorded list is what proves it.
    """
    calls: list[tuple[str, ...]] = []
    replies = {
        ("remote",): (0, remote),
        ("status", "--porcelain"): (0, dirty),
        ("fetch", "--quiet"): (0, ""),
        ("rev-parse", "HEAD"): (0, "aaaaaaaaa"),
        ("rev-parse", "@{u}"): (0, "bbbbbbbbb"),
        ("log", "--oneline", "HEAD..@{u}"): (0, "bbbbbbb feat: a thing"),
        ("pull", "--ff-only", "--quiet"): (0, ""),
    }
    replies.update(extra or {})

    def fake_git(*argv):
        calls.append(argv)
        return replies.get(argv, (0, ""))

    monkeypatch.setattr(cli, "_git", fake_git)
    args = argparse.Namespace(check=True, db=tmp_path / "absent.db")
    return cli.cmd_update(args), calls


def test_update_refuses_without_a_remote_and_touches_nothing(monkeypatch, tmp_path):
    """A clone with no remote has nothing to update from, and says so.

    Checked FIRST, so the failure is one clear sentence rather than a git error
    about `@{u}` being unresolvable -- which is the same fact spelled in a way
    that sends the reader to the wrong place.
    """
    code, calls = _run_update(monkeypatch, tmp_path, remote="")
    assert code == cli.EXIT_CONFIG
    assert calls == [("remote",)], "nothing else may run once there is no remote"


def test_update_refuses_a_dirty_tree_before_it_fetches(monkeypatch, tmp_path):
    """Uncommitted work stops the update, and stops it early.

    A pull that stashed someone's edits without being asked is a worse outcome
    than stopping, so this refuses. It refuses before `git fetch` as well, which
    is what keeps a refusal from touching the network at all.
    """
    code, calls = _run_update(
        monkeypatch, tmp_path, remote="origin", dirty=" M src/optjournal/cli.py")
    assert code == cli.EXIT_CONFIG
    assert ("fetch", "--quiet") not in calls, "a refusal must not reach the network"
    assert ("pull", "--ff-only", "--quiet") not in calls


def test_update_check_reports_what_is_new_without_pulling(monkeypatch, tmp_path):
    """`--check` is read-only, and that has to be true of the git calls too.

    A dry run that fetches is fine -- fetching changes no working file -- but one
    that pulls is not a dry run at all, and the flag exists for someone deciding
    whether to update at a moment that suits them.
    """
    code, calls = _run_update(monkeypatch, tmp_path, remote="origin")
    assert code == cli.EXIT_OK
    assert ("fetch", "--quiet") in calls
    assert ("pull", "--ff-only", "--quiet") not in calls


def test_update_reports_up_to_date_when_the_heads_match(monkeypatch, tmp_path):
    """Nothing new is a success, not a no-op worth a warning.

    This runs on a schedule in the hands of anyone who wires it up, so the quiet
    path has to be the common one.
    """
    code, calls = _run_update(
        monkeypatch, tmp_path, remote="origin",
        extra={("rev-parse", "@{u}"): (0, "aaaaaaaaa")})
    assert code == cli.EXIT_OK
    assert ("log", "--oneline", "HEAD..@{u}") not in calls, (
        "there is no range to log when the heads agree"
    )
