"""Updating a downloaded ZIP from a GitHub release, end to end but offline.

`updates.stage` downloads and verifies; `launcher/app.py` swaps. The tests run
both halves against an archive built here in the shape GitHub serves, and the
property they protect is the one a friend would notice: after an update the
journal is exactly where it was and the code is the new version, or nothing
changed at all.
"""

from __future__ import annotations

import importlib.util
import io
import zipfile
from pathlib import Path

import pytest

from optjournal import cli, updates
from optjournal.config import ROOT


def _launcher():
    spec = importlib.util.spec_from_file_location("launcher_app", ROOT / "launcher" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _zipball(version: str = "0.2.0", *, extra: dict[str, bytes] | None = None,
             top: str = "robert-mp-optjournal-abc1234") -> bytes:
    files = {
        "pyproject.toml": f'[project]\nname = "optjournal"\nversion = "{version}"\n'.encode(),
        "src/optjournal/new.py": b"NEW = True\n",
        "Start optjournal.command": b"#!/bin/bash\necho hi\n",
        **(extra or {}),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(zipfile.ZipInfo(f"{top}/"), b"")
        for name, body in files.items():
            info = zipfile.ZipInfo(f"{top}/{name}")
            info.external_attr = (0o755 if name.endswith(".command") else 0o644) << 16
            archive.writestr(info, body)
    return buf.getvalue()


def _install(tmp_path: Path) -> Path:
    """A downloaded 0.1.0 with a journal beside its code, the worst case."""
    root = tmp_path / "optjournal-main"
    (root / "src" / "optjournal").mkdir(parents=True)
    (root / "src" / "optjournal" / "old.py").write_text("OLD = True\n")
    (root / "pyproject.toml").write_text('[project]\nname = "optjournal"\nversion = "0.1.0"\n')
    (root / "journal.db").write_bytes(b"my trades")
    (root / "raw").mkdir()
    (root / "raw" / "activity-1.xml").write_text("<statement/>")
    return root


@pytest.fixture()
def supervised(monkeypatch):
    monkeypatch.setenv("OPTJOURNAL_SUPERVISED", "1")


def _release(version: str = "0.2.0") -> updates.Release:
    return updates.Release(version=version, notes="n", page_url="",
                           zip_url="https://api.github.com/repos/x/y/zipball/v0.2.0")


def test_an_update_replaces_the_code_and_leaves_the_journal(tmp_path, monkeypatch, supervised):
    root = _install(tmp_path)
    monkeypatch.setattr(updates, "_get", lambda _url, limit: _zipball())

    updates.stage(_release(), root=root)
    assert (root / "src" / "optjournal" / "old.py").exists(), (
        "staging changed the running code; only the launcher may")
    assert _launcher().apply_staged(root) == "0.2.0"

    assert updates.current_version(root) == "0.2.0"
    assert (root / "src" / "optjournal" / "new.py").exists()
    assert not (root / "src" / "optjournal" / "old.py").exists(), "src/ was merged, not replaced"
    assert (root / "journal.db").read_bytes() == b"my trades"
    assert (root / "raw" / "activity-1.xml").exists()
    assert (root / "Start optjournal.command").stat().st_mode & 0o111, (
        "the macOS Start file lost its executable bit, so it no longer opens")
    assert not (root / updates.STAGING).exists() and not (root / ".update-old").exists()


def test_a_release_that_names_journal_data_is_refused(tmp_path, monkeypatch, supervised):
    root = _install(tmp_path)
    monkeypatch.setattr(updates, "_get",
                        lambda _url, limit: _zipball(extra={"journal.db": b"empty"}))
    with pytest.raises(updates.UpdateRefused, match="journal data"):
        updates.stage(_release(), root=root)
    assert (root / "journal.db").read_bytes() == b"my trades"


@pytest.mark.parametrize("name", ["../escape.py", "/abs.py"])
def test_a_path_outside_the_folder_is_refused(tmp_path, monkeypatch, supervised, name):
    root = _install(tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("top/pyproject.toml", '[project]\nname="optjournal"\nversion="0.2.0"\n')
        archive.writestr(f"top/{name}" if name.startswith("..") else name, b"x")
    monkeypatch.setattr(updates, "_get", lambda _url, limit: buf.getvalue())
    with pytest.raises(updates.UpdateRefused):
        updates.stage(_release(), root=root)
    assert not (tmp_path / "escape.py").exists()


def test_a_release_whose_code_is_another_version_is_refused(tmp_path, monkeypatch, supervised):
    root = _install(tmp_path)
    monkeypatch.setattr(updates, "_get", lambda _url, limit: _zipball("0.1.5"))
    with pytest.raises(updates.UpdateRefused, match="holds version"):
        updates.stage(_release("0.2.0"), root=root)


def test_a_git_clone_is_never_updated_from_a_release(tmp_path, supervised):
    root = _install(tmp_path)
    (root / ".git").mkdir()
    with pytest.raises(updates.UpdateRefused, match="git clone"):
        updates.stage(_release(), root=root)


def test_an_unsupervised_server_cannot_restart_so_it_does_not_offer(tmp_path, monkeypatch):
    monkeypatch.delenv("OPTJOURNAL_SUPERVISED", raising=False)
    assert "Start file" in updates.cannot_apply(_install(tmp_path))


def test_downloads_must_be_https():
    with pytest.raises(updates.UpdateRefused, match="HTTPS"):
        updates._get("http://example.com/x.zip", limit=10)


def test_an_interrupted_download_is_discarded_not_applied(tmp_path):
    root = _install(tmp_path)
    (root / updates.STAGING / "src").mkdir(parents=True)     # no READY marker
    assert _launcher().apply_staged(root) is None
    assert (root / "src" / "optjournal" / "old.py").exists()
    assert not (root / updates.STAGING).exists()


def test_a_failed_swap_puts_the_old_code_back(tmp_path, monkeypatch, supervised):
    root = _install(tmp_path)
    monkeypatch.setattr(updates, "_get", lambda _url, limit: _zipball())
    updates.stage(_release(), root=root)
    launcher = _launcher()
    real = launcher.os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 4:          # partway through the second entry
            raise OSError("file in use")
        return real(src, dst)

    monkeypatch.setattr(launcher.os, "replace", flaky)
    assert launcher.apply_staged(root) is None
    assert updates.current_version(root) == "0.1.0"
    assert (root / "src" / "optjournal" / "old.py").exists()
    assert (root / "journal.db").read_bytes() == b"my trades"


@pytest.mark.parametrize(("latest", "available"), [("0.2.0", True), ("0.1.0", False),
                                                   ("0.0.9", False), ("0.10.0", True)])
def test_the_banner_offers_only_a_newer_version(tmp_path, monkeypatch, latest, available):
    root = _install(tmp_path)
    monkeypatch.setattr(updates, "latest_release", lambda: _release(latest))
    reply = updates.check(root=root, force=True)
    assert reply["available"] is available and reply["current"] == "0.1.0"


def test_offline_is_not_an_error_the_page_has_to_handle(tmp_path, monkeypatch):
    from urllib.error import URLError

    def offline():
        raise URLError("no network")

    monkeypatch.setattr(updates, "latest_release", offline)
    reply = updates.check(root=_install(tmp_path), force=True)
    assert reply["available"] is False and "could not check" in reply["error"]


def test_the_launcher_and_the_app_agree_on_the_contract():
    launcher = _launcher()
    assert (launcher.STAGING, launcher.READY) == (updates.STAGING, updates.READY)
    assert launcher.RESTART == cli.EXIT_RESTART


def test_the_launcher_imports_nothing_from_the_app():
    """On Windows a running process locks what it loaded, and an update replaces
    the app's dependencies: the launcher must hold none of them."""
    source = (ROOT / "launcher" / "app.py").read_text()
    assert "import optjournal" not in source and "from optjournal" not in source


def test_the_version_has_one_source():
    import optjournal

    assert optjournal.__version__ == updates.current_version()
