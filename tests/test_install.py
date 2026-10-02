"""Moving a journal home, and finding the one a previous download left.

The property every test here protects: NO JOURNAL WITH DATA IS EVER OVERWRITTEN
OR DELETED. A friend who unzips a new version must find their trades, and a
friend who clicks the wrong thing must still find them.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from optjournal import config, install


def _journal(directory: Path, statements: int = 1) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(directory / "journal.db")
    conn.execute("CREATE TABLE statements (source_file TEXT)")
    conn.executemany("INSERT INTO statements VALUES (?)",
                     [(f"s{i}",) for i in range(statements)])
    conn.commit()
    conn.close()
    (directory / "raw").mkdir(exist_ok=True)
    (directory / "raw" / "activity-x.xml").write_text("<x/>")
    return directory


def _download(directory: Path, statements: int = 1) -> Path:
    """A folder that looks like an unzipped optjournal release with a journal."""
    _journal(directory, statements)
    (directory / "pyproject.toml").write_text('[project]\nname = "optjournal"\n')
    return directory


def test_data_home_prefers_the_override_then_a_journal_beside_the_code(
        tmp_path, monkeypatch):
    monkeypatch.setenv(config.HOME_ENV, str(tmp_path / "elsewhere"))
    assert config.data_home() == tmp_path / "elsewhere"

    monkeypatch.delenv(config.HOME_ENV)
    monkeypatch.setattr(config, "ROOT", _journal(tmp_path / "code"))
    assert config.data_home() == tmp_path / "code", (
        "an install with data beside the code must keep reading it until moved")

    monkeypatch.setattr(config, "ROOT", tmp_path / "bare")
    assert config.data_home() == config.platform_home()


def test_relocate_moves_every_data_file_and_leaves_the_code(tmp_path):
    source = _journal(tmp_path / "old")
    (source / ".optjournal.json").write_text("{}")
    (source / "src").mkdir()
    home = tmp_path / "home"

    moved = install.relocate(source, home)

    assert set(moved) == {"journal.db", "raw", ".optjournal.json"}
    assert (home / "raw" / "activity-x.xml").exists()
    assert not (source / "journal.db").exists()
    assert (source / "src").exists(), "the move reached the code"


def test_relocate_refuses_to_overwrite_a_journal_with_statements(tmp_path):
    source = _journal(tmp_path / "old")
    home = _journal(tmp_path / "home", statements=3)

    with pytest.raises(install.RelocateRefused):
        install.relocate(source, home)
    assert (source / "journal.db").exists(), "the refused move still moved"


def test_relocate_sets_an_empty_journal_aside_instead_of_deleting_it(tmp_path):
    source = _journal(tmp_path / "old")
    home = _journal(tmp_path / "home", statements=0)

    install.relocate(source, home)

    (aside,) = home.glob("replaced-*")
    assert (aside / "journal.db").exists()


def test_relocate_refuses_a_journal_that_is_open(tmp_path):
    source = _journal(tmp_path / "old")
    holder = sqlite3.connect(source / "journal.db")
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("BEGIN IMMEDIATE")          # a writer holding the journal
    try:
        with pytest.raises(install.RelocateRefused, match="in use"):
            install.relocate(source, tmp_path / "home")
    finally:
        holder.close()
    assert (source / "journal.db").exists()


def test_a_journal_merely_read_in_wal_mode_still_moves_whole(tmp_path):
    """The e2e failure: discovery's own read left `-wal`/`-shm` behind, and the
    old file check took that for a running server and refused every import."""
    source = _journal(tmp_path / "old")
    conn = sqlite3.connect(source / "journal.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("INSERT INTO statements VALUES ('only-in-wal')")
    conn.commit()
    conn.close()
    (source / "journal.db-shm").write_bytes(b"")     # what a read leaves behind

    install.relocate(source, tmp_path / "home")

    moved = sqlite3.connect(tmp_path / "home" / "journal.db")
    assert moved.execute("SELECT COUNT(*) FROM statements").fetchone()[0] == 2


def test_scanning_another_download_creates_no_files_in_it(tmp_path):
    old = _download(tmp_path / "Downloads" / "optjournal-main")
    conn = sqlite3.connect(old / "journal.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    before = sorted(p.name for p in old.iterdir())
    assert install.previous_journals(tmp_path / "home", search_root=tmp_path) == [old]
    assert sorted(p.name for p in old.iterdir()) == before


def test_previous_journals_finds_other_downloads_but_not_empty_ones(tmp_path):
    found = _download(tmp_path / "Downloads" / "optjournal-main")
    _download(tmp_path / "Desktop" / "optjournal-empty", statements=0)
    _journal(tmp_path / "Documents" / "not-optjournal")

    assert install.previous_journals(tmp_path / "home", search_root=tmp_path) == [found]


def test_previous_journals_finds_a_clone_in_the_home_folder_but_goes_no_deeper(tmp_path):
    """A friend who cloned from a new terminal has `~/optjournal`; walking the
    whole home folder would reach `~/Library` on every page load."""
    clone = _download(tmp_path / "optjournal")
    _download(tmp_path / "code" / "optjournal")

    assert install.previous_journals(tmp_path / "home", search_root=tmp_path) == [clone]


def test_previous_journals_offers_nothing_to_a_journal_with_data(tmp_path):
    _download(tmp_path / "Downloads" / "optjournal-main")
    home = _journal(tmp_path / "home")
    assert install.previous_journals(home, search_root=tmp_path) == []


def test_choose_import_accepts_only_a_journal_it_found(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(install, "previous_journals", lambda _h: [tmp_path / "a"])

    with pytest.raises(install.RelocateRefused):
        install.choose_import(tmp_path / "anything-else", home)
    install.choose_import(tmp_path / "a", home)
    assert json.loads((home / install.PENDING_IMPORT).read_text()) == {
        "source": str(tmp_path / "a")}


def test_prepare_carries_out_a_confirmed_import_once(tmp_path):
    source = _download(tmp_path / "Downloads" / "optjournal-main")
    home = tmp_path / "home"
    home.mkdir()
    (home / install.PENDING_IMPORT).write_text(json.dumps({"source": str(source)}))

    done = install.prepare(home, code_dir=tmp_path / "code")

    assert (home / "journal.db").exists() and done[0].startswith("imported")
    assert not (home / install.PENDING_IMPORT).exists(), "the choice would replay"


def test_prepare_moves_a_downloads_own_journal_home(tmp_path):
    code = _journal(tmp_path / "code")
    home = tmp_path / "home"
    install.prepare(home, code_dir=code)
    assert (home / "journal.db").exists() and not (code / "journal.db").exists()


def test_prepare_leaves_a_git_clones_journal_where_it_is(tmp_path):
    code = _journal(tmp_path / "code")
    (code / ".git").mkdir()
    install.prepare(tmp_path / "home", code_dir=code)
    assert (code / "journal.db").exists(), "a developer's journal was moved"


# --- a move that fails partway --------------------------------------------------
#
# The failure a friend meets is an antivirus scan or a OneDrive sync holding one
# file of the journal. Simulated by making every copy, move and rename of `raw`
# fail, whichever of them the move uses: the property is that the journal ends
# up whole in exactly one place, not how it got there.


def _lock(monkeypatch, blocked: Path, *, copies: bool = True) -> None:
    """Make moving `blocked` fail with PermissionError, as a held file does.

    `copies=False` still lets it be copied, so only taking it out of its folder
    fails: the case where the new home already holds a full copy.
    """
    def guard(real):
        def wrapper(src, dst, *args, **kwargs):
            if Path(src) == blocked:
                raise PermissionError(13, "held by another program", str(src))
            return real(src, dst, *args, **kwargs)
        return wrapper

    for module, name in ((os, "rename"), (os, "replace"), (shutil, "move")):
        monkeypatch.setattr(module, name, guard(getattr(module, name)))
    if copies:
        monkeypatch.setattr(shutil, "copytree", guard(shutil.copytree))


def _tree(directory: Path) -> dict[str, bytes]:
    return {p.relative_to(directory).as_posix(): p.read_bytes()
            for p in sorted(directory.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("copies", [True, False], ids=["copying-fails", "removing-fails"])
def test_a_move_that_fails_partway_leaves_the_whole_journal_where_it_was(
        tmp_path, monkeypatch, copies):
    """M19: `journal.db` moved, then `raw` failed, and the journal was split.

    The data home then resolved back to the code folder, which had lost its
    database, and every later `prepare` refused. Now the source is untouched,
    the home is exactly as it was (its empty journal included), and `prepare`
    reports one line instead of a traceback.
    """
    code = _journal(tmp_path / "code")
    (code / ".optjournal.json").write_text('{"query_id": "1"}')
    home = _journal(tmp_path / "home", statements=0)
    code_before, home_before = _tree(code), _tree(home)
    _lock(monkeypatch, code / "raw", copies=copies)

    done = install.prepare(home, code_dir=code)

    assert len(done) == 1 and done[0].startswith("journal left beside the code"), done
    assert "held by another program" in done[0] and "\n" not in done[0]
    assert _tree(code) == code_before, "the source lost part of the journal"
    assert _tree(home) == home_before, "the home kept part of a move that was undone"
    assert sorted(p.name for p in code.iterdir()) == [".optjournal.json", "journal.db", "raw"]
    assert sorted(p.name for p in home.iterdir()) == ["journal.db", "raw"]
    monkeypatch.delenv(config.HOME_ENV, raising=False)
    monkeypatch.setattr(config, "ROOT", code)
    assert config.data_home() == code


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                    reason="a read-only folder is a POSIX permission, and root ignores it")
def test_a_failed_move_leaves_no_copy_behind_and_the_next_start_finishes_it(tmp_path):
    """A real failure, not a simulated one: a read-only `raw` copies fine but
    its copy cannot be renamed into place. The copy must not stay in the home
    (a failure that repeats at every start would leave a journal's worth each
    time), and the next start, even within the same second, must move it all."""
    code = _journal(tmp_path / "code")
    home = tmp_path / "home"
    (code / "raw").chmod(0o555)
    try:
        done = install.prepare(home, code_dir=code)
        assert done[0].startswith("journal left beside the code"), done
        assert sorted(p.name for p in home.iterdir()) == [], "the failed move left files"
        assert sorted(p.name for p in code.iterdir()) == ["journal.db", "raw"]
    finally:
        (code / "raw").chmod(0o755)

    done = install.prepare(home, code_dir=code)

    assert done[0].startswith("moved journal.db, raw"), done
    assert sorted(p.name for p in home.iterdir()) == ["journal.db", "raw"]
    assert sorted(p.name for p in code.iterdir()) == []


def test_an_original_an_earlier_rollback_could_not_put_back_blocks_the_move(tmp_path):
    """Two failures in a row (the move, then its own undo) can leave an ORIGINAL
    inside `.moved-<stamp>`. Moving what is still beside the code would then
    complete a split journal, with the only real `journal.db` hidden where
    nothing reads it. So the move is refused and the folder is named."""
    code = _journal(tmp_path / "code", statements=3)
    hidden = code / ".moved-20260930T000000Z"
    hidden.mkdir()
    (code / "journal.db").rename(hidden / "journal.db")
    before = _tree(code)
    home = tmp_path / "home"

    with pytest.raises(install.RelocateRefused, match=r"\.moved-20260930T000000Z"):
        install.relocate(code, home)

    assert _tree(code) == before, "the move touched the code folder"
    assert not home.exists() or not any(home.iterdir()), "the move wrote into the home"


def test_a_staging_folder_an_earlier_attempt_left_is_cleared(tmp_path):
    """It only ever holds copies, so it is nobody's only copy of anything."""
    home = tmp_path / "home"
    leftover = home / f"{install.STAGING_PREFIX}old" / "raw"
    leftover.mkdir(parents=True)
    (leftover / "activity-x.xml").write_text("<x/>")
    install.relocate(_journal(tmp_path / "old"), home)
    assert sorted(p.name for p in home.iterdir()) == ["journal.db", "raw"]


def test_a_move_that_succeeds_leaves_nothing_behind_in_either_folder(tmp_path):
    code = _journal(tmp_path / "code")
    (code / "src").mkdir()
    (code / "src" / "app.py").write_text("code")
    before = _tree(code)
    home = tmp_path / "home"

    assert install.relocate(code, home) == ["journal.db", "raw"]

    assert sorted(p.name for p in code.iterdir()) == ["src"], "a staging folder was left"
    assert sorted(p.name for p in home.iterdir()) == ["journal.db", "raw"]
    assert _tree(home) == {k: v for k, v in before.items() if not k.startswith("src/")}


def test_an_import_sets_aside_the_replaced_journals_wal_with_it(tmp_path):
    """M20: the empty journal's `-wal`/`-shm` stayed beside the imported one.

    SQLite then applied that WAL to a database it does not belong to, and the
    import opened as "database disk image is malformed". Built here the way a
    killed server leaves it: a WAL still holding frames, and no connection open.
    """
    source = _journal(tmp_path / "old", statements=3)
    home = tmp_path / "home"
    home.mkdir()
    conn = sqlite3.connect(home / "journal.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE statements (source_file TEXT)")
    conn.execute("CREATE TABLE server_only (x)")
    conn.commit()
    left = {name: (home / name).read_bytes() for name in ("journal.db-wal", "journal.db-shm")}
    conn.close()
    for name, body in left.items():                   # what a SIGKILL leaves
        (home / name).write_bytes(body)

    install.relocate(source, home)

    moved = sqlite3.connect(home / "journal.db")
    try:
        assert moved.execute("SELECT COUNT(*) FROM statements").fetchone()[0] == 3
        assert moved.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        moved.close()
    (aside,) = home.glob("replaced-*")
    replaced = sqlite3.connect(aside / "journal.db")
    try:
        assert replaced.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'server_only'"
        ).fetchone()[0] == 1, "the set-aside journal lost what was only in its WAL"
    finally:
        replaced.close()


def test_an_import_refuses_while_the_replaced_journal_is_open(tmp_path):
    """The journal being set aside is released first, like the one being moved."""
    source = _journal(tmp_path / "old")
    home = _journal(tmp_path / "home", statements=0)
    holder = sqlite3.connect(home / "journal.db")
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(install.RelocateRefused, match="in use"):
            install.relocate(source, home)
    finally:
        holder.close()
    assert (source / "journal.db").exists() and not list(home.glob("replaced-*"))


def test_an_unreadable_journal_in_the_home_is_set_aside_not_refused(tmp_path):
    """It has no statements anyone could lose, and it is kept, not deleted."""
    source = _journal(tmp_path / "old")
    home = tmp_path / "home"
    home.mkdir()
    (home / "journal.db").write_bytes(b"garbage" * 1000)
    (home / "journal.db-wal").write_bytes(b"stale")

    install.relocate(source, home)

    (aside,) = home.glob("replaced-*")
    assert (aside / "journal.db").read_bytes() == b"garbage" * 1000
    assert not (home / "journal.db-wal").exists() and not (home / "journal.db-shm").exists()
    assert install._statement_count(home / "journal.db") == 1


def test_a_corrupt_journal_to_import_is_reported_once_not_on_every_start(tmp_path):
    """L21: `prepare` crashed on a corrupt source and kept `.pending-import.json`,
    so it crashed again on every start."""
    source = _download(tmp_path / "Downloads" / "optjournal-main")
    (source / "journal.db").write_bytes(b"garbage" * 1000)
    home = tmp_path / "home"
    home.mkdir()
    (home / install.PENDING_IMPORT).write_text(json.dumps({"source": str(source)}))

    done = install.prepare(home, code_dir=tmp_path / "code")

    assert done[0].startswith("import not done") and "not a readable journal" in done[0]
    assert not (home / install.PENDING_IMPORT).exists()
    assert (source / "journal.db").exists(), "an unreadable journal must not be moved"


def test_a_corrupt_journal_beside_the_code_is_reported_and_left(tmp_path):
    code = _journal(tmp_path / "code")
    (code / "journal.db").write_bytes(b"this is not a database" * 100)

    done = install.prepare(tmp_path / "home", code_dir=code)

    assert done[0].startswith("journal left beside the code") and "not a readable" in done[0]
    assert (code / "journal.db").exists() and (code / "raw").exists()


_ODD = ["hash #1", "pct 100%", "pct%41", "with space", "ünïcode"]
if sys.platform != "win32":                  # `?` is not allowed in a Windows name
    _ODD.append("q?mark")


@pytest.mark.parametrize("folder", _ODD)
def test_a_journal_in_a_folder_with_uri_characters_is_read_and_left_untouched(
        tmp_path, folder):
    """L22: the path went into a `file:` URI unescaped. `#` or `?` cut it short,
    dropping `mode=ro`, so the read opened (and created) a different file, read
    no statements, and wrote into someone else's folder."""
    old = _download(tmp_path / "Downloads" / folder, statements=2)
    before = sorted(p.name for p in old.parent.rglob("*"))

    assert install._statement_count(old / "journal.db", foreign=True) == 2
    assert install._statement_count(old / "journal.db") == 2
    assert sorted(p.name for p in old.parent.rglob("*")) == before
