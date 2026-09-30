"""Updating a downloaded ZIP from a GitHub release, end to end but offline.

`updates.stage` downloads and verifies; `launcher/app.py` swaps. The tests run
both halves against an archive built here in the shape GitHub serves, and the
property they protect is the one a friend would notice: after an update the
journal is exactly where it was and the code is the new version, or nothing
changed at all.
"""

from __future__ import annotations

import http.server
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import zipfile
from contextlib import contextmanager
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


def test_a_download_that_is_not_a_zip_is_refused(tmp_path, monkeypatch, supervised):
    """L27 (its update part): a non-zip body raised `BadZipFile`, which the
    server's update endpoint does not catch, so the page got no reply at all."""
    root = _install(tmp_path)
    monkeypatch.setattr(updates, "_get", lambda _url, limit: b"<html>rate limited</html>")
    with pytest.raises(updates.UpdateRefused, match="not a release archive"):
        updates.stage(_release(), root=root)
    assert not (root / updates.STAGING).exists()


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


def _code(root: Path) -> dict[str, bytes | None]:
    """Everything in `root` but the swap's own two folders, by relative path."""
    return {p.relative_to(root).as_posix(): p.read_bytes() if p.is_file() else None
            for p in sorted(root.rglob("*"))
            if not p.relative_to(root).parts[0].startswith((".update-",))}


def test_a_swap_that_fails_at_any_step_puts_the_old_code_back(
        tmp_path, monkeypatch, supervised):
    """Every rename of the swap is made to fail in turn, and each time the install
    must be exactly the old version, with nothing left stranded mid-swap."""
    monkeypatch.setattr(updates, "_get", lambda _url, limit: _zipball())
    launcher = _launcher()
    real = launcher.os.replace
    steps = {"n": 0}

    def counting(src, dst):
        steps["n"] += 1
        return real(src, dst)

    counted = _install(tmp_path / "counted")
    updates.stage(_release(), root=counted)
    monkeypatch.setattr(launcher.os, "replace", counting)
    assert launcher.apply_staged(counted) == "0.2.0"
    total, steps["n"] = steps["n"], 0
    assert total >= 4

    for failing in range(1, total + 1):
        root = _install(tmp_path / f"fail-{failing}")
        before = _code(root)
        updates.stage(_release(), root=root)

        def flaky(src, dst, failing=failing):
            steps["n"] += 1
            if steps["n"] == failing:
                raise OSError("file in use")
            return real(src, dst)

        steps["n"] = 0
        monkeypatch.setattr(launcher.os, "replace", flaky)
        assert launcher.apply_staged(root) is None, failing
        assert _code(root) == before, f"step {failing} of {total} left a mixed install"
        assert not list((root / launcher.OLD).glob("*")), "old code stranded in .update-old"


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


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _other_program():
    """Something else answering HTTP on a port, as AnkiConnect does on 8765."""
    class Anki(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib naming
            body = b'{"result": null, "error": "unsupported action"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Anki)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def test_the_launcher_knows_optjournal_from_anything_else_on_its_port(tmp_path):
    """L20: anything listening on the port was taken for optjournal, and the
    browser was opened at it. Pinned against the real server, so the page and
    the launcher cannot drift apart on what identifies it."""
    from optjournal import web

    launcher = _launcher()
    with web.serve_ephemeral(db_path=tmp_path / "journal.db", archive_dir=tmp_path / "raw") as base:
        assert launcher._whats_on(int(base.rsplit(":", 1)[1].strip("/"))) == "optjournal"
    with _other_program() as port:
        assert launcher._whats_on(port) == "other"
    with socket.socket() as silent:                   # accepts, never answers
        silent.bind(("127.0.0.1", 0))
        silent.listen()
        assert launcher._whats_on(silent.getsockname()[1], timeout_s=0.5) == "other"
    assert launcher._whats_on(_free_port()) is None


def _fake_uv_for_launcher(tmp_path: Path, serve_codes: list[int]) -> Path:
    """A `uv` that logs each call and exits `serve` with the next code given."""
    log = tmp_path / "uv.log"
    codes = tmp_path / "codes.json"
    codes.write_text(json.dumps(serve_codes))
    script = tmp_path / "uv"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(log)!r}, 'a') as f:\n"
        "    f.write(json.dumps([os.getcwd(), *sys.argv[1:]]) + '\\n')\n"
        "if 'serve' in sys.argv:\n"
        f"    left = json.loads(open({str(codes)!r}).read())\n"
        f"    open({str(codes)!r}, 'w').write(json.dumps(left[1:]))\n"
        "    sys.exit(left[0])\n"
    )
    script.chmod(0o755)
    return log


def _run_launcher(monkeypatch, tmp_path, port: int):
    monkeypatch.setenv("OPTJOURNAL_PORT", str(port))
    launcher = _launcher()
    opened: list[str] = []
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    monkeypatch.setattr(launcher, "ROOT", _install(tmp_path))
    return launcher, opened


def test_the_launcher_refuses_a_port_another_program_holds(tmp_path, monkeypatch, capsys):
    with _other_program() as port:
        launcher, opened = _run_launcher(monkeypatch, tmp_path, port)
        monkeypatch.setenv("UV", str(tmp_path / "no-such-uv"))
        assert launcher.main() == 1
    assert opened == [], "the browser was opened at another program"
    assert "another program" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in uv is a script with a shebang")
def test_the_launcher_restarts_after_an_update_and_never_relocks(tmp_path, monkeypatch):
    """Exit code RESTART starts the app again; anything else ends the loop. And
    every uv call is `--frozen` (M17): a start must not rewrite `uv.lock`."""
    launcher, opened = _run_launcher(monkeypatch, tmp_path, _free_port())
    log = _fake_uv_for_launcher(tmp_path, [launcher.RESTART, 0])
    monkeypatch.setenv("UV", str(tmp_path / "uv"))

    assert launcher.main() == 0

    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [c[-1] if "prepare" in c else "serve" for c in calls] == [
        "prepare", "serve", "prepare", "serve"]
    assert all(c[0] == str(launcher.ROOT) for c in calls)
    assert all("--frozen" in c for c in calls), calls
    assert opened == [], "a server that never answered was opened in the browser"


def _uv_lock_check(project: Path, *, offline: bool) -> subprocess.CompletedProcess[str]:
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        pytest.skip("uv is not on PATH")
    argv = [uv, "lock", "--check", *(["--offline"] if offline else [])]
    try:
        return subprocess.run(argv, cwd=project, capture_output=True, text=True,
                              timeout=120, check=False)
    except subprocess.TimeoutExpired:
        pytest.skip("uv lock --check timed out (no network?)")


def test_the_committed_lockfile_matches_the_committed_pyproject(tmp_path):
    """M17: a version bump committed without `uv lock` ships a stale lock.

    Every friend's install then re-locks it: `update` and the launcher install
    with `--frozen` and leave it alone, but a hand-typed `uv run` rewrites
    `uv.lock`, and that modified tracked file refuses the next update.

    The COMMITTED pair, from git, not the working tree: `uv run pytest` (and CI's
    first `uv run`) re-locks the working tree before this test can see it. Checked
    offline first, which needs no network when the lock is current; only when
    that cannot decide (a dependency not in this machine's cache) is the index
    asked, and the test is skipped if it cannot be reached.
    """
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    for name in ("pyproject.toml", "uv.lock"):
        shown = subprocess.run(["git", "show", f"HEAD:{name}"], cwd=ROOT,
                               capture_output=True, check=False)
        if shown.returncode:
            pytest.skip(f"git cannot show HEAD:{name}")
        (tmp_path / name).write_bytes(shown.stdout)

    checked = _uv_lock_check(tmp_path, offline=True)
    if checked.returncode and "needs to be updated" not in checked.stderr + checked.stdout:
        checked = _uv_lock_check(tmp_path, offline=False)
        if checked.returncode and "needs to be updated" not in checked.stderr + checked.stdout:
            pytest.skip(f"uv could not check the lock: {checked.stderr.strip()[-300:]}")
    assert checked.returncode == 0, (
        "the committed uv.lock does not match the committed pyproject.toml: run "
        "`uv lock` and commit uv.lock with the change")


def test_the_start_files_keep_their_line_endings_and_the_mac_one_its_mode():
    """cmd.exe misreads a `.bat` with bare LF, bash a `.command` with CR, and a
    `.command` without its executable bit does not open on double-click. Kept by
    `.gitattributes` and the index; a release is `git archive` of both."""
    bat = (ROOT / "Start optjournal.bat").read_bytes()
    command = (ROOT / "Start optjournal.command").read_bytes()
    assert bat.count(b"\r\n") == bat.count(b"\n") > 0, "the .bat lost its CRLF"
    assert b"\r" not in command, "the .command has CR line endings"
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout, so there is no index to read the mode from")
    staged = subprocess.run(["git", "ls-files", "-s", "Start optjournal.command"],
                            cwd=ROOT, capture_output=True, text=True, check=False)
    assert staged.stdout.startswith("100755"), staged.stdout


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
