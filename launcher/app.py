"""What the Start files run: optjournal, kept running, restarted after an update.

STDLIB ONLY, AND IT IMPORTS NOTHING FROM OPTJOURNAL. Run with
`uv run --no-project`, so it holds no file inside the app's environment. That
matters on Windows, where a running process locks the files it has loaded, and
an update can replace the app's dependencies: importing `optjournal` here would
load pydantic's compiled module and stop `uv` from ever replacing it.

The loop, once per start and again after every update:

1. Swap in a staged release, if `optjournal.updates.stage` left a complete one.
2. `optjournal prepare`: move a journal home (see `optjournal.install`).
3. `optjournal serve`, until it exits. Exit code `RESTART` means "an update or
   an import is waiting, start me again"; anything else ends the loop.

Both run as `uv run --frozen`, so a start never rewrites `uv.lock`.
"""

from __future__ import annotations

import http.client
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"
#: `$OPTJOURNAL_PORT` moves it, for running a second copy beside the first.
PORT = int(os.environ.get("OPTJOURNAL_PORT") or 8765)
URL = f"http://{HOST}:{PORT}/"

#: Pinned to `optjournal.updates` by `tests/test_updates.py`.
STAGING = ".update-staging"
READY = ".ready"
#: Pinned to `optjournal.cli.EXIT_RESTART` by the same test.
RESTART = 75
#: Where the previous code waits while a swap is in progress, so a failed swap
#: can be put back.
OLD = ".update-old"


def apply_staged(root: Path = ROOT) -> str | None:
    """Swap a staged release into `root`. Returns its version, or None.

    Entry by entry at the top level: the current one moves into `OLD`, the
    staged one into its place. If any move fails, everything already swapped is
    put back, so the install is either the old version or the new one and never
    half of each. Entries the release does not contain are left alone, which is
    what keeps the journal's data untouched when it sits beside the code.
    """
    staging = root / STAGING
    ready = staging / READY
    if not ready.exists():
        shutil.rmtree(staging, ignore_errors=True)   # an interrupted download
        return None
    version = ready.read_text().strip()
    ready.unlink()
    old = root / OLD
    shutil.rmtree(old, ignore_errors=True)
    old.mkdir()
    swapped: list[str] = []
    try:
        for entry in sorted(p.name for p in staging.iterdir()):
            if (root / entry).exists():
                os.replace(root / entry, old / entry)
            swapped.append(entry)
            os.replace(staging / entry, root / entry)
    except OSError as exc:
        for entry in reversed(swapped):
            if (root / entry).exists() and not (staging / entry).exists():
                os.replace(root / entry, staging / entry)
            if (old / entry).exists():
                os.replace(old / entry, root / entry)
        print(f"Update to {version} failed and was undone: {exc}")
        return None
    shutil.rmtree(old, ignore_errors=True)
    shutil.rmtree(staging, ignore_errors=True)
    return version


def _whats_on(port: int = PORT, timeout_s: float = 2.0) -> str | None:
    """What answers on `port`: "optjournal", "other", or None when nothing does.

    Asked of the page itself, whose title names the app, rather than taken from
    an open port: other programs listen on 8765 too (AnkiConnect's default), and
    a browser opened at one of them shows a stranger's page or an error.
    `tests/test_updates.py` pins this against the real server.
    """
    try:
        with socket.create_connection((HOST, port), timeout=0.3):
            pass
    except OSError:
        return None
    # `http.client` rather than `urlopen`, which would send a loopback request
    # through any proxy set in the environment.
    conn = http.client.HTTPConnection(HOST, port, timeout=timeout_s)
    try:
        conn.request("GET", "/")
        head = conn.getresponse().read(8192).decode("utf-8", "replace")
    except (OSError, http.client.HTTPException):
        return "other"
    finally:
        conn.close()
    title = re.search(r"<title>([^<]*)</title>", head)
    return "optjournal" if title and "optjournal" in title.group(1) else "other"


def _open_when_ready(proc: subprocess.Popen[bytes], limit_s: float = 120) -> None:
    """Open the browser once the server answers. The first start builds the
    environment and can take a minute, so this waits rather than guessing."""
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline and proc.poll() is None:
        if _whats_on() == "optjournal":
            webbrowser.open(URL)
            return
        time.sleep(0.5)


def main() -> int:
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        print("uv was not found. Start optjournal with its Start file.")
        return 1
    on_port = _whats_on()
    if on_port == "optjournal":
        print(f"optjournal is already running. Opening {URL}")
        webbrowser.open(URL)
        return 0
    if on_port == "other":
        print(f"Port {PORT} is in use by another program, not optjournal, so "
              "optjournal cannot start. Close that program and start optjournal again.")
        return 1
    print("Starting optjournal. Keep this window open while you use it;")
    print("close it to stop optjournal.\n")
    env = {**os.environ, "OPTJOURNAL_SUPERVISED": "1"}
    # `--frozen` on every run: install exactly the `uv.lock` that shipped. A run
    # that re-locked would leave a modified tracked file in a git clone, which
    # `optjournal update` then refuses as uncommitted work.
    app = [uv, "run", "--frozen", "--no-dev", "optjournal"]
    first = True
    while True:
        version = apply_staged(ROOT)
        if version:
            print(f"Updated to version {version}.")
        subprocess.run([*app, "prepare"], cwd=ROOT, env=env, check=False)
        proc = subprocess.Popen([*app, "serve", "--port", str(PORT)], cwd=ROOT, env=env)
        try:
            if first:
                first = False
                _open_when_ready(proc)
            code = proc.wait()
        except KeyboardInterrupt:
            # Ctrl+C reaches the server too, which stops on its own: wait for it,
            # including during the first start's wait for the page.
            code = proc.wait()
        if code != RESTART:
            return code
        print("\nRestarting optjournal...\n")


if __name__ == "__main__":
    sys.exit(main())
