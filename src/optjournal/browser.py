"""Rendering the page in a real browser engine, and stripping the result to text.

This exists as a module rather than test-local helpers because two callers need
it -- ``tests/test_rendered.py`` (one page, asserted in the suite) and
``sweep.py`` (the whole page matrix, run on demand). The knowledge below was
bought with a wasted session and is wrong to hold in two places.

The known hazard on this machine: an x64 desktop Chrome running under Rosetta
hangs in EVERY headless variant (profile isolation does not help; desktop
Chrome being open was coincidence), while the arm64 chrome-headless-shell in
the Playwright cache renders in ~0.2s. So discovery prefers native Playwright
binaries, every launch gets a throwaway ``--user-data-dir``, and each attempt
carries a short timeout so a hanging build is bounded rather than fatal.

``--no-sandbox`` is required because Chromium's sandbox cannot initialise in
this environment ("Operation not permitted"). Acceptable here, where the
browser renders only our own page from our own loopback server.

A missing or hanging browser is an ENVIRONMENT condition and is reported as
such -- `dump_dom` returns None rather than raising, so callers can skip
loudly instead of failing a page that was never rendered.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

#: Long enough for the page's /api/state fetch and render, then the browser
#: exits on its own -- no `timeout` binary needed (macOS has none; that
#: already burned one session).
_VIRTUAL_TIME_BUDGET_MS = 8000

#: Backstop per attempt: caps what a hanging Rosetta build can cost before the
#: next candidate is tried.
_ATTEMPT_TIMEOUT_S = 12


def _windows_install_paths(environ: Mapping[str, str]) -> list[Path]:
    """Conventional Chromium locations supplied by a Windows environment."""
    roots = [
        (environ.get("PROGRAMFILES"), Path("Google/Chrome/Application/chrome.exe")),
        (environ.get("PROGRAMFILES(X86)"), Path("Google/Chrome/Application/chrome.exe")),
        (environ.get("LOCALAPPDATA"), Path("Google/Chrome/Application/chrome.exe")),
        (environ.get("PROGRAMFILES"), Path("Microsoft/Edge/Application/msedge.exe")),
        (environ.get("PROGRAMFILES(X86)"), Path("Microsoft/Edge/Application/msedge.exe")),
        (environ.get("LOCALAPPDATA"), Path("Chromium/Application/chrome.exe")),
    ]
    return [Path(root) / tail for root, tail in roots if root]


def browsers() -> list[str]:
    """Candidate binaries, best first. Empty list means skip, never fail.

    Playwright's cached arm64 binaries lead because they are the ones proven to
    work on this machine; the system Chrome trails because a Rosetta build
    hangs in headless mode, and trailing it means a working native binary is
    always tried first. PATH names cover Linux/CI.
    """
    out: list[str] = []
    if sys.platform == "win32":
        cache = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        caches = cache / "ms-playwright"
        patterns = (
            "chromium_headless_shell-*/chrome-headless-shell-win64/"
            "chrome-headless-shell.exe",
            "chromium-*/chrome-win64/chrome.exe",
        )
    elif sys.platform == "darwin":
        caches = Path.home() / "Library" / "Caches" / "ms-playwright"
        patterns = (
            "chromium_headless_shell-*/chrome-headless-shell-mac-*/chrome-headless-shell",
            "chromium-*/chrome-mac-*/Chromium.app/Contents/MacOS/Chromium",
        )
    else:
        caches = Path.home() / ".cache" / "ms-playwright"
        patterns = (
            "chromium_headless_shell-*/chrome-headless-shell-linux/"
            "chrome-headless-shell",
            "chromium-*/chrome-linux/chrome",
        )
    for pattern in patterns:
        hits = sorted(caches.glob(pattern))
        if hits:
            out.append(str(hits[-1]))
    installed = (
        _windows_install_paths(os.environ)
        if sys.platform == "win32"
        else [
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ] if sys.platform == "darwin" else []
    )
    out.extend(str(path) for path in installed if path.is_file())
    for name in (
        "chromium", "chromium-browser", "google-chrome", "chrome",
        "chrome.exe", "msedge.exe",
    ):
        found = shutil.which(name)
        if found:
            out.append(found)
    return list(dict.fromkeys(out))


def dump_dom(url: str, profile: Path) -> str | None:
    """The post-JS DOM from the first candidate that produces one, else None.

    ``--headless`` is tried first (the headless shell and every modern Chrome
    accept it), then ``--headless=new`` for the builds that spell it that way.
    """
    for i, chrome in enumerate(browsers()):
        for headless in ("--headless", "--headless=new"):
            cmd = [
                chrome, headless, "--no-sandbox", "--disable-gpu",
                "--no-first-run", "--no-default-browser-check",
                f"--user-data-dir={profile}-{i}",
                f"--virtual-time-budget={_VIRTUAL_TIME_BUDGET_MS}",
                # Routes page console output to stderr, so an uncaught error can
                # be asserted on. Worth having: a ReferenceError blanks the whole
                # view, and 470 passing Python tests did not notice one -- the
                # payload contract and the unit suite both read source and data,
                # neither of which knows the browser refused to run it.
                "--enable-logging=stderr", "--v=0",
                "--dump-dom", url,
            ]
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=_ATTEMPT_TIMEOUT_S,
                )
            except (subprocess.TimeoutExpired, OSError):
                continue
            if proc.returncode == 0 and "<html" in proc.stdout.lower():
                _LAST_CONSOLE.clear()
                _LAST_CONSOLE.extend(console_errors(proc.stderr))
                return proc.stdout
    return None


#: Console errors from the most recent successful dump_dom. Module state rather
#: than a return value so every existing caller keeps working unchanged; the
#: sweep reads it immediately after its own dump, which is single-threaded.
_LAST_CONSOLE: list[str] = []


def last_console_errors() -> list[str]:
    """Console errors from the most recent dump_dom, newest call only."""
    return list(_LAST_CONSOLE)


#: Substrings that identify a JavaScript failure in a console line. Plain
#: containment rather than a regex: this list is the entire specification, and a
#: reader should not have to parse word boundaries to see what counts.
_JS_ERROR_TOKENS = (
    "Uncaught",
    "Error:",
    "is not a function",
    "is not defined",
)


def console_errors(stderr: str) -> list[str]:
    """Uncaught page errors from chrome's stderr, ignoring its own chatter.

    Only CONSOLE lines are considered, and only those naming a JS error type: a
    404 for a favicon and a deprecation notice are not defects in this page,
    whereas an uncaught ReferenceError means the view did not render at all.
    """
    found: list[str] = []
    for line in stderr.splitlines():
        if ":CONSOLE(" not in line and ":CONSOLE:" not in line:
            continue
        if any(token in line for token in _JS_ERROR_TOKENS):
            found.append(line.split("] ", 1)[-1].strip())
    return found


def _strip_code(dom: str) -> str:
    """The document with <script> and <style> bodies removed, tags intact."""
    return re.sub(r"<(script|style)\b.*?</\1>", " ", dom, flags=re.S | re.I)


def markup(dom: str) -> str:
    """The rendered markup: script and style bodies gone, tags kept.

    The view structural checks need, and the reason it exists as a third view
    rather than callers using the raw dump: ``--dump-dom`` embeds the page's
    ENTIRE <script> source, so a pattern like ``<td class="side\\s*([^"]*)"``
    matches the template literal that generates the cell as readily as the cell
    itself. Every structural check in the first sweep run matched page.html's
    own source instead of its output -- reporting the Annual tab's year-row
    template as a Positions subtotal, and ``${String(pos.side)...}`` as a CSS
    class.

    `rendered_text` cannot serve here because it also strips tags, which is
    exactly what a structural check reads.
    """
    return _strip_code(dom)


def rendered_text(dom: str) -> str:
    """Rendered text only: scripts, styles and tags stripped.

    The dump embeds the page's entire <script> source, which legitimately
    contains words like "undefined" -- scanning it would make the junk-text
    assertion fire on healthy code.
    """
    return re.sub(r"<[^>]+>", " ", _strip_code(dom))
