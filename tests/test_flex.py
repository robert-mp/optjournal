"""Parser-contract tests over a tracked, redacted Flex statement."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import RAW_DIR
from py_ibkr import Trade

from optjournal import flex
from optjournal.flex import load
from optjournal.sections import MODELLED_SECTIONS, raw_sections, section_tags

# `requestID` is an artefact of the Flex request itself, not trade data, and
# py_ibkr deliberately omits it. Anything else appearing here is real drift.
KNOWN_UNMODELLED_TRADE_ATTRS = {"requestID"}

# Sections we know py_ibkr does not model and that we read via the shim.
KNOWN_UNMODELLED_SECTIONS = {
    "AccountInformation",
    "OpenPositions",
    "SecuritiesInfo",
    "CorporateActions",
    "Transfers",
    # Emitted, ingested, and load-bearing: the section is enabled on the Flex
    # query, `ingest._ingest_equity_summaries` persists it (259 rows in the real
    # journal), and it is the denominator behind `Gain % of Net Liq`. It appears
    # in THIS set for a different reason from the rest -- not "we do not read it"
    # but "py_ibkr does not model it", so it reaches us through the
    # `sections.raw_sections` shim rather than as a typed attribute. That is what
    # this set means: unmodelled by the parser, whatever we then do with it.
    "EquitySummaryInBase",
}


def statements() -> list[Path]:
    return sorted(RAW_DIR.glob("activity-*.xml"))


@pytest.fixture(params=statements(), ids=lambda p: p.name)
def statement(request) -> Path:
    return request.param


def test_raw_dir_is_populated():
    """A fresh checkout must include the deterministic parser corpus."""
    assert statements(), f"no tracked statement fixtures in {RAW_DIR}"


def test_parses_without_error(statement: Path):
    resp = load(statement)
    assert resp.FlexStatements, "parsed response contains no statements"


def test_statement_metadata_present(statement: Path):
    for stmt in load(statement).FlexStatements:
        assert stmt.accountId
        assert stmt.fromDate and stmt.toDate
        assert stmt.fromDate <= stmt.toDate


def test_trade_fields_we_depend_on_are_modelled():
    """Fields the grouping and P&L engine will require."""
    required = {
        "ibOrderID",       # deterministic leg grouping
        "ibExecID",        # idempotent upsert key
        "openCloseIndicator",
        "notes",           # assignment / exercise / expiry codes
        "assetCategory",
        "buySell",
        "quantity",
        "tradePrice",
        "putCall",
        "strike",
        "expiry",
        "multiplier",
        "underlyingSymbol",
        "conid",
        "fxRateToBase",    # EUR-base account: non-optional here
        "ibCommission",
        "fifoPnlRealized",
        "levelOfDetail",
    }
    missing = required - set(Trade.model_fields)
    assert not missing, f"py_ibkr Trade is missing required fields: {sorted(missing)}"


def test_execution_level_detail(statement: Path):
    """The query must stay at execution granularity, not aggregated."""
    for stmt in load(statement).FlexStatements:
        levels = {t.levelOfDetail for t in (stmt.Trades or [])}
        assert levels <= {"EXECUTION"}, (
            f"unexpected levelOfDetail {levels}; the Flex query template "
            f"may have been changed away from execution granularity"
        )


def test_field_drift(statement: Path):
    """Fail if IBKR sends Trade attributes py_ibkr would silently drop.

    py_ibkr's models use extra='ignore', so unknown attributes vanish with
    no error. This turns that from a silent hazard into a failing test.
    """
    modelled = {f.lower() for f in Trade.model_fields}
    aliases = {
        v.alias.lower() for v in Trade.model_fields.values() if v.alias
    }
    known = modelled | aliases | {a.lower() for a in KNOWN_UNMODELLED_TRADE_ATTRS}

    seen: set[str] = set()
    for el in ET.parse(str(statement)).getroot().iter("Trade"):
        seen |= set(el.attrib)

    dropped = sorted(a for a in seen if a.lower() not in known)
    assert not dropped, (
        f"IBKR sent Trade attributes py_ibkr will discard: {dropped}. "
        f"Add them to the model or to KNOWN_UNMODELLED_TRADE_ATTRS."
    )


def test_section_drift(statement: Path):
    """Fail if the statement gains a section we neither model nor shim."""
    known = MODELLED_SECTIONS | KNOWN_UNMODELLED_SECTIONS
    unexpected = [t for t in section_tags(statement) if t not in known]
    assert not unexpected, f"unhandled statement sections: {unexpected}"


def test_shim_exposes_unmodelled_sections(statement: Path):
    sections = raw_sections(statement)
    assert not (set(sections) & MODELLED_SECTIONS), (
        "shim must not duplicate sections py_ibkr already models"
    )
    for tag, rows in sections.items():
        for row in rows:
            assert row, f"{tag}: empty attribute dict"


# --- retry budget -------------------------------------------------------------
#
# The polling ceiling is not a free parameter: callers size their timeouts from
# it. A cron script had FETCH_TIMEOUT_S=240 against a real worst case of 2,100s,
# on a stale comment claiming 84s, so a routine slow statement generation became
# a raw traceback and a spent request with no cooldown recorded. These pin the
# arithmetic and, more importantly, fail if MAX_RETRIES grows past what a daily
# cron can wait for.


def test_poll_worst_case_matches_backoff_arithmetic():
    """Recomputed independently of the module's own expression."""
    per_stage = sum(
        min(flex.RETRY_INTERVAL * (2**i), flex.MAX_RETRY_INTERVAL)
        for i in range(flex.MAX_RETRIES)
    )
    assert 2 * per_stage == flex.POLL_WORST_CASE_S, (
        "worst case must cover both py_ibkr poll stages (SendRequest and "
        "GetStatement), each of which gets the full retry budget"
    )


def test_poll_worst_case_is_hand_computable():
    """MAX_RETRIES=4 -> [30, 60, 120, 120] = 330s/stage -> 660s."""
    assert flex.MAX_RETRIES == 4
    assert flex.POLL_WORST_CASE_S == 660


def test_retry_budget_stays_within_a_daily_cron_window():
    """The guard that makes the timeout fix durable.

    `~/.meshclaw/crons/optjournal_sync.py` sets a subprocess timeout above
    POLL_WORST_CASE_S, and its cron registration sets a timeout above that.
    Raising MAX_RETRIES silently invalidates both. Fail here instead, where the
    message can say so, rather than at 07:00 in a sandboxed subprocess.
    """
    assert flex.POLL_WORST_CASE_S <= 720, (
        f"POLL_WORST_CASE_S is {flex.POLL_WORST_CASE_S}s. Raise "
        f"FETCH_TIMEOUT_S in optjournal_sync.py above it, and the cron's own "
        f"timeout above that, or lower MAX_RETRIES."
    )


def test_backoff_is_capped_not_unbounded():
    waits = [
        min(flex.RETRY_INTERVAL * (2**i), flex.MAX_RETRY_INTERVAL)
        for i in range(flex.MAX_RETRIES)
    ]
    assert max(waits) == flex.MAX_RETRY_INTERVAL
    assert waits == sorted(waits), "backoff must be monotonically non-decreasing"


# --- request-budget guard -----------------------------------------------------
#
# Every fetch spends one request against an IBKR allowance that locks the token
# out when exhausted, and a statement is regenerated once a day -- so a second
# fetch inside the window cannot return new information and can only cost. The
# cooldown is the only thing standing between a retry loop and a lockout, and it
# had no test at all.
#
# What makes it fragile is ORDERING rather than arithmetic: the guard has to fire
# before anything with a side effect. Removing the check entirely, or letting a
# reordering put it after `read_token` or the download, leaves a function that
# still behaves correctly on every happy path.


def _record(archive_dir, query_id, when):
    """Write the fetch-state file the cooldown reads, as `_record_fetch` does."""
    flex._write_state(archive_dir, {
        str(query_id): {"last_fetch": when.isoformat(timespec="seconds"),
                        "sha256": "x" * 64, "archive": "activity-x.xml"},
    })


def test_a_recent_fetch_is_refused_before_anything_is_spent(tmp_path, monkeypatch):
    """The guard runs before the keyring, let alone before the network.

    Asserted by making both fail loudly: if `fetch` reaches either one, this test
    errors with that call's message instead of raising FetchCooldown, which names
    the ordering regression precisely. A test that only asserted FetchCooldown
    would still pass if the guard had drifted after the download.
    """
    def no(*_a, **_k):
        raise AssertionError("a request was spent before the cooldown was checked")

    monkeypatch.setattr(flex, "read_token", no)
    monkeypatch.setattr(flex, "FlexClient", no)

    _record(tmp_path, "1591754", datetime.now(UTC) - timedelta(seconds=60))
    with pytest.raises(flex.FetchCooldown) as caught:
        flex.fetch("1591754", archive_dir=tmp_path, cooldown_s=900)
    # The retry hint is what a caller backs off on, so it must be usable.
    assert 0 < caught.value.retry_after_s <= 900


def test_a_fetch_outside_the_window_is_allowed(tmp_path):
    """The guard must not be a permanent refusal -- 0 means "go now"."""
    _record(tmp_path, "1591754", datetime.now(UTC) - timedelta(seconds=1_000))
    assert flex.cooldown_remaining(tmp_path, "1591754", cooldown_s=900) == 0


def test_a_never_fetched_query_is_not_in_cooldown(tmp_path):
    """No state file at all is the first-run case, not an error."""
    assert flex.cooldown_remaining(tmp_path, "1591754") == 0
    assert flex.last_fetch(tmp_path, "1591754") is None


def test_force_bypasses_the_cooldown_deliberately(tmp_path, monkeypatch):
    """`force=True` is the documented override, so it must reach the token.

    Proven by asserting it gets PAST the guard: TokenMissing here means the
    cooldown let it through, which is the whole claim. Stopping at the keyring
    keeps this test off the network.
    """
    def no_token(*_a, **_k):
        raise flex.TokenMissing("reached the keyring")

    monkeypatch.setattr(flex, "read_token", no_token)
    _record(tmp_path, "1591754", datetime.now(UTC) - timedelta(seconds=60))
    with pytest.raises(flex.TokenMissing):
        flex.fetch("1591754", archive_dir=tmp_path, force=True, cooldown_s=900)


def test_the_cooldown_can_be_disabled_but_not_by_accident(tmp_path):
    """`cooldown_s=0` disables the guard; a positive default is what ships.

    The second half matters more than the first: a default of 0 would silently
    remove the protection for every caller that does not pass the argument.
    """
    _record(tmp_path, "1591754", datetime.now(UTC))
    assert flex.cooldown_remaining(tmp_path, "1591754", cooldown_s=0) == 0
    assert flex.FETCH_COOLDOWN_S > 0, "the shipped default must protect the budget"


def test_an_unparseable_timestamp_does_not_wedge_fetching(tmp_path):
    """A corrupt state file must fail open, not lock the archive out forever.

    The file is written by us, so a bad value means something went wrong locally
    -- and the cost of failing open is one request, while failing closed would
    make the tool permanently unusable with no way to tell why.
    """
    flex._write_state(tmp_path, {"1591754": {"last_fetch": "not-a-timestamp"}})
    assert flex.cooldown_remaining(tmp_path, "1591754") == 0


# --------------------------------------------------------------------------
# The socket timeout (SCHEDULER_PLAN.md step 7).
#
# `py_ibkr` calls `urlopen(req)` with NO timeout (py_ibkr/flex/client.py:109) and
# `socket.getdefaulttimeout()` is None, so a connection that opens and then stalls
# blocks forever. Verified both facts before writing the fix.
#
# Today the only thing that kills such a stall is the MeshClaw cron's 720s
# subprocess timeout, and the scheduler plan DELETES the cron. In the app the same
# stall would hold a job thread, its file lock and its `running` row indefinitely --
# and Python cannot interrupt a thread blocked in a syscall, so no amount of
# `timeout_s` on the job spec would help. It has to be on the socket.
# --------------------------------------------------------------------------


def test_a_stalled_response_times_out_instead_of_hanging_forever():
    """Against a REAL server that accepts and then says nothing.

    The whole point is behaviour under a stall, which no assertion about an
    attribute can show: `urlopen` with no timeout would sit in `recv` until the peer
    gave up. A one-second timeout against a server that never replies is the
    smallest honest reproduction.
    """
    import socket
    import threading
    import time

    from optjournal.flex import FlexError, _TimeoutFlexClient

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    held: list[socket.socket] = []

    def stall() -> None:
        # Accept, then never write a response. The client is left waiting on recv,
        # which is exactly the failure mode being bounded.
        conn, _ = listener.accept()
        held.append(conn)

    thread = threading.Thread(target=stall, daemon=True)
    thread.start()
    try:
        client = _TimeoutFlexClient(user_agent="test", timeout_s=1)
        started = time.monotonic()
        with pytest.raises(FlexError) as caught:
            client._get(f"http://127.0.0.1:{port}/stalls")
        elapsed = time.monotonic() - started
    finally:
        for conn in held:
            conn.close()
        listener.close()

    assert elapsed < 10, (
        f"the request took {elapsed:.1f}s against a 1s timeout, so the timeout is "
        "not reaching urlopen -- a stalled Flex fetch would hold a job thread, its "
        "file lock and its `running` row forever"
    )
    # And it arrives as the same exception every other transport failure does, so
    # `fetch`'s callers are unchanged.
    assert "timed out" in str(caught.value).lower(), (
        f"the timeout surfaced as {caught.value!r}, not a recognisable timeout"
    )


def test_the_timeout_is_not_installed_process_wide():
    """`socket.setdefaulttimeout()` would have been the one-line version, and it
    would have been wrong: it is PROCESS-GLOBAL, so it would also apply to the web
    server's own accept and read sockets. A scheduler must not configure the HTTP
    server by side effect.
    """
    import inspect
    import socket

    from optjournal import flex

    assert socket.getdefaulttimeout() is None, (
        "importing optjournal.flex set a process-wide socket timeout, which now "
        "applies to the web server's sockets too"
    )
    # CODE, NOT PROSE, and this took two attempts. Grepping the whole source
    # tripped on this test's own explanation of why the global is wrong; a
    # line-prefix filter then tripped on `_TimeoutFlexClient`'s docstring, which
    # says the same thing in the right place. `ast` is the only version that
    # actually distinguishes the two: it walks real Call nodes, so a mention in
    # any comment or docstring is invisible to it by construction.
    import ast

    calls = {
        ast.unparse(node.func)
        for node in ast.walk(ast.parse(inspect.getsource(flex)))
        if isinstance(node, ast.Call)
    }
    assert not any("setdefaulttimeout" in call for call in calls), (
        "flex CALLS the process-global default instead of passing a timeout to its "
        f"own requests: {sorted(c for c in calls if 'timeout' in c)}"
    )


def test_the_real_client_is_the_one_with_the_timeout():
    """The subclass has to be what `fetch` actually uses.

    Worth pinning because the failure is silent: `FlexClient` and
    `_TimeoutFlexClient` behave identically on a healthy link, so a revert to the
    parent would pass every other test in this file and only show up as a hung
    scheduler thread months later.
    """
    import inspect

    from optjournal import flex

    # Through the SEAM, not the class name. `fetch` constructs
    # `_client_factory`, which exists because naming the class at the call site
    # broke `tests/test_locks.py`'s network stub -- it kept replacing
    # `flex.FlexClient`, a name nothing called any more, and two subprocesses went
    # to the real IBKR endpoint. So the invariant is about what the factory IS.
    assert flex._client_factory is flex._TimeoutFlexClient, (
        f"the fetch path builds {flex._client_factory!r}, which is not the client "
        "that carries a socket timeout"
    )
    body = inspect.getsource(flex._fetch_locked)
    assert "_client_factory(" in body, (
        "the fetch path no longer goes through the one stubbable seam, so a test "
        "that stubs the network can silently miss and make real requests"
    )
    assert "= FlexClient(" not in body


def test_the_socket_timeout_sits_below_the_polling_ceiling():
    """Two different budgets, and confusing them is the mistake to avoid.

    `POLL_WORST_CASE_S` (660s) is the wall clock for the whole two-stage fetch
    INCLUDING the retry ladder that waits for IBKR to generate a statement. The
    socket timeout bounds ONE request inside that. A socket timeout above the
    ceiling could never fire; one at a few seconds would kill a legitimately slow
    download.
    """
    from optjournal.flex import FETCH_SOCKET_TIMEOUT_S, POLL_WORST_CASE_S

    assert 10 <= FETCH_SOCKET_TIMEOUT_S < POLL_WORST_CASE_S, (
        f"the per-request timeout ({FETCH_SOCKET_TIMEOUT_S}s) is not inside the "
        f"polling ceiling ({POLL_WORST_CASE_S}s)"
    )


# --------------------------------------------------------------------------
# write_token -- the one writer of the keyring entry.
#
# `keyring.set_password` is patched throughout. The suite must never touch the
# developer's real credential store: it is the one piece of state on the machine
# that no fixture can isolate, and a test that stored a fake token there would
# break the journal it was run from.
# --------------------------------------------------------------------------

def _captured_keyring(monkeypatch) -> list[tuple[str, str, str]]:
    wrote: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        flex.keyring, "set_password",
        lambda service, account, token: wrote.append((service, account, token)),
    )
    return wrote


def test_write_token_strips_what_a_paste_carries(monkeypatch):
    """A pasted token brings whitespace, and IBKR rejects it as invalid.

    Stripped HERE rather than in each caller, which is the reason this function
    exists at all: the terminal prompt and the settings page both hand it a value
    somebody pasted, and the failure it prevents -- "Token is invalid" for a token
    that is perfectly valid -- is indistinguishable from a wrong token to the
    person reading it.
    """
    wrote = _captured_keyring(monkeypatch)
    account = flex.write_token("  123456789012345\n", "tester")
    assert account == "tester"
    assert wrote == [(flex.KEYRING_SERVICE, "tester", "123456789012345")]


@pytest.mark.parametrize("bad", ["", "   ", "\n", "\t "])
def test_write_token_refuses_an_empty_token_rather_than_storing_one(
    monkeypatch, bad,
):
    """Storing "" would be worse than doing nothing: `read_token` treats a falsey
    entry as MISSING, so an empty write silently replaces a working token with a
    state whose error message tells the reader to create an entry that already
    exists."""
    wrote = _captured_keyring(monkeypatch)
    with pytest.raises(ValueError, match="empty"):
        flex.write_token(bad, "tester")
    assert wrote == []


def test_write_token_refuses_something_far_too_long_to_be_a_token(monkeypatch):
    """A length bound, not a format one, and the distinction is deliberate: IBKR
    does not document the token's shape, so a digits-only rule could refuse a
    token that works, while a length rule can only refuse one that IBKR has never
    issued. What it catches is a whole page pasted into the field."""
    wrote = _captured_keyring(monkeypatch)
    with pytest.raises(ValueError, match="not one"):
        flex.write_token("1" * (flex.TOKEN_MAX_LEN + 1), "tester")
    assert wrote == []


def test_write_token_and_read_token_agree_on_where_the_entry_lives(monkeypatch):
    """The round trip, through the service name and account rule BOTH use.

    This is the defect the shared writer exists to prevent: two modules spelling
    the keyring coordinates for themselves, one storing an entry the other cannot
    find -- which presents as a token that was definitely saved and is definitely
    missing.
    """
    store: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        flex.keyring, "set_password",
        lambda service, account, token: store.__setitem__((service, account), token),
    )
    monkeypatch.setattr(
        flex.keyring, "get_password",
        lambda service, account: store.get((service, account)),
    )
    flex.write_token("123456789012345", "tester")
    assert flex.read_token("tester") == "123456789012345"


def test_a_keychain_that_refuses_to_replace_an_entry_says_how_to_fix_it(monkeypatch):
    """The measured failure, and the one with the least guessable remedy.

    `keyring` writes on macOS by DELETING the existing item and adding a fresh
    one, and an item created by a different program refuses the delete with
    errSecInvalidOwnerEdit. So an entry that reads back perfectly cannot be
    replaced -- and the raw error says only `(-25244, 'Unknown Error')`, which
    names neither the cause nor the one command that fixes it.

    The status is read off the exception CHAIN, the way the macOS backend reports
    it, rather than scraped out of the rendered message.
    """
    cause = Exception(-25244, "Unknown Error")

    def refuse(service, account, token):
        raise RuntimeError("Can't store password on keychain") from cause

    monkeypatch.setattr(flex.keyring, "set_password", refuse)
    with pytest.raises(flex.TokenWriteRefused) as caught:
        flex.write_token("123456789012345", "tester")
    message = str(caught.value)
    assert "errSecInvalidOwnerEdit" in message, "the status is not named"
    assert "security delete-generic-password" in message, "no remedy is offered"
    assert flex.KEYRING_SERVICE in message
    assert "-a tester" in message, "the remedy does not name the account"


def test_an_unrecognised_keyring_failure_is_passed_through_not_guessed_at(
    monkeypatch,
):
    """Advice invented for a status this does not know would be worse than the
    original error. Only the two ownership refusals get a remedy; everything else
    keeps its own text, and still arrives as TokenWriteRefused so callers have one
    thing to catch."""
    def refuse(service, account, token):
        raise RuntimeError("the keychain is on fire")

    monkeypatch.setattr(flex.keyring, "set_password", refuse)
    with pytest.raises(flex.TokenWriteRefused, match="on fire") as caught:
        flex.write_token("123456789012345", "tester")
    assert "security delete-generic-password" not in str(caught.value), (
        "a remedy was offered for a status whose remedy is unknown"
    )


# --------------------------------------------------------------------------
# A token IBKR refuses, told apart from a request it refuses.
# --------------------------------------------------------------------------

def test_an_expired_token_is_named_as_such_with_the_remedy(monkeypatch):
    """1012 arrives as `FlexAuthError` with the code stripped OUT of the message.

    py_ibkr's template for 1009/1012 is "IBKR Authentication Error: {msg}", so the
    number is gone by the time it reaches us and only the class identifies it. This
    is the failure this journal actually hit: two weeks of `sync` reporting
    `FlexAuthError` and never that the fix is a new token.
    """
    from py_ibkr.flex.client import FlexAuthError

    def expired(*args, **kwargs):
        raise FlexAuthError("IBKR Authentication Error: Token has expired.")

    monkeypatch.setattr(flex, "read_token", lambda account=None: "tok")
    monkeypatch.setattr(flex, "_client_factory",
                        lambda **kw: type("C", (), {"download": expired})())
    with pytest.raises(flex.TokenRejected) as caught:
        flex.fetch("1591754", archive_dir=RAW_DIR, force=True)
    message = str(caught.value)
    assert "expired" in message
    assert "Client Portal" in message, "the remedy is not stated"
    assert "optjournal setup" in message


def test_a_regenerated_token_reads_as_invalid_and_still_gets_the_remedy(monkeypatch):
    """1015 falls through py_ibkr's table to a BARE `FlexError`.

    So a class check alone misses it -- and 1015 is what the same token answered
    after being regenerated in Client Portal, measured against the live endpoint.
    The code is recovered from the message, which is the only place py_ibkr puts it.
    """
    from py_ibkr import FlexError

    def invalid(*args, **kwargs):
        raise FlexError("Flex API Error 1015: Token is invalid.")

    monkeypatch.setattr(flex, "read_token", lambda account=None: "tok")
    monkeypatch.setattr(flex, "_client_factory",
                        lambda **kw: type("C", (), {"download": invalid})())
    with pytest.raises(flex.TokenRejected, match="regenerated"):
        flex.fetch("1591754", archive_dir=RAW_DIR, force=True)


def test_an_error_that_is_not_about_the_token_is_left_alone(monkeypatch):
    """Statement-not-ready, throttling and a bad query id are NOT credential
    problems, and dressing them as one would send the reader to Client Portal to
    replace a token that works. They keep their own class so the existing
    handlers -- and the retry policy -- still see them."""
    from py_ibkr import FlexError

    def other(*args, **kwargs):
        raise FlexError("Flex API Error 1003: Statement is not available.")

    monkeypatch.setattr(flex, "read_token", lambda account=None: "tok")
    monkeypatch.setattr(flex, "_client_factory",
                        lambda **kw: type("C", (), {"download": other})())
    with pytest.raises(FlexError) as caught:
        flex.fetch("1591754", archive_dir=RAW_DIR, force=True)
    assert not isinstance(caught.value, flex.TokenRejected)


def test_the_error_code_pattern_still_matches_what_py_ibkr_writes():
    """A pin on somebody else's message format, because that is what we parse.

    `FlexError` carries no code attribute -- checked in the installed source -- so
    the code is recovered from the rendered text. If py_ibkr rewords that string,
    this fails loudly here instead of quietly dropping the remedy for 1015 and
    leaving a reader with "Unknown Error" again.
    """
    import inspect

    import py_ibkr.flex.client as client

    source = inspect.getsource(client)
    assert 'f"Flex API Error {error_code}: {error_msg}"' in source, (
        "py_ibkr changed how it renders an unmapped error code; "
        "flex._FLEX_CODE no longer matches it"
    )
    assert flex._FLEX_CODE.search("Flex API Error 1015: Token is invalid.")
