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
"""

from __future__ import annotations

import os
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


def _listening(timeout_s: float = 0.3) -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=timeout_s):
            return True
    except OSError:
        return False


def _open_when_ready(proc: subprocess.Popen[bytes], limit_s: float = 120) -> None:
    """Open the browser once the server answers. The first start builds the
    environment and can take a minute, so this waits rather than guessing."""
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline and proc.poll() is None:
        if _listening():
            webbrowser.open(URL)
            return
        time.sleep(0.5)


def main() -> int:
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        print("uv was not found. Start optjournal with its Start file.")
        return 1
    if _listening():
        print(f"optjournal is already running. Opening {URL}")
        webbrowser.open(URL)
        return 0
    print("Starting optjournal. Keep this window open while you use it;")
    print("close it to stop optjournal.\n")
    env = {**os.environ, "OPTJOURNAL_SUPERVISED": "1"}
    first = True
    while True:
        version = apply_staged()
        if version:
            print(f"Updated to version {version}.")
        subprocess.run([uv, "run", "--no-dev", "optjournal", "prepare"], cwd=ROOT, env=env,
                       check=False)
        proc = subprocess.Popen([uv, "run", "--no-dev", "optjournal", "serve",
                                 "--port", str(PORT)],
                                cwd=ROOT, env=env)
        if first:
            _open_when_ready(proc)
            first = False
        try:
            code = proc.wait()
        except KeyboardInterrupt:
            code = proc.wait()
        if code != RESTART:
            return code
        print("\nRestarting optjournal...\n")


if __name__ == "__main__":
    sys.exit(main())
