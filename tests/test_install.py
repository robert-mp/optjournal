"""Moving a journal home, and finding the one a previous download left.

The property every test here protects: NO JOURNAL WITH DATA IS EVER OVERWRITTEN
OR DELETED. A friend who unzips a new version must find their trades, and a
friend who clicks the wrong thing must still find them.
"""

from __future__ import annotations

import json
import sqlite3
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
