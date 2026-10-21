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
import time
import zipfile
from contextlib import contextmanager, suppress
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
    # Windows has no executable bit to keep, and never runs the macOS Start file.
    assert os.name == "nt" or (root / "Start optjournal.command").stat().st_mode & 0o111, (
        "the macOS Start file lost its executable bit, so it no longer opens")
    assert not (root / updates.STAGING).exists() and not list(root.glob(".update-old*"))


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


def test_a_download_that_is_not_a_zip_stages_nothing(tmp_path, monkeypatch, supervised):
    """L27 (its update part): a non-zip body (an error or rate-limit page) is a
    failed download, which the endpoint answers as a retryable 502, and nothing
    is staged from it."""
    root = _install(tmp_path)
    monkeypatch.setattr(updates, "_get", lambda _url, limit: b"<html>rate limited</html>")
    with pytest.raises(zipfile.BadZipFile, match="not a release archive"):
        updates.stage(_release(), root=root)
    assert not (root / updates.STAGING).exists()


def test_a_release_whose_code_is_another_version_is_refused(tmp_path, monkeypatch, supervised):
    root = _install(tmp_path)
    monkeypatch.setattr(updates, "_get", lambda _url, limit: _zipball("0.1.5"))
    with pytest.raises(updates.UpdateRefused, match="holds version"):
        updates.stage(_release("0.2.0"), root=root)


def test_a_release_of_another_project_is_refused(tmp_path, monkeypatch, supervised):
    root = _install(tmp_path)
    other = b'[project]\nname = "not-optjournal"\nversion = "0.2.0"\n'
    monkeypatch.setattr(updates, "_get",
                        lambda _url, limit: _zipball(extra={"pyproject.toml": other}))
    with pytest.raises(updates.UpdateRefused, match="not optjournal"):
        updates.stage(_release(), root=root)
    assert not (root / updates.STAGING).exists()


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


def _answering(monkeypatch, body: bytes) -> list[tuple[str, float]]:
    """`urlopen` answering `body`. Returns each URL it was asked, with its timeout."""
    asked: list[tuple[str, float]] = []

    def urlopen(request, *, timeout):
        asked.append((request.full_url, timeout))
        return io.BytesIO(body)

    monkeypatch.setattr(updates, "urlopen", urlopen)
    return asked


def test_a_download_one_byte_over_its_limit_is_refused(monkeypatch):
    asked = _answering(monkeypatch, b"x" * 11)
    with pytest.raises(updates.UpdateRefused, match="more than 10 bytes"):
        updates._get("https://example.com/x.zip", limit=10)
    assert asked == [("https://example.com/x.zip", updates.TIMEOUT_S)]
    _answering(monkeypatch, b"x" * 10)
    assert updates._get("https://example.com/x.zip", limit=10) == b"x" * 10


#: The real one: `conftest` replaces `updates.latest_release` in every test.
_LATEST_RELEASE = updates.latest_release


def test_githubs_latest_release_is_read_by_its_tag_and_a_tag_that_is_no_version_refused(
        monkeypatch):
    def latest(tag: str) -> bytes:
        return json.dumps({
            "tag_name": tag, "body": "What changed.",
            "zipball_url": f"https://api.github.com/repos/robert-mp/optjournal/zipball/{tag}",
            "html_url": f"https://github.com/robert-mp/optjournal/releases/tag/{tag}",
        }).encode()

    monkeypatch.delenv(updates.REPO_ENV, raising=False)
    asked = _answering(monkeypatch, latest("v0.5.0"))
    assert _LATEST_RELEASE() == updates.Release(
        version="0.5.0", notes="What changed.",
        zip_url="https://api.github.com/repos/robert-mp/optjournal/zipball/v0.5.0",
        page_url="https://github.com/robert-mp/optjournal/releases/tag/v0.5.0")
    assert asked == [("https://api.github.com/repos/robert-mp/optjournal/releases/latest",
                      updates.TIMEOUT_S)]
    _answering(monkeypatch, latest("nightly"))
    with pytest.raises(updates.UpdateRefused, match="'nightly' is not a version"):
        _LATEST_RELEASE()


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


#: `os.replace` itself, before any test wraps it: the launcher's `os` is this one.
_REPLACE = os.replace


def _replace_failing(monkeypatch, launcher, fail) -> list[int]:
    """The launcher's `os.replace`, raising `fail(n, src)` at its n-th call when
    that is an exception rather than None. Returns the count, as a one-item list."""
    calls = [0]

    def replace(src, dst):
        calls[0] += 1
        exc = fail(calls[0], Path(src))
        if exc is not None:
            raise exc
        return _REPLACE(src, dst)

    monkeypatch.setattr(launcher.os, "replace", replace)
    return calls


def _a_clean_update(root: Path, monkeypatch, launcher) -> tuple[int, dict[str, bytes | None]]:
    """How many renames a whole swap makes, and the install it leaves."""
    updates.stage(_release(), root=root)
    calls = _replace_failing(monkeypatch, launcher, lambda _n, _src: None)
    assert launcher.apply_staged(root) == "0.2.0"
    assert calls[0] >= 4
    return calls[0], _code(root)


def test_a_swap_that_fails_at_any_step_puts_the_old_code_back(
        tmp_path, monkeypatch, supervised):
    """Every rename of the swap is made to fail in turn, and each time the install
    must be exactly the old version, with nothing left stranded mid-swap."""
    monkeypatch.setattr(updates, "_get", lambda _url, limit: _zipball())
    launcher = _launcher()
    total, _new = _a_clean_update(_install(tmp_path / "clean"), monkeypatch, launcher)

    for failing in range(1, total + 1):
        root = _install(tmp_path / f"fail-{failing}")
        before = _code(root)
        updates.stage(_release(), root=root)
        fail = {failing: OSError("in use")}
        _replace_failing(monkeypatch, launcher, lambda n, _src, fail=fail: fail.get(n))
        assert launcher.apply_staged(root) is None, failing
        assert _code(root) == before, f"step {failing} of {total} left a mixed install"
        assert not list(root.glob(f"{launcher.OLD}*/*")), "old code stranded mid-swap"


class _Died(BaseException):
    """The launcher's process ending: its window closed, or Ctrl+C, which is no
    OSError. Raised from inside a rename, so nothing after it runs."""


def test_a_swap_cut_off_at_any_step_is_put_right_by_the_next_starts(
        tmp_path, monkeypatch, supervised):
    """Cut off after `src` had moved into `.update-old`, the next start found no
    `.ready`, deleted the staged release, and left an install with no `src/`.

    Now the next start puts the old code back before it tries the release
    again, so when that try cannot install anything the old version runs, and
    the start after it installs the new one."""
    monkeypatch.setattr(updates, "_get", lambda _url, limit: _zipball())
    launcher = _launcher()
    total, new = _a_clean_update(_install(tmp_path / "clean"), monkeypatch, launcher)

    def blocked(_n, src):
        return OSError("in use") if src.parent.name == updates.STAGING else None

    for step in range(1, total + 1):
        root = _install(tmp_path / f"cut-{step}")
        updates.stage(_release(), root=root)
        cut = {step: _Died()}
        _replace_failing(monkeypatch, launcher, lambda n, _src, cut=cut: cut.get(n))
        with pytest.raises(_Died):
            launcher.apply_staged(root)

        _replace_failing(monkeypatch, launcher, blocked)
        assert launcher.apply_staged(root) is None
        code = sorted(p.name for p in (root / "src" / "optjournal").iterdir())
        assert (updates.current_version(root), code) == ("0.1.0", ["old.py"]), (
            f"cut at step {step} of {total}, then a mixed install")
        assert (root / "journal.db").read_bytes() == b"my trades"

        monkeypatch.setattr(launcher.os, "replace", _REPLACE)
        assert launcher.apply_staged(root) == "0.2.0", f"cut at step {step} of {total}"
        assert _code(root) == new, f"cut at step {step} of {total}, then a mixed install"
        assert not (root / updates.STAGING).exists() and not list(root.glob(f"{launcher.OLD}*"))


def test_an_undo_that_meets_a_locked_file_is_finished_by_the_next_start(
        tmp_path, monkeypatch, supervised):
    """On Windows the undo of a failed swap can meet a file an antivirus scan
    holds. That PermissionError escaped the undo and crashed the launcher, and
    the next start deleted the old code waiting in `.update-old`: pyproject.toml
    said the new version over the old `src/`, so the banner never offered the
    update again. Now the undo goes on past it, and the next start finishes it
    and installs the release."""
    monkeypatch.setattr(updates, "_get", lambda _url, limit: _zipball())
    launcher = _launcher()
    total, new = _a_clean_update(_install(tmp_path / "clean"), monkeypatch, launcher)

    for failing in range(1, total + 1):
        root = _install(tmp_path / f"held-{failing}")
        updates.stage(_release(), root=root)
        # The rename after the failing one is the undo's first.
        fail = {failing: OSError("in use"), failing + 1: PermissionError("scanning")}
        _replace_failing(monkeypatch, launcher, lambda n, _src, fail=fail: fail.get(n))
        assert launcher.apply_staged(root) is None, failing
        monkeypatch.setattr(launcher.os, "replace", _REPLACE)
        assert launcher.apply_staged(root) == "0.2.0", f"failed at step {failing} of {total}"
        assert _code(root) == new, f"failed at step {failing} of {total}, then a mixed install"


#: `os.unlink` itself, for the same reason as `_REPLACE`.
_UNLINK = os.unlink


def test_a_file_held_after_a_good_swap_does_not_stop_the_next_update(
        tmp_path, monkeypatch, supervised):
    """A file another process holds (an antivirus scan, on Windows) can outlive
    the delete of the old code after a good swap. That old code is no undo for
    a second update staged in the same window: it goes in while the files are
    still held, none of them comes back into the install, and a later start
    deletes them once they are let go."""
    launcher = _launcher()
    root = _install(tmp_path)

    def held(path, *args, **kwargs):
        if os.path.basename(path) in ("old.py", "pyproject.toml"):
            raise PermissionError("scanning")
        return _UNLINK(path, *args, **kwargs)

    monkeypatch.setattr(updates, "_get", lambda _url, limit: _zipball("0.2.0"))
    updates.stage(_release("0.2.0"), root=root)
    monkeypatch.setattr(os, "unlink", held)
    assert launcher.apply_staged(root) == "0.2.0"

    monkeypatch.setattr(updates, "_get", lambda _url, limit: _zipball("0.3.0"))
    updates.stage(_release("0.3.0"), root=root)
    version = launcher.apply_staged(root)
    code = sorted(p.name for p in (root / "src" / "optjournal").iterdir())
    assert (updates.current_version(root), code) == ("0.3.0", ["new.py"])
    assert version == "0.3.0"

    monkeypatch.setattr(os, "unlink", _UNLINK)
    assert launcher.apply_staged(root) is None
    assert updates.current_version(root) == "0.3.0"
    assert sorted(p.name for p in root.iterdir() if p.name.startswith(".update-")) == []


def test_a_ready_marker_that_names_no_version_is_never_made_a_path(
        tmp_path, monkeypatch, supervised):
    """The old code's folder is named for the version `READY` holds, so a name
    that is not a plain version swaps nothing in and writes nothing outside."""
    monkeypatch.setattr(updates, "_get", lambda _url, limit: _zipball())
    root = _install(tmp_path)
    before = _code(root)
    updates.stage(_release(), root=root)
    (root / updates.STAGING / updates.READY).write_text("../../escape")
    assert _launcher().apply_staged(root) is None
    assert _code(root) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["optjournal-main"]


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


def test_a_failed_check_is_asked_again_and_a_successful_one_is_kept(tmp_path, monkeypatch):
    """A failed check was kept for six hours like a good one, so a server
    started offline showed no banner for six hours after the network came back.
    Only an answer is kept now; GitHub is still asked once per six hours."""
    from urllib.error import URLError

    asked: list[int] = []

    def latest():
        asked.append(1)
        if len(asked) == 1:
            raise URLError("offline")
        return _release("0.2.0")

    monkeypatch.setattr(updates, "latest_release", latest)
    root = _install(tmp_path)
    assert "could not check" in updates.check(root=root)["error"]
    assert updates.check(root=root)["available"] is True, "the failure was kept"
    assert updates.check(root=root)["available"] is True
    assert len(asked) == 2, "the answer was not kept"


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


def test_the_launchers_probe_is_bounded_by_a_program_that_streams(tmp_path):
    """A program streaming on the port (a byte every tenth of a second) kept the
    probe reading for as long as it streamed, because the timeout bounds each
    read rather than the whole probe. Now it gives up at its own deadline."""
    class Stream(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib naming
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            with suppress(OSError):
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
        started = time.monotonic()
        seen = _launcher()._whats_on(int(server.server_address[1]), timeout_s=0.5)
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()
    assert seen == "other"
    assert elapsed < 2.0, f"the probe read the stream for {elapsed:.1f}s"


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
