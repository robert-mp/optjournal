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

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import STATEMENTS, connect_migrated

from optjournal import cli
from optjournal.cli import _asset_filter, main
from optjournal.db import SCHEMA_VERSION, connect
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


# --- update: a friend's clone, fast-forwarded -----------------------------------
#
# Against REAL git: a bare "published" repository, the publisher's clone and the
# friend's. The refusals are the point of `update`, and the ones that went wrong
# (no upstream, a detached HEAD, a clone ahead of the remote) are states only git
# itself describes faithfully. Each is a few commits in a temp folder.
#
# `uv` is the one stand-in: a script that records what it was asked to run, and
# runs the migration snippet with this interpreter, so the schema step really
# migrates the journal in a separate process. POSIX only, for its shebang.

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32",
                                 reason="the stand-in uv is a script with a shebang")


def _git(cwd: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "advice.detachedHead=false", *argv],
        cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _publish(repo: Path, text: str) -> None:
    (repo / "README.md").write_text(text)
    _git(repo, "commit", "-qam", text)
    _git(repo, "push", "-q", "origin", "HEAD:main")


def _commit_locally(repo: Path) -> None:
    (repo / "notes.txt").write_text("mine")
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-qm", "local")


@pytest.fixture
def clones(tmp_path, monkeypatch):
    """(publisher, friend): two clones of one published repository, in step.

    `cli.ROOT` is the friend's, since `update` always works on the code folder.
    """
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    pub = tmp_path / "pub"
    _git(tmp_path, "clone", "-q", str(remote), str(pub))
    (pub / "README.md").write_text("v1")
    _git(pub, "add", "README.md")
    _git(pub, "commit", "-qm", "v1")
    _git(pub, "push", "-q", "origin", "HEAD:main")
    friend = tmp_path / "friend"
    _git(tmp_path, "clone", "-q", str(remote), str(friend))
    monkeypatch.setattr(cli, "ROOT", friend)
    return pub, friend


@pytest.fixture
def fake_uv(tmp_path, monkeypatch):
    """A `uv` that logs each call, as `[cwd, *argv]`, to the returned file."""
    log = tmp_path / "uv.log"
    script = tmp_path / "bin" / "uv"
    script.parent.mkdir()
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys\n"
        f"with open({str(log)!r}, 'a') as f:\n"
        "    f.write(json.dumps([os.getcwd(), *sys.argv[1:]]) + '\\n')\n"
        "if sys.argv[1] == 'run':\n"
        "    rest = sys.argv[sys.argv.index('python') + 1:]\n"
        "    sys.exit(subprocess.call([sys.executable, *rest]))\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("UV", str(script))
    return log


def _uv_calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _update(capsys, db: Path, *flags: str) -> tuple[int, str]:
    capsys.readouterr()
    code = main(["update", "--db", str(db), *flags])
    out = capsys.readouterr()
    return code, out.out + out.err


def test_update_refuses_without_a_remote_and_touches_nothing(
        tmp_path, monkeypatch, capsys):
    """A clone with no remote has nothing to update from, and says so first."""
    _git(tmp_path, "init", "-q", str(tmp_path / "solo"))
    monkeypatch.setattr(cli, "ROOT", tmp_path / "solo")
    code, text = _update(capsys, tmp_path / "journal.db")
    assert code == cli.EXIT_CONFIG and "No git remote" in text


def test_update_refuses_a_dirty_tree_before_it_fetches(clones, tmp_path, capsys):
    """Uncommitted edits to tracked files stop the update, and stop it early.

    Before `git fetch`: the remote is made unreachable here, so a refusal that
    came after the fetch would report the network instead.
    """
    pub, friend = clones
    _publish(pub, "v2")
    (friend / "README.md").write_text("my edit")
    (tmp_path / "remote.git").rename(tmp_path / "gone.git")
    code, text = _update(capsys, tmp_path / "journal.db")
    assert code == cli.EXIT_CONFIG and "uncommitted changes" in text
    assert "README.md" in text


def test_an_untracked_file_does_not_block_an_update(clones, tmp_path, capsys):
    """M18: Finder's `.DS_Store` is not the reader's work, and blocked every update.

    Safe to ignore because git itself refuses a pull that would overwrite an
    untracked file, so nothing of the reader's can be lost this way.
    """
    pub, friend = clones
    _publish(pub, "v2")
    (friend / ".DS_Store").write_bytes(b"finder")
    code, text = _update(capsys, tmp_path / "journal.db", "--check")
    assert code == cli.EXIT_OK, text
    assert "1 new commit(s)" in text and "v2" in text


def test_update_check_reports_what_is_new_without_pulling(clones, tmp_path, capsys):
    pub, friend = clones
    before = _git(friend, "rev-parse", "HEAD")
    _publish(pub, "v2")
    code, text = _update(capsys, tmp_path / "journal.db", "--check")
    assert code == cli.EXIT_OK and "1 new commit(s)" in text
    assert _git(friend, "rev-parse", "HEAD") == before, "--check pulled"


def test_update_reports_up_to_date_when_the_heads_match(clones, tmp_path, capsys):
    code, text = _update(capsys, tmp_path / "journal.db")
    assert code == cli.EXIT_OK and "Already up to date" in text


@pytest.mark.parametrize("move", [("switch", "-q", "-c", "mine"), ("switch", "-q", "--detach")],
                         ids=["no-upstream", "detached"])
@pytest.mark.parametrize("flags", [(), ("--check",)], ids=["update", "check"])
def test_a_clone_off_its_tracking_branch_is_told_so(clones, tmp_path, capsys, move, flags):
    """L17: a branch with no upstream, or a detached HEAD, has nothing to pull.

    It printed "1 new commit(s)" followed by git's own fatal text and exited 0,
    then `update` called it a divergence.
    """
    pub, friend = clones
    _publish(pub, "v2")
    _git(friend, *move)
    code, text = _update(capsys, tmp_path / "journal.db", *flags)
    assert code == cli.EXIT_CONFIG, text
    assert "not on a branch that tracks a remote" in text
    assert "new commit" not in text and "fatal" not in text


@pytest.mark.parametrize("flags", [(), ("--check",)], ids=["update", "check"])
def test_a_clone_ahead_of_the_remote_is_up_to_date(clones, tmp_path, capsys, fake_uv, flags):
    """L18: local commits and nothing new upstream is up to date, not an update.

    It reported "0 new commit(s)", then ran the whole update and "Updated to".
    """
    _pub, friend = clones
    _commit_locally(friend)
    code, text = _update(capsys, tmp_path / "journal.db", *flags)
    assert code == cli.EXIT_OK, text
    assert "Already up to date" in text and "1 local commit" in text
    assert "Updated to" not in text and "new commit" not in text
    assert _uv_calls(fake_uv) == [], "nothing to install, so nothing may run"


@pytest.mark.parametrize("flags", [(), ("--check",)], ids=["update", "check"])
def test_a_diverged_clone_is_refused_before_anything_runs(
        clones, tmp_path, capsys, fake_uv, flags):
    pub, friend = clones
    _publish(pub, "v2")
    _commit_locally(friend)
    before = _git(friend, "rev-parse", "HEAD")
    code, text = _update(capsys, tmp_path / "journal.db", *flags)
    assert code == cli.EXIT_ERROR and "diverged" in text, text
    assert _git(friend, "rev-parse", "HEAD") == before
    assert _uv_calls(fake_uv) == []


@_POSIX_ONLY
def test_update_leaves_the_migration_to_a_server_still_running(
        clones, tmp_path, capsys, fake_uv, monkeypatch):
    """The running `serve` is the OLD code and migrates on every request, so a
    migration now was rolled back by its next page load and forward again at the
    restart, dropping the views under readers each time. With optjournal answering
    on its port, `update` installs the new code and leaves the migration to the
    restart it asks for."""
    from optjournal import web

    pub, _friend = clones
    _publish(pub, "v2")
    db = tmp_path / "home" / "journal.db"
    connect(db).close()
    with web.serve_ephemeral(db_path=tmp_path / "served.db",
                             archive_dir=tmp_path / "raw") as base:
        monkeypatch.setenv("OPTJOURNAL_PORT", base.rsplit(":", 1)[1].strip("/"))
        code, text = _update(capsys, db)

    assert code == cli.EXIT_OK, text
    assert [c[1] for c in _uv_calls(fake_uv)] == ["sync"], "it migrated anyway"
    assert "not migrated: optjournal is running with the old code" in text


@_POSIX_ONLY
def test_update_migrates_with_the_new_code_in_a_fresh_process(
        clones, tmp_path, capsys, fake_uv):
    """M16: the migration is the PULLED code's, so it runs in a new process.

    This process imported the old `db.py` before the pull, and migrating here
    reported the old schema version and skipped every new migration. And no uv
    call may rewrite the lock (M17): the sync is `--locked`, which refuses a
    stale lock by name, and the migration `--frozen`, so an update never writes a
    new lock that then blocks the next update as a dirty tree.
    """
    pub, friend = clones
    _publish(pub, "v2")
    db = tmp_path / "home" / "journal.db"
    connect(db).close()                            # a journal, not yet migrated

    code, text = _update(capsys, db)

    assert code == cli.EXIT_OK, text
    calls = _uv_calls(fake_uv)
    assert [c[1] for c in calls] == ["sync", "run"], "sync first, then migrate"
    assert all(c[0] == str(friend) for c in calls), "uv ran outside the code folder"
    assert "--locked" in calls[0] and "--frozen" in calls[1], calls
    assert f"Schema at version {SCHEMA_VERSION}." in text
    assert "Updated to" in text
    conn = connect(db)
    try:
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] \
            == SCHEMA_VERSION
    finally:
        conn.close()
    assert _git(friend, "status", "--porcelain") == ""


@_POSIX_ONLY
def test_a_failed_migration_is_reported_by_update(clones, tmp_path, capsys, fake_uv):
    """Not left for `serve` to hit later, when nobody is watching the terminal."""
    pub, _friend = clones
    _publish(pub, "v2")
    db = tmp_path / "journal.db"
    db.write_bytes(b"this is not a database" * 100)

    code, text = _update(capsys, db)

    assert code == cli.EXIT_ERROR
    assert "migrating the journal failed" in text and "not a database" in text
    assert "Updated to" not in text


def test_uv_is_found_through_its_own_variable_then_path_then_its_installer(
        tmp_path, monkeypatch):
    """L19: `uv run` exports `$UV`, and the installer puts uv in ~/.local/bin,
    which a double-clicked Start file's PATH need not include."""
    git_dir = str(Path(shutil.which("git") or "git").parent)
    if shutil.which("uv", path=git_dir):
        pytest.skip("uv sits beside git here, so PATH cannot exclude it")
    monkeypatch.setenv("PATH", git_dir)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.delenv("UV", raising=False)
    assert cli._find_uv() is None

    installed = tmp_path / ".local" / "bin" / ("uv.exe" if sys.platform == "win32" else "uv")
    installed.parent.mkdir(parents=True)
    installed.write_text("")
    assert cli._find_uv() == str(installed)

    exported = tmp_path / "exported-uv"
    exported.write_text("")
    monkeypatch.setenv("UV", str(exported))
    assert cli._find_uv() == str(exported)


def test_update_without_uv_refuses_before_it_pulls(clones, tmp_path, monkeypatch, capsys):
    """L19: it pulled, then found no uv, leaving new code on old dependencies."""
    pub, friend = clones
    _publish(pub, "v2")
    before = _git(friend, "rev-parse", "HEAD")
    monkeypatch.setattr(cli, "_find_uv", lambda: None)
    code, text = _update(capsys, tmp_path / "journal.db")
    assert code == cli.EXIT_CONFIG and "uv" in text
    assert _git(friend, "rev-parse", "HEAD") == before, "pulled without a uv to finish"


def test_update_has_no_json_output_and_says_so(tmp_path, monkeypatch, capsys):
    """L24: `--json` printed plain text. `update` reports progress, not data."""
    monkeypatch.setattr(cli, "ROOT", tmp_path)          # not a clone: nothing may run
    code, text = _update(capsys, tmp_path / "journal.db", "--json")
    assert code == cli.EXIT_CONFIG and "--json" in text


def test_friction_refuses_a_month_that_is_not_one(tmp_path, capsys):
    """L24: `--month garbage` matched nothing and exited 3, "no data"."""
    with pytest.raises(SystemExit) as refused:
        main(["friction", "--month", "garbage", "--db", str(tmp_path / "j.db")])
    assert refused.value.code == cli.EXIT_CONFIG
    assert "--month" in capsys.readouterr().err
    for period in ("2026-08", "2026"):
        assert main(["friction", "--month", period, "--db", str(tmp_path / "j.db")]) \
            == cli.EXIT_NO_DATA


def test_friction_has_data_when_its_lines_net_to_zero(tmp_path, capsys):
    """"No data" was decided on the fee SUM, so a market-data charge and the
    CANCEL row refunding it read as an empty month, and so did a month holding
    only dividend withholding. Both have lines the report prints."""
    from conftest import add_statement

    db = tmp_path / "j.db"
    conn = connect_migrated(db)
    add_statement(conn)
    rows = [
        ("1", "2026-03-01", "Other Fees", "OPRA NP L1", None, -1.29),
        ("2", "2026-03-02", "Other Fees", "CANCEL[OPRA NP L1]", None, 1.29),
        ("3", "2026-04-01", "Withholding Tax", "KO CASH DIVIDEND", "KO", -3.00),
    ]
    for tid, day, kind, description, symbol, amount in rows:
        conn.execute(
            "INSERT INTO cash_transactions (transaction_id, account_id, date_time,"
            " type, description, symbol, amount, currency, fx_rate_to_base,"
            " amount_base, raw, source_file, first_seen_at)"
            " VALUES (?, 'U1', ?, ?, ?, ?, ?, 'EUR', 1.0, ?, '{}', 't.xml', 'now')",
            (tid, f"{day} 10:00:00", kind, description, symbol, amount, amount),
        )
    conn.commit()
    conn.close()
    for period in ("2026-03", "2026-04"):
        assert main(["friction", "--month", period, "--db", str(db)]) == cli.EXIT_OK
    assert main(["friction", "--month", "2026-05", "--db", str(db)]) \
        == cli.EXIT_NO_DATA


# --- watch: the two fields the reader types -----------------------------------
#
# `optjournal watch` is the only writer of user-typed facts in the CLI, and it is
# exactly the boundary this module exists for: argparse succeeds, the command
# prints a table, the exit code is 0, and what was typed did not reach the row.
# No statement is needed for any of it, so these run wherever the suite does.


def _watch_json(capsys, *argv: str) -> list[dict]:
    """Run `optjournal watch ... --json` and hand back the rows it printed."""
    capsys.readouterr()
    assert main(["watch", *argv, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_watch_records_an_earnings_date_that_round_trips(tmp_path, capsys):
    """The date reaches the row, comes back verbatim, and carries a countdown.

    Verbatim because it is the one figure on this tab the reader supplied: there is
    no source to reconcile it against, so the only claim being made is "this is what
    you typed". The countdown beside it is derived on every read, so it is asserted
    as a TYPE here and pinned to a fixed clock in `test_serialize.py` -- asserting
    the number here would make this test fail on a date rather than on a change.
    """
    db = tmp_path / "watch.db"
    rows = _watch_json(capsys, "dell", "--earnings", "2026-08-27",
                       "--note", "watching the print", "--db", str(db))
    assert [r["symbol"] for r in rows] == ["DELL"], "lower case in, upper case stored"
    assert rows[0]["earnings_on"] == "2026-08-27"
    assert isinstance(rows[0]["earnings_in_days"], int)
    assert rows[0]["note"] == "watching the print"


def test_watch_leaves_typed_fields_alone_unless_the_flag_is_passed(tmp_path, capsys):
    """A bare re-add is not an edit, and `--clear-note` is.

    The same key-present rule as the endpoint, and here the flag's presence is what
    says which request this is. Both directions matter: a re-add that blanked a note
    would lose the only thing in this journal nothing can re-derive, and a note that
    cannot be cleared is the hole `web._watchlist_write` documents -- so the CLI
    grew the explicit verb rather than overloading `--note ''`, which argparse and a
    shell disagree about often enough.
    """
    db = tmp_path / "watch.db"
    _watch_json(capsys, "DELL", "--earnings", "2026-08-27", "--note", "mine",
                "--db", str(db))

    rows = _watch_json(capsys, "DELL", "--db", str(db))
    assert (rows[0]["note"], rows[0]["earnings_on"]) == ("mine", "2026-08-27"), (
        "a bare re-add rewrote a typed field"
    )

    rows = _watch_json(capsys, "DELL", "--clear-note", "--db", str(db))
    assert rows[0]["note"] is None
    assert rows[0]["earnings_on"] == "2026-08-27", (
        "clearing the note cleared a date the command never mentioned"
    )

    # And a date can be un-recorded, since the reader is the only source of it.
    rows = _watch_json(capsys, "DELL", "--earnings", "", "--db", str(db))
    assert rows[0]["earnings_on"] is None
    assert rows[0]["earnings_in_days"] is None


def test_watch_refuses_a_malformed_earnings_date(tmp_path, capsys):
    """Refused with a reason, and nothing written.

    A format check only, through the same `clock.parse_day` the endpoint uses, so
    the two surfaces cannot disagree about what a date is. `27/08/2026` is the
    interesting case: it is a date to a human, and stored it would sort wrong, print
    beside a YYYY-MM-DD date in the same column, and count down to nothing.

    Exit 2 (config) rather than 1, because nothing failed -- the invocation was
    wrong, which is what that code means.
    """
    db = tmp_path / "watch.db"
    assert main(["watch", "DELL", "--earnings", "27/08/2026", "--db", str(db)]) == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err

    rows = _watch_json(capsys, "--db", str(db))
    assert rows == [], "a refused command still added the symbol"


def test_watch_refuses_a_field_with_no_symbol_to_write_it_to(tmp_path, capsys):
    """`optjournal watch --earnings 2026-08-27` names nothing to record it against.

    Silently doing nothing is the failure this module's docstring describes: the
    table prints, the exit code is 0, and the date is nowhere. So it is refused with
    the shape of a working command in the message.
    """
    db = tmp_path / "watch.db"
    assert main(["watch", "--earnings", "2026-08-27", "--db", str(db)]) == 2
    assert "name the symbol" in capsys.readouterr().err


def test_watch_refuses_setting_and_clearing_a_note_at_once(tmp_path):
    """Two requests in one command, so argparse refuses it at the parser.

    SystemExit rather than a return code: a mutually exclusive group is argparse's
    own answer, and it prints the usage line naming both flags.
    """
    db = tmp_path / "watch.db"
    with pytest.raises(SystemExit):
        main(["watch", "DELL", "--note", "x", "--clear-note", "--db", str(db)])


def _fake_keyring(monkeypatch) -> list[str]:
    """No token stored, and writes captured: the suite never touches the real one."""
    import keyring  # noqa: PLC0415 - local to the setup tests

    wrote: list[str] = []
    monkeypatch.setattr(keyring, "get_password", lambda service, account: None)
    monkeypatch.setattr(keyring, "set_password",
                        lambda service, account, token: wrote.append(token))
    return wrote


def _setup(monkeypatch, home) -> int:
    import io  # noqa: PLC0415 - local to the setup tests

    monkeypatch.setenv("OPTJOURNAL_HOME", str(home))
    monkeypatch.setattr("sys.stdin", io.StringIO("123456789012345\n"))
    return main(["setup", "--query-id", "1591754", "--token-stdin", "--no-verify"])


def test_setup_on_a_fresh_machine_stores_the_token_and_the_query_id(
    tmp_path, monkeypatch,
):
    """H8: the README's first step, on a machine whose journal folder does not
    exist yet. Both halves must land."""
    from optjournal import settings  # noqa: PLC0415 - local to this test

    wrote = _fake_keyring(monkeypatch)
    home = tmp_path / "Application Support" / "optjournal"
    assert _setup(monkeypatch, home) == 0
    assert wrote == ["123456789012345"]
    assert settings.query_id(root=home) == "1591754"


def test_setup_stores_no_token_when_the_query_id_cannot_be_saved(
    tmp_path, monkeypatch, capsys,
):
    """H8: a settings write that fails must not leave the token stored and the
    query id lost, which is a half-configured journal that says it is set up.
    The query id is saved first, so a failure there stores nothing."""
    wrote = _fake_keyring(monkeypatch)
    blocker = tmp_path / "a-file"
    blocker.write_text("", encoding="utf-8")
    assert _setup(monkeypatch, blocker / "optjournal") == 1
    assert wrote == [], "the token was stored although the query id was not"
    assert "Nothing was stored" in capsys.readouterr().err


# --- an unreadable file in the archive (M6, L24) ------------------------------


@pytest.mark.skipif(not STATEMENTS, reason="needs an archived statement")
def test_an_unreadable_archive_file_is_skipped_and_the_rest_still_ingest(
    tmp_path, capsys,
):
    """M6: one bad file used to halt `optjournal ingest` at that file forever.

    It sorts FIRST here, so the old behaviour ingests nothing at all. Now it is
    named and skipped, every later statement lands, and the exit code still says
    something went wrong.
    """
    archive = tmp_path / "raw"
    archive.mkdir()
    (archive / "activity-20200101T000000Z.xml").write_bytes(
        b"<html><body>Scheduled maintenance</body></html>")
    (archive / STATEMENTS[0].name).write_bytes(STATEMENTS[0].read_bytes())
    db = tmp_path / "cli.db"

    code = main(["ingest", "--archive", str(archive), "--db", str(db)])

    out = capsys.readouterr()
    assert code != 0, "a skipped file must not read as a clean run"
    assert "activity-20200101T000000Z.xml" in out.out
    assert "Traceback" not in out.err
    conn = connect_migrated(db)
    names = [r[0] for r in conn.execute("SELECT source_file FROM statements")]
    assert names == [STATEMENTS[0].name]
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] > 0


def test_ingest_reports_an_unreadable_file_in_its_json(tmp_path, capsys):
    bad = tmp_path / "activity-bad.xml"
    bad.write_bytes(b"<FlexQueryResponse")
    code = main(["ingest", str(bad), "--db", str(tmp_path / "cli.db"), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert code != 0
    assert [u["file"] for u in data["unreadable"]] == ["activity-bad.xml"]


@pytest.mark.parametrize("command", ["costs", "show"])
def test_a_malformed_statement_is_one_line_not_a_traceback(tmp_path, capsys,
                                                            command):
    """L24: `costs <file>` printed a raw ParseError traceback."""
    bad = tmp_path / "activity-bad.xml"
    bad.write_bytes(b"<FlexQueryResponse")
    code = main([command, str(bad)])
    err = capsys.readouterr().err.strip()
    assert code != 0
    assert "activity-bad.xml" in err
    assert len(err.splitlines()) == 1, err
    assert "request" not in err, "a local file is not a failed Flex request"


@pytest.mark.skipif(not STATEMENTS, reason="needs an archived statement")
def test_ingest_names_a_value_py_ibkr_does_not_declare(tmp_path, capsys):
    """Such a value is accepted so the statement still ingests, but it was
    accepted SILENTLY, while a reader that knows only the declared values can
    drop the row from what it reads. The run now says which values arrived."""
    text = STATEMENTS[0].read_text(encoding="utf-8")
    assert 'orderType="LMT"' in text
    path = tmp_path / "activity-unfamiliar.xml"
    path.write_text(text.replace('orderType="LMT"', 'orderType="LIT"', 1), encoding="utf-8")

    code = main(["ingest", str(path), "--db", str(tmp_path / "j.db")])

    assert code == 0
    err = capsys.readouterr().err
    assert "does not declare, kept as sent" in err and "LIT" in err, err


@pytest.mark.skipif(not STATEMENTS, reason="needs an archived statement")
def test_each_run_names_the_undeclared_values_it_met_and_no_others(tmp_path, capsys):
    """The notes were process-wide sets, never cleared, and a value was recorded
    only the first time the process met it. So a second run in one process
    repeated the first run's notes, about a file it never read, and stopped
    naming a value it did read again. Seen as a shuffled-suite failure."""
    text = STATEMENTS[0].read_text(encoding="utf-8")
    path = tmp_path / "activity-unfamiliar.xml"
    path.write_text(text.replace('orderType="LMT"', 'orderType="LIT"', 1), encoding="utf-8")
    bad = tmp_path / "activity-bad.xml"
    bad.write_bytes(b"<FlexQueryResponse")

    assert main(["ingest", str(path), "--db", str(tmp_path / "a.db")]) == 0
    assert "OrderType=LIT" in capsys.readouterr().err
    main(["show", str(bad)])
    assert "does not declare" not in capsys.readouterr().err, "a note from another run"
    assert main(["ingest", str(path), "--db", str(tmp_path / "b.db")]) == 0
    assert "OrderType=LIT" in capsys.readouterr().err, "met again, and not named"


@pytest.mark.skipif(not STATEMENTS, reason="needs an archived statement")
@pytest.mark.parametrize("command", ["costs", "show", "ingest"])
def test_a_statement_with_a_malformed_number_is_one_line_not_a_traceback(
    tmp_path, capsys, command,
):
    """Well-formed XML whose one number is not a number: py_ibkr raises
    decimal.InvalidOperation, an ArithmeticError rather than a ValueError, so
    it escaped as a traceback and stopped `ingest` before the files after it."""
    text = STATEMENTS[0].read_text(encoding="utf-8")
    assert 'quantity="' in text
    bad = tmp_path / "activity-badnumber.xml"
    bad.write_text(re.sub(r'quantity="[^"]*"', 'quantity="abc"', text, count=1),
                   encoding="utf-8")
    args = [command, str(bad)] + (["--db", str(tmp_path / "j.db")]
                                  if command == "ingest" else [])
    code = main(args)
    out = capsys.readouterr()
    assert code != 0
    assert "Traceback" not in out.err
    assert "activity-badnumber.xml" in out.out + out.err


def test_the_update_probe_is_bounded_by_a_program_that_streams(monkeypatch):
    """`update` asks the port whether optjournal is running. A program streaming
    there held that read for as long as it streamed, because the socket timeout
    bounds each read rather than the probe: the same defect the launcher's twin
    probe had. It gives up at its own deadline now."""
    import contextlib
    import http.server
    import threading
    import time

    class Stream(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib naming
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            with contextlib.suppress(OSError):
                for _ in range(50):
                    self.wfile.write(b".")
                    self.wfile.flush()
                    time.sleep(0.1)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stream)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("OPTJOURNAL_PORT", str(server.server_address[1]))
        started = time.monotonic()
        answered = cli._serving_here(timeout_s=0.5)
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()
    assert answered is False
    assert elapsed < 2.0, f"the probe read the stream for {elapsed:.1f}s"


def test_sync_behind_another_fetch_is_busy_not_a_traceback(tmp_path, capsys, monkeypatch):
    """`optjournal sync` waits out a whole fetch for the lock (it has nothing else
    to do), and if another fetch holds it even longer, says busy, exits as
    throttled, and records it for the scheduler. It escaped as a traceback with
    no ledger row."""
    from optjournal.flex import FetchLockTimeout

    seen: dict = {}

    def held(**kwargs):
        seen.update(kwargs)
        raise FetchLockTimeout("raw/.fetch.lock held by another fetch")

    monkeypatch.setattr(cli, "sync_journal", held)
    db = tmp_path / "j.db"

    code = main(["sync", "1591754", "--db", str(db), "--archive", str(tmp_path / "raw")])

    err = capsys.readouterr().err
    assert code == cli.EXIT_THROTTLED
    assert "Traceback" not in err and "Busy" in err, err
    assert seen["lock_timeout_s"] == cli.FETCH_LOCK_TIMEOUT_S
    row = connect_migrated(db).execute(
        "SELECT status, detail FROM job_runs WHERE job = 'sync' ORDER BY id DESC"
    ).fetchone()
    assert (row["status"], row["detail"][:5]) == ("nothing", "busy:")


def test_sync_waits_for_the_real_fetch_lock_as_long_as_it_is_told(
    tmp_path, capsys, monkeypatch,
):
    """The whole path behind the test above, with a real holder: the wait the cron
    hands over in `OPTJOURNAL_LOCK_WAIT_S`, then busy and throttled. Nothing past
    the lock may run, so the keyring and IBKR are refused outright."""
    import time

    from optjournal import flex, locks

    def refused(*_a, **_k):
        raise AssertionError("got past the fetch lock: keyring/IBKR must not be reached")

    monkeypatch.setattr(flex, "read_token", refused)
    monkeypatch.setattr(flex, "_client_factory", refused)
    monkeypatch.setenv(cli.LOCK_WAIT_ENV, "0.2")
    archive = tmp_path / "raw"
    started = time.monotonic()
    with locks.locked(archive / flex.FETCH_LOCK):
        code = main(["sync", "1591754", "--db", str(tmp_path / "j.db"),
                     "--archive", str(archive)])
    assert code == cli.EXIT_THROTTLED
    assert "Busy" in capsys.readouterr().err
    assert time.monotonic() - started < 5, "the wait it was told was not the one used"


def _held_elsewhere(monkeypatch, module, path):
    """Hold the lock file at `path` here, and make `module` wait for it not at all.

    The two locks this is for wait `locks.DEFAULT_TIMEOUT_S` (120s), so the wait is
    cut to one attempt: the lock itself is real, and so is the timeout it raises.
    """
    from optjournal import locks

    monkeypatch.setattr(module, "locked",
                        lambda target, **_: locks.locked(target, timeout_s=0))
    return locks.locked(path)


def test_a_migration_lock_timeout_is_an_error_not_busy(tmp_path, capsys, monkeypatch):
    """Only the FETCH lock's timeout means "another fetch is running, try later".
    Every `LockTimeout` read that way, so a migration wedged behind another
    process exited EXIT_THROTTLED with "Busy: another fetch is still running",
    which the cron turns into a silent Skip. Not EXIT_ERROR either: the bars cron
    reads 1 as a per-window fetch failure and skips it. Its own code, which
    every cron raises."""
    from optjournal import db as dbmod

    journal = tmp_path / "j.db"             # new, so the migration takes its lock
    with _held_elsewhere(monkeypatch, dbmod, Path(f"{journal}.migrate.lock")):
        code = main(["sync", "1591754", "--db", str(journal),
                     "--archive", str(tmp_path / "raw")])
    err = capsys.readouterr().err
    assert code == cli.EXIT_LOCKED, err
    assert "Busy" not in err and "another fetch" not in err, err
    assert "migrate.lock" in err and "Traceback" not in err, err


def test_a_settings_lock_timeout_is_an_error_not_busy(tmp_path, capsys, monkeypatch):
    """The same for the settings file's lock, which `setup` takes to save the id."""
    from optjournal import settings

    _fake_keyring(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    with _held_elsewhere(monkeypatch, settings, settings.lock_path(home)):
        code = _setup(monkeypatch, home)
    err = capsys.readouterr().err
    assert code == cli.EXIT_LOCKED, err
    assert "Busy" not in err and settings.LOCK_FILENAME in err, err
