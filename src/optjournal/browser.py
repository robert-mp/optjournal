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

import re
import shutil
import subprocess
from pathlib import Path

#: Long enough for the page's /api/state fetch and render, then the browser
#: exits on its own -- no `timeout` binary needed (macOS has none; that
#: already burned one session).
_VIRTUAL_TIME_BUDGET_MS = 8000

#: Backstop per attempt: caps what a hanging Rosetta build can cost before the
#: next candidate is tried.
_ATTEMPT_TIMEOUT_S = 12


def browsers() -> list[str]:
    """Candidate binaries, best first. Empty list means skip, never fail.

    Playwright's cached arm64 binaries lead because they are the ones proven to
    work on this machine; the system Chrome trails because a Rosetta build
    hangs in headless mode, and trailing it means a working native binary is
    always tried first. PATH names cover Linux/CI.
    """
    out: list[str] = []
    caches = Path.home() / "Library" / "Caches" / "ms-playwright"
    for pattern in (
        "chromium_headless_shell-*/chrome-headless-shell-mac-*/chrome-headless-shell",
        "chromium-*/chrome-mac-*/Chromium.app/Contents/MacOS/Chromium",
    ):
        hits = sorted(caches.glob(pattern))
        if hits:
            out.append(str(hits[-1]))
    for path in (
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ):
        if Path(path).is_file():
            out.append(path)
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            out.append(found)
    return out


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
                "--dump-dom", url,
            ]
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=_ATTEMPT_TIMEOUT_S
                )
            except (subprocess.TimeoutExpired, OSError):
                continue
            if proc.returncode == 0 and "<html" in proc.stdout.lower():
                return proc.stdout
    return None


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
