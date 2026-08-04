"""The page executed once, in a real browser engine, against a demo journal.

A third of the web tests are regex analysis of the page's JS source, and three
bug classes are structurally invisible to that: conditional rendering of
nullable fields, data binding that renders blank or literal "undefined", and a
template error that blanks a whole panel. All three have shipped past a green
suite. One executed render with DOM assertions closes the class, at the cost
of one browser launch.

The skip/fail contract is deliberate: a missing browser or a launch the
machine kills is an ENVIRONMENT problem and skips loudly, but a DOM that came
back and fails an assertion is the PAGE being wrong and fails the suite. A
skip here must never hide a rendering bug -- it may only hide the absence of
Chrome.

The known hazard -- headless Chrome hanging on this machine -- was probed,
not guessed: an x64 desktop Chrome running under Rosetta hangs in EVERY
headless variant (profile isolation does not help; desktop Chrome being open
was coincidence), while the arm64 chrome-headless-shell in the Playwright
cache renders in ~0.2s. So discovery prefers native Playwright binaries,
every launch gets a throwaway ``--user-data-dir`` and a short per-attempt
timeout, and ``--no-sandbox`` is required because Chromium's sandbox cannot
initialise in this environment ("Operation not permitted") -- acceptable
here, where the browser renders only our own page from our own loopback
server.
"""

from __future__ import annotations

import http.server
import json
import re
import shutil
import subprocess
import threading
import urllib.request
from functools import partial
from pathlib import Path

import pytest

from optjournal import web
from optjournal.db import connect, migrate
from optjournal.demo import write_demo_statement
from optjournal.ingest import DEFAULT_ASSET_FILTER, ingest_file


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    """A live optjournal server over a demo journal, on an ephemeral port.

    Wired exactly as ``serve()`` wires production -- same ServeConfig, same
    handler, same threading server -- so the render exercises the real
    request path, not a lookalike. Port 0 lets the OS pick, so the suite
    never collides with a journal already serving on 8765/8766.
    """
    root = tmp_path_factory.mktemp("render")
    statement = write_demo_statement(root / "demo", root / "demo.db")
    conn = connect(root / "demo.db")
    migrate(conn)
    ingest_file(conn, statement)
    conn.close()

    cfg = web.ServeConfig(
        db_path=root / "demo.db",
        archive_dir=statement.parent,
        query_id=None,
        assets=tuple(DEFAULT_ASSET_FILTER),
    )

    class _Srv(http.server.ThreadingHTTPServer):
        daemon_threads = True

    httpd = _Srv(("127.0.0.1", 0), partial(web._Handler, cfg))
    port = httpd.socket.getsockname()[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


def _browsers() -> list[str]:
    """Candidate binaries, best first. Empty list skips, never fails.

    Playwright's cached arm64 binaries lead because they are the ones proven
    to work on this machine; the system Chrome trails because a Rosetta
    build hangs in headless mode, and trailing it means a working native
    binary is always tried first. PATH names cover Linux/CI.
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


def _dump_dom(url: str, profile: Path) -> str | None:
    """The post-JS DOM from the first candidate that produces one, else None.

    ``--virtual-time-budget`` keeps the browser alive long enough for the
    page's /api/state fetch and render, then exits on its own -- no
    `timeout` binary needed (macOS has none; that already burned one
    session). The 12s subprocess timeout per attempt is the backstop that
    caps what a hanging Rosetta build can cost before the next candidate is
    tried. ``--headless`` first: the headless shell and every modern Chrome
    accept it; ``--headless=new`` second for the builds that spell it that
    way.
    """
    for i, chrome in enumerate(_browsers()):
        for headless in ("--headless", "--headless=new"):
            cmd = [
                chrome, headless, "--no-sandbox", "--disable-gpu",
                "--no-first-run", "--no-default-browser-check",
                f"--user-data-dir={profile}-{i}", "--virtual-time-budget=8000",
                "--dump-dom", url,
            ]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
            except (subprocess.TimeoutExpired, OSError):
                continue
            if proc.returncode == 0 and "<html" in proc.stdout.lower():
                return proc.stdout
    return None


def _text(dom: str) -> str:
    """Rendered text only: scripts, styles and tags stripped.

    The dump embeds the page's entire <script> source, which legitimately
    contains words like "undefined" -- scanning it would make the junk-text
    assertion fire on healthy code.
    """
    dom = re.sub(r"<(script|style)\b.*?</\1>", " ", dom, flags=re.S | re.I)
    return re.sub(r"<[^>]+>", " ", dom)


def test_the_dashboard_renders_from_the_payload(served, tmp_path):
    if not _browsers():
        pytest.skip("no Chrome/Chromium on this machine")
    dom = _dump_dom(served + "/", tmp_path / "profile")
    if dom is None:
        pytest.skip("no browser produced a DOM (environment, not the page)")

    with urllib.request.urlopen(served + "/api/state") as res:
        payload = json.load(res)
    text = _text(dom)

    # The tab bar is the first thing draw() emits; its absence means the
    # page JS threw before rendering anything at all.
    assert dom.count('data-tab="') >= 5, "tab bar missing -- page JS crashed on load"

    # Data binding: the month dropdown holds exactly the browsable range,
    # plus its one "All time" head. An off-by-anything here is the render
    # disagreeing with the payload it was handed.
    sel = re.search(r'<select id="month">(.*?)</select>', dom, re.S)
    assert sel, "the filter bar never rendered"
    assert sel.group(1).count("<option") == len(payload["month_range"]) + 1

    # Conditional rendering, oracle-driven from the same payload the page
    # fetched: quotes present means the currency toggle exists and offers
    # exactly the base plus each quote; no quotes means no toggle. This is
    # the class the source-analysis tests are structurally blind to.
    if payload["fx"]["quotes"]:
        assert '<div class="ccytog">' in dom
        for code in {payload["fx"]["base"], *(q["code"] for q in payload["fx"]["quotes"])}:
            assert code in text, f"currency toggle is missing {code}"
    else:
        assert '<div class="ccytog">' not in dom

    # Catastrophic binding failures do not throw in a template literal --
    # they render as literal junk text. None of these words belong on the
    # page as prose.
    assert not re.search(r"\b(undefined|NaN)\b", text), "a binding rendered as junk"
    assert "[object Object]" not in text

    # The stat-card grid actually populated.
    assert "Trades" in text
    assert "Commissions" in text
