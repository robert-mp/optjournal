"""Shared fixtures and builders for the suite.

What belongs here: the setup that says nothing about the thing under test.
Five test modules each defined `RAW_DIR` and `STATEMENTS` with the same two
lines, four defined a `conn` fixture that opened and migrated a database, and
seven wrote out the same eight-column `INSERT INTO statements` with different
values -- so a schema change to that table meant seven edits, and the copies
had already drifted (`asset_filter` was 'OPT' in five and 'ALL' in one, for no
reason either stated).

What deliberately does NOT belong here: anything a test is asserting about.
The per-module builders that insert *trades* stay in their own files, because
what a trade row contains is the subject of those tests rather than scaffolding
for them -- `test_history` needs fills with notes and dispositions,
`test_archive` needs provenance columns, and folding those into one builder
would produce a function with a dozen parameters that no reader could follow
back to the behaviour it exercises.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
from pathlib import Path

import pytest

from optjournal import settings
from optjournal.config import data_home
from optjournal.db import connect, migrate
from optjournal.ingest import ASSET_FILTER_ALL, ingest_file
from optjournal.mutate import CLONE_ENV

#: The deterministic, redacted corpus used by the normal suite and CI.
RAW_DIR = Path(__file__).resolve().parent / "fixtures" / "statements"

#: Sorted so a test that takes "the newest" gets the same file on every machine.
STATEMENTS = sorted(RAW_DIR.glob("activity-*.xml"))

#: The project root, for tests reaching source files rather than data.
ROOT = Path(__file__).resolve().parent.parent

#: Optional acceptance corpus from the developer's account. Tests that make
#: claims about specific live rows opt into this explicitly; the normal suite
#: never changes when another statement is fetched.
LIVE_RAW_DIR = data_home() / "raw"
LIVE_STATEMENTS = sorted(LIVE_RAW_DIR.glob("activity-*.xml"))


def _is_copy() -> bool:
    """Whether this tree is a COPY of the checkout rather than the checkout itself.

    Two copies exist in practice, and each announces itself differently.

    A git WORKTREE is recognisable from what git left behind: `.git` is a FILE
    holding a `gitdir:` pointer rather than a directory. Read from git rather than
    by matching path names, so it holds wherever the worktree is put.

    A `copytree` CLONE, which `optjournal mutate` builds, is not recognisable at
    all -- it is the whole checkout copied, `.git` directory and all, so it looks
    exactly like the original and the git check above says "original". That is why
    it is TOLD, via `mutate.CLONE_ENV`. Inferring it was the earlier attempt, and
    it silently did nothing: the plist test went on running in every clone,
    failing, and leaving a baseline the harness refuses to measure against.
    """
    if os.environ.get(CLONE_ENV):
        return True
    dot_git = ROOT / ".git"
    return dot_git.is_file() or not dot_git.exists()


#: Skip marker for assertions that pin the ORIGINAL checkout's absolute paths.
#:
#: `test_launchd` asserts the plist execs THIS checkout's console script.
#: launchd stores absolute paths, so a moved repo is exactly the failure it
#: guards; the assertion cannot hold in a worktree or mutation clone.
#:
#: They were the reason `optjournal mutate` reported `dirty-baseline` and measured
#: NOTHING -- it runs the suite in a `copytree` clone, so the baseline could never
#: be green, and every mutant came back uncounted while the tool still printed a
#: reassuring summary line. A harness that looks like it is working is worse than
#: one that is visibly broken, which is why this is a skip rather than a note in
#: the README.
skip_if_copy = pytest.mark.skipif(
    _is_copy(),
    reason="pins the original checkout's absolute paths; this tree is a copy "
           "(git worktree or mutation clone), where they cannot hold",
)


@pytest.fixture(autouse=True, scope="session")
def _isolated_settings_home(tmp_path_factory):
    """Point `settings` at a scratch directory for the whole session.

    AUTOUSE, because the failure it prevents is silent and does not belong to any
    one test: `settings.path_for` defaults to the repo root, so every test that
    exercises the Flex query id's precedence would otherwise read the developer's
    own `.optjournal.json`. That made
    `test_no_query_id_anywhere_stays_none_rather_than_empty` pass or fail
    depending on whether whoever ran the suite had run `optjournal setup` --
    a test whose result depends on the machine is a test that has stopped
    describing the code.

    Session-scoped so a test CAN write settings and see them, which the ones
    about persistence need; anything wanting a pristine directory passes its own
    `root` (see tests/test_settings.py, which uses `tmp_path` throughout).
    """
    home = tmp_path_factory.mktemp("settings-home")
    previous = os.environ.get(settings.HOME_ENV)
    os.environ[settings.HOME_ENV] = str(home)
    yield home
    if previous is None:
        os.environ.pop(settings.HOME_ENV, None)
    else:
        os.environ[settings.HOME_ENV] = previous


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    """An empty migrated journal. No rows, so a test states its own data."""
    return connect_migrated(tmp_path / "journal.db")


@pytest.fixture(scope="session")
def _populated_master(tmp_path_factory) -> Path:
    """The ingest, done ONCE per session. Never handed to a test.

    Private, and `populated_db` copies it, because the alternative -- handing this
    file to every test -- would make the suite order-dependent: the watchlist
    endpoint tests write rows, and a shared file would carry them into whatever
    ran next. A copy per test keeps the isolation the function scope gave for
    free while paying for the ingest once.
    """
    if not STATEMENTS:
        pytest.skip("needs a tracked statement fixture")
    db = tmp_path_factory.mktemp("master") / "journal.db"
    c = connect_migrated(db)
    for path in STATEMENTS:
        ingest_file(c, path, assets=ASSET_FILTER_ALL)
    c.close()
    return db


@pytest.fixture
def populated_db(tmp_path, _populated_master) -> Path:
    """A database with every tracked statement fixture ingested.

    Returns the PATH, not a connection: the web layer opens its own connection
    per request (sqlite3 handles cannot cross threads), so a test that handed it
    a live handle would be testing something the server never does.

    A COPY of a session-scoped master, so each test still gets a private file it
    may write to. Ingesting all statements per test rebuilt an identical database
    66 times, which measured 33s of an 85s suite -- and the suite runs twice per
    mutant, so it was the single biggest cost in the mutation survey.
    """
    db = tmp_path / "journal.db"
    shutil.copy(_populated_master, db)
    return db


def connect_migrated(path: Path) -> sqlite3.Connection:
    """Open a journal at `path` and bring the schema up to date."""
    c = connect(path)
    migrate(c)
    return c


_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
#: The `(?<!:)` keeps `://` in a URL from being mistaken for a comment start.
#: A protocol-relative `"//host"` would still be stripped, which is acceptable
#: here: `test_page_loads_no_external_resources` asserts the page has none.
_LINE_COMMENT = re.compile(r"(?<!:)//[^\n]*")


def code_only(source: str) -> str:
    """JavaScript with its comments removed, so prose is not scanned as code.

    Both JS guards need this and each had grown its own version. They had
    drifted into different behaviour: this one strips a comment wherever it
    starts, while `test_frontend`'s only matched comments occupying a WHOLE line
    (`^\\s*//.*$`), so a trailing `const x = 1; // reads document.title` left the
    word `document` in the "code" and would have failed that module's
    browser-API check on its own documentation. Verified, not assumed.

    Regex rather than a real tokenizer, which is sound for these two files: they
    use block comments almost exclusively, the markers are balanced (asserted by
    `test_page_comments_are_balanced`), and neither puts a comment marker inside
    a string literal. The helper is tested directly rather than trusted.
    """
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", source))


def add_statement(
    conn: sqlite3.Connection,
    *,
    source_file: str = "t.xml",
    sha256: str = "x",
    account_id: str = "U1",
    from_date: str = "2025-01-01",
    to_date: str = "2026-12-31",
    base_currency: str = "EUR",
    asset_filter: str = "OPT",
) -> None:
    """Insert the statement row that trade and snapshot rows hang off.

    Every column is a keyword with a default, so a test names only what it is
    actually about -- a date range when it is testing period logic, an
    `asset_filter` when it is testing scoping -- and stays silent about the six
    it does not care about. Written once because the column list is the
    schema's, not any one test's: adding a column here used to mean editing
    seven literals.
    """
    conn.execute(
        "INSERT INTO statements (source_file, sha256, account_id, from_date,"
        " to_date, base_currency, asset_filter, ingested_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'now')",
        (source_file, sha256, account_id, from_date, to_date,
         base_currency, asset_filter),
    )


@pytest.fixture(autouse=True)
def _no_fetch_on_watch(monkeypatch):
    """Adding a watched symbol fetches its history (`bars.fetch_watch_bars`), which
    is a network request. AUTOUSE so no test that merely adds a symbol reaches
    Yahoo; a test about the fetch itself patches its own fake over this one."""
    from optjournal import bars  # noqa: PLC0415 - local to the fixture
    monkeypatch.setattr(bars, "fetch_bars", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def _no_earnings_fetch(monkeypatch):
    """Adding a symbol and refreshing quotes both ask Nasdaq for earnings dates.
    AUTOUSE so the suite never reaches it; a test about it patches its own."""
    from optjournal import earnings  # noqa: PLC0415 - local to the fixture
    monkeypatch.setattr(earnings, "fetch_earnings", lambda *a, **k: None)


@pytest.fixture
def broken_http():
    """A local HTTP server that breaks off mid-reply, for the transport tests.

    `url("truncated")` answers 200 with a `Content-Length` of 100 and sends ten
    bytes before closing, which `http.client` reports as `IncompleteRead`.
    `url("hangup")` accepts the connection and closes it without a word, which it
    reports as `RemoteDisconnected`. Both are what a flaky link or an overloaded
    source actually does, and neither is a `URLError`, which is why each fetcher
    has to name them.

    A real socket rather than a patched `urlopen`, so the exceptions are the
    ones the standard library really raises.
    """
    import socket  # noqa: PLC0415 - local to the fixture
    import threading  # noqa: PLC0415

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]

    def serve() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                request = conn.recv(65536).decode("latin-1")
                if "/truncated" in request.split("\r\n", 1)[0]:
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json"
                                 b"\r\nContent-Length: 100\r\n\r\n{\"chart\": ")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield lambda mode: f"http://127.0.0.1:{port}/{mode}"
    listener.close()
