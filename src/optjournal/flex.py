"""IBKR Flex Web Service access for optjournal.

Transport and XML parsing are delegated to `py_ibkr`, which already models
the Flex protocol (async statement generation, reference-code redemption)
and IBKR's failure modes -- including request lockout, which is easy to
trigger by retrying too eagerly.

This module adds the three things py_ibkr deliberately leaves to callers:

1. Credential handling. The Flex token grants read access to the entire
   account, so it lives in the OS keyring, never in argv, env, or a file.
2. Raw archiving. Every response is written to disk before parsing. IBKR
   locks out clients that request too often, so re-parsing must never
   require re-requesting.
3. Offline reload, so parser work can iterate against archived XML.
4. Request-budget protection. A cooldown per query ID, because the thing
   py_ibkr cannot know is whether *you* already asked a minute ago. Observed
   in practice: three identical statements fetched inside 60 seconds by a
   second process, all byte-identical, three requests spent for nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import keyring
from py_ibkr import FlexClient, FlexError, FlexQueryResponse
from py_ibkr.flex.client import FlexAuthError
from py_ibkr.flex.parser import parse_xml_file

from optjournal.locks import locked

__all__ = [
    "FETCH_COOLDOWN_S",
    "POLL_WORST_CASE_S",
    "FetchCooldown",
    "ConfirmFetch",
    "FetchResult",
    "TokenMissing",
    "TokenRejected",
    "TokenWriteRefused",
    "archive_digest",
    "cooldown_remaining",
    "fetch",
    "fetch_confirms",
    "last_fetch",
    "load",
    "read_token",
    "write_token",
]

log = logging.getLogger(__name__)

KEYRING_SERVICE = "ibkr-flex-token"
USER_AGENT = "optjournal/0.1"

#: Minimum gap between two fetches of the same query ID, in seconds.
#:
#: An Activity Statement is regenerated once a day, so a second fetch inside
#: this window cannot return new information -- it can only spend a request
#: against IBKR's lockout allowance. Fifteen minutes is short enough never to
#: obstruct a legitimate daily cron or a deliberate manual sync, and long
#: enough to absorb a retry loop, a duplicate cron trigger, or a second agent
#: working on the same project.
#:
#: This is a local guard, not an IBKR rule. `force=True` bypasses it.
FETCH_COOLDOWN_S = 900

#: Sidecar recording the last fetch per query ID. Lives beside the archive
#: rather than in the database, so `fetch` stays usable with no DB present
#: and the guard survives a database rebuild.
STATE_FILE = ".fetch-state.json"

#: Archive filename prefixes, one per Flex query type. `activity-*.xml` is what
#: every archive walker globs, so a confirm MUST NOT land under it -- see
#: `_archive`.
ACTIVITY_PREFIX = "activity"
CONFIRM_PREFIX = "confirm"

#: Sibling lock file for the whole check-download-record sequence. Beside the
#: state file it guards, in the archive directory, so one journal's fetches do
#: not serialise against another's.
FETCH_LOCK = ".fetch.lock"


class FetchCooldown(RuntimeError):
    """A fetch was refused locally because the same query ran too recently.

    Deliberately not a subclass of py_ibkr's throttling errors: nothing was
    sent, so no request was spent. Callers should treat it the way they treat
    IBKR throttling -- back off quietly, retry later -- but the distinction
    matters when diagnosing, because one means IBKR pushed back and the other
    means we never asked.
    """

    def __init__(self, query_id: str, last_fetch: datetime, retry_after_s: int):
        self.query_id = query_id
        self.last_fetch = last_fetch
        self.retry_after_s = retry_after_s
        super().__init__(
            f"query {query_id} was fetched at "
            f"{last_fetch.isoformat(timespec='seconds')}; "
            f"retry in {retry_after_s}s or pass force=True"
        )

#: Retry budget for Flex statement generation.
#:
#: py_ibkr backs off as ``min(RETRY_INTERVAL * 2**i, MAX_RETRY_INTERVAL)`` and
#: applies the budget to *each* of its two stages independently (SendRequest,
#: which retries while another statement is generating, and GetStatement,
#: which retries while the statement is not ready). So the worst case is
#: twice the per-stage sum:
#:
#:     MAX_RETRIES=4  ->  [30, 60, 120, 120] = 330s/stage  ->  660s (11 min)
#:
#: Four is chosen so that ceiling fits inside a daily cron's timeout. It was
#: 10, which is 1,050s per stage and 35 minutes end to end -- far longer than
#: any caller was willing to wait, and long enough that the cron's own
#: subprocess timeout fired first and turned a routine slow generation into a
#: raw traceback. Patience beyond a few minutes buys nothing here: an
#: Activity statement is regenerated once a day, so a statement that is not
#: ready in five minutes will still be there at the next scheduled run.
#:
#: Keep any caller-side timeout above 660s, and the cron timeout above that,
#: so the caller's own handler runs before anything kills the process.
MAX_RETRIES = 4
RETRY_INTERVAL = 30
MAX_RETRY_INTERVAL = 120

#: Worst-case wall time of `fetch`'s polling, derived from the constants
#: above. Exported so callers can size their timeouts from the real number
#: rather than guessing -- guessing is what produced the 240s-vs-2100s
#: mismatch this replaces.
POLL_WORST_CASE_S = 2 * sum(
    min(RETRY_INTERVAL * (2**i), MAX_RETRY_INTERVAL) for i in range(MAX_RETRIES)
)


#: Per-HTTP-REQUEST socket timeout for the Flex calls. Not a budget for the whole
#: fetch: `download` polls, so the wall-clock ceiling is `POLL_WORST_CASE_S` and
#: this bounds each individual request inside it.
#:
#: IT EXISTS BECAUSE NOTHING ELSE BOUNDS A HUNG SOCKET. `py_ibkr` calls
#: `urlopen(req)` with no timeout (py_ibkr/flex/client.py:109) and
#: `socket.getdefaulttimeout()` is None -- both verified -- so a connection that
#: opens and then stalls blocks forever. Today the only killer is the MeshClaw
#: cron's 720s subprocess timeout, and SCHEDULER_PLAN.md deletes the cron. In the
#: app the same stall would hold a job thread, its file lock and its `running` row
#: indefinitely, and Python cannot interrupt a thread blocked in a syscall -- so no
#: `timeout_s` on a job spec could help. It has to be on the socket.
#:
#: 60s per request: an Activity statement download is seconds on a working link,
#: and `download`'s own retry ladder handles a slow GENERATION. A request that has
#: produced nothing in a minute is a stall, not slowness.
FETCH_SOCKET_TIMEOUT_S = 60


class _TimeoutFlexClient(FlexClient):
    """`FlexClient` with a socket timeout on every request.

    A SUBCLASS RATHER THAN `socket.setdefaulttimeout()`, and the distinction
    matters: the default is PROCESS-GLOBAL, so it would also apply to the web
    server's own accept and read sockets -- a scheduler configuring the HTTP server
    by side effect. `_get` is the single choke point both Flex calls go through
    (`send_request` and `get_statement` each call it), so overriding it covers the
    whole protocol with one method.

    Reimplements the body rather than calling `super()`, because the timeout has to
    reach `urlopen` itself; the parent passes no `timeout` argument at all.
    """

    def __init__(self, *args: object, timeout_s: int = FETCH_SOCKET_TIMEOUT_S,
                 **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.timeout_s = timeout_s

    def _get(self, url: str) -> bytes:
        request = Request(url, headers={"User-Agent": self.user_agent})  # noqa: S310
        try:
            with urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310
                return bytes(response.read())
        except HTTPError as exc:
            raise FlexError(f"HTTP Error {exc.code}: {exc.reason}") from exc
        except URLError as exc:
            raise FlexError(f"URL Error: {exc.reason}") from exc
        except TimeoutError as exc:
            # MEASURED, and my first version got it wrong: a socket timeout raises a
            # BARE `TimeoutError`, which is an `OSError` and NOT a `URLError`
            # (verified -- `isinstance(exc, URLError)` is False). Without this clause
            # the timeout escaped unhandled instead of arriving as the `FlexError`
            # every other transport failure does, so `sync_journal`'s callers would
            # have seen a raw traceback -- exactly the shape of the 2026-08-07
            # keychain failure this plan exists to stop.
            raise FlexError(f"timed out after {self.timeout_s}s: {exc}") from exc


#: What `fetch` constructs. A module-level indirection so there is exactly ONE name
#: to replace when a test needs the network stubbed -- see `_fetch_locked`.
_client_factory = _TimeoutFlexClient


class TokenMissing(RuntimeError):
    """No usable Flex token in the OS keyring."""


class TokenRejected(RuntimeError):
    """IBKR refused the TOKEN, rather than the request made with it.

    Separated from every other `FlexError` because the remedy is different in
    kind: nothing about the journal, the query or the network will fix it, and no
    retry will either -- somebody has to generate a new token. The scheduler
    treats it as a credentials failure, the same as a missing one, so it stops
    spending requests on a token IBKR has already rejected.
    """


class TokenWriteRefused(RuntimeError):
    """The OS credential store would not store the token.

    Its own type because the remedy is not the caller's to guess and the
    underlying error does not state it: `keyring` reports a numeric OS status
    with the text "Unknown Error", which tells the reader nothing about what to
    do next. `write_token` translates the statuses that HAVE a remedy.
    """


#: macOS keychain statuses that mean "an entry is here and this program may not
#: replace it", as opposed to "the store is broken". Both are refusals to change
#: an item's OWNERSHIP rather than to write at all -- adding a brand new entry
#: from the same process succeeds, which is what makes the distinction worth
#: drawing. Named from Security/SecBase.h, because a bare -25244 in a log is a
#: number nobody can look up from memory.
_KEYCHAIN_NOT_OURS = {
    -25244: "errSecInvalidOwnerEdit",
    -25243: "errSecNoAccessForItem",
}


@dataclass(frozen=True, slots=True)
class FetchResult:
    """A parsed statement plus the path of the raw XML it came from."""

    response: FlexQueryResponse
    raw_path: Path
    raw_bytes: int
    #: Set when the download was byte-identical to a file already archived,
    #: in which case `raw_path` points at that pre-existing file and nothing
    #: new was written.
    duplicate_of: Path | None = None

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_of is not None


def read_token(account: str | None = None) -> str:
    """Return the Flex token from the OS keyring.

    `account` defaults to the current user, matching how the entry is
    created:  security add-generic-password -a "$USER" -s ibkr-flex-token -w
    """
    if account is None:
        import getpass

        account = getpass.getuser()

    token = keyring.get_password(KEYRING_SERVICE, account)
    if not token:
        raise TokenMissing(
            f"No keyring entry {KEYRING_SERVICE!r} for account {account!r}. "
            f'Create it with:\n  security add-generic-password -a "$USER" '
            f"-s {KEYRING_SERVICE} -w"
        )
    return token.strip()


#: Longest token this will store. IBKR's tokens are short numeric strings, but
#: the FORMAT is not documented, so this bounds the write without asserting a
#: shape: a length rule cannot reject a valid token IBKR decides to lengthen,
#: where an `isdigit` rule could. A paste that is not a token fails at IBKR with
#: a message that says so, which is a better teacher than a guess here.
TOKEN_MAX_LEN = 128


def write_token(token: str, account: str | None = None) -> str:
    """Store the Flex token in the OS keyring. Returns the account it is under.

    One place writes the credential, for the same reason one place reads it: the
    service name and the account rule have to agree between `optjournal setup`
    and the settings page, and they were two `keyring` calls in two modules
    before this. A second spelling of either is an entry nothing can find.

    Whitespace-stripped because the value arrives PASTED -- from Client Portal,
    through a terminal prompt or a browser field, all three of which pick up a
    trailing newline or a leading space that IBKR then rejects as an invalid
    token. Nothing here echoes, logs or returns the value.
    """
    token = token.strip()
    if not token:
        raise ValueError("refusing to store an empty Flex token")
    if len(token) > TOKEN_MAX_LEN:
        raise ValueError(
            f"refusing to store a {len(token)}-character Flex token: "
            f"IBKR's are under {TOKEN_MAX_LEN}, so this is not one"
        )
    if account is None:
        import getpass

        account = getpass.getuser()
    try:
        keyring.set_password(KEYRING_SERVICE, account, token)
    except Exception as exc:
        raise TokenWriteRefused(_write_refusal(exc, account)) from exc
    return account


#: IBKR error codes that mean "this token is no good", with what each one is
#: actually telling you. Measured against the live endpoint on 2026-09-24: a token
#: past its lifetime answered 1012, and the same token after being regenerated in
#: Client Portal answered 1015 -- so the pair is how you tell "it aged out" from
#: "it was replaced", which is worth keeping distinct in the message.
_TOKEN_CODES = {
    "1012": "expired",
    "1015": "invalid, which is also what a token reads as once it has been "
            "regenerated in Client Portal",
    "1009": "not accepted",
}

#: How py_ibkr renders an IBKR error code it has no specific class for:
#: `f"Flex API Error {code}: {msg}"`. Parsed rather than read off an attribute
#: because `FlexError` carries no code -- checked in the installed source, and
#: pinned by a test, so an upstream wording change fails loudly here instead of
#: quietly losing the remedy.
_FLEX_CODE = re.compile(r"Flex API Error (\d+)")


def _reraise_if_token_rejected(exc: FlexError) -> None:
    """Raise `TokenRejected` if IBKR's complaint is about the token. Else return.

    TWO DETECTIONS, because py_ibkr reports the same class of problem two ways:
    1009 and 1012 arrive as `FlexAuthError` with the code stripped out of the
    message, while 1015 falls through to a bare `FlexError` whose text still
    carries "Flex API Error 1015". Matching on the class alone missed 1015 -- the
    code this journal actually hit -- and matching on the text alone would miss
    1012.
    """
    code = None
    found = _FLEX_CODE.search(str(exc))
    if found:
        code = found.group(1)
    if code not in _TOKEN_CODES and not isinstance(exc, FlexAuthError):
        return
    reads_as = _TOKEN_CODES.get(code or "", "not accepted")
    raise TokenRejected(
        f"IBKR says your Flex token is {reads_as}. Nothing here can retry past "
        f"that: generate a new one in Client Portal (Settings → Flex Web "
        f"Service), then store it in the page under Settings → Flex token, or "
        f"run `optjournal setup`.\n  IBKR said: {exc}"
    ) from exc


def _write_refusal(exc: Exception, account: str) -> str:
    """Why the credential store said no, and what to do about it.

    THE MESSAGE IS THE FEATURE. Measured on this machine: replacing a token that
    `security add-generic-password` had created in August failed with
    `Can't store password on keychain: (-25244, 'Unknown Error')`, three times,
    with no indication that the remedy is one command. `keyring` writes on macOS
    by DELETING the existing item and adding a fresh one, and the delete is what
    an item created by another program refuses -- so an entry that reads back
    perfectly cannot be replaced, which is the least guessable failure here.

    The status is read off the CAUSE rather than parsed out of the message text:
    `keyring.backends.macOS` raises `PasswordSetError(...) from api.Error(status,
    ...)`, so the number is a real attribute one link down the chain, and matching
    on the rendered string would break on a wording change.

    Anything else is passed through with its own text. A guess dressed as advice
    is worse than the original error, and this only knows about two statuses.
    """
    status = None
    cause = exc.__cause__
    if cause is not None and cause.args and isinstance(cause.args[0], int):
        status = cause.args[0]
    if status in _KEYCHAIN_NOT_OURS:
        return (
            f"the keychain will not let this program replace the existing "
            f"{KEYRING_SERVICE!r} entry ({_KEYCHAIN_NOT_OURS[status]}, {status}): "
            f"it was created by a different program, and macOS refuses to change "
            f"an item's owner. Delete it once and store the new token:\n"
            f"  security delete-generic-password -s {KEYRING_SERVICE} "
            f"-a {account}\n"
            f"Nothing else is lost -- the entry holds only the token you are "
            f"replacing."
        )
    return str(exc)


def _state_path(archive_dir: Path) -> Path:
    return archive_dir / STATE_FILE


def _read_state(archive_dir: Path) -> dict[str, dict[str, str]]:
    """Load the per-query fetch log, treating any damage as absent.

    A corrupt sidecar must not block fetching -- the guard is a courtesy to
    the request budget, not a correctness invariant, so it fails open.
    """
    path = _state_path(archive_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(archive_dir: Path, state: dict[str, dict[str, str]]) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    tmp = _state_path(archive_dir).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(_state_path(archive_dir))


def _check_cooldown(archive_dir: Path, query_id: str, cooldown_s: int) -> None:
    """Raise FetchCooldown when this query ran inside the cooldown window."""
    if cooldown_s <= 0:
        return
    entry = _read_state(archive_dir).get(str(query_id)) or {}
    stamp = entry.get("last_fetch")
    if not stamp:
        return
    try:
        last = datetime.fromisoformat(stamp)
    except ValueError:
        return
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    elapsed = datetime.now(UTC) - last
    if elapsed < timedelta(seconds=cooldown_s):
        raise FetchCooldown(
            query_id=str(query_id),
            last_fetch=last,
            retry_after_s=int((timedelta(seconds=cooldown_s) - elapsed).total_seconds()),
        )


def cooldown_remaining(
    archive_dir: Path, query_id: str, cooldown_s: int = FETCH_COOLDOWN_S
) -> int:
    """Seconds until `query_id` may be fetched again; 0 when allowed now.

    The non-raising counterpart to `_check_cooldown`, for callers that want to
    show or reason about the guard rather than be stopped by it -- a UI needs
    to grey out a button and say why, not catch an exception to find out.
    """
    try:
        _check_cooldown(archive_dir, query_id, cooldown_s)
    except FetchCooldown as exc:
        return max(0, exc.retry_after_s)
    return 0


def last_fetch(archive_dir: Path, query_id: str) -> str | None:
    """ISO timestamp of the last fetch of `query_id`, or None if never."""
    entry = _read_state(archive_dir).get(str(query_id)) or {}
    return entry.get("last_fetch")


def _record_fetch(archive_dir: Path, query_id: str, digest: str, path: Path) -> None:
    state = _read_state(archive_dir)
    state[str(query_id)] = {
        "last_fetch": datetime.now(UTC).isoformat(timespec="seconds"),
        "sha256": digest,
        "archive": path.name,
    }
    _write_state(archive_dir, state)


def archive_digest(path: Path) -> str:
    """SHA-256 of an archived statement, used to detect exact duplicates."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _find_identical(raw: bytes, archive_dir: Path) -> Path | None:
    """An already-archived file byte-identical to `raw`, if one exists.

    IBKR generates an Activity statement once per calendar day and re-serves
    the same bytes for the rest of it -- verified: four fetches on one day
    returned identical content, same sha256 and the same whenGenerated of
    05:06:22. So a daily cron plus any manual sync produces exact duplicates,
    and writing a fresh timestamped file each time makes the archive grow
    without adding information.

    Size is compared first so a full read is only done for real candidates.
    """
    if not archive_dir.is_dir():
        return None
    size, digest = len(raw), hashlib.sha256(raw).hexdigest()
    for candidate in sorted(archive_dir.glob("activity-*.xml")):
        if candidate.stat().st_size != size:
            continue
        if archive_digest(candidate) == digest:
            return candidate
    return None


def _archive(
    raw: bytes,
    archive_dir: Path,
    *,
    prefix: str = ACTIVITY_PREFIX,
    stamp_format: str = "%Y%m%dT%H%M%SZ",
) -> tuple[Path, Path | None]:
    """Archive raw XML, reusing an identical existing file if there is one.

    Returns (path_to_use, duplicate_of). When a duplicate is found nothing is
    written and `path_to_use` is the pre-existing file, so the archive holds
    exactly one copy of each distinct statement.

    `prefix` names the query TYPE, and the two must not share one: everything that
    walks the archive -- `archive.newest_statement`, the statements inventory, the
    ingest -- globs `activity-*.xml` and would try to read a confirm as a
    statement. A confirm is a different schema, not a smaller statement.
    """
    existing = _find_identical(raw, archive_dir)
    if existing is not None:
        return existing, existing

    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime(stamp_format)
    dest = archive_dir / f"{prefix}-{stamp}.xml"
    dest.write_bytes(raw)
    return dest, None


@dataclass(frozen=True, slots=True)
class ConfirmFetch:
    """A downloaded Trade Confirmation payload, archived and unparsed.

    Deliberately NOT a `FetchResult`. That type carries a parsed
    `FlexQueryResponse`, and py_ibkr models the Activity Statement only -- there is
    no `TCF` model to put there. Making its `response` optional would push a
    `None` check into every Activity caller to serve a query type they never see.
    `confirms.parse_confirms` reads the file this points at.
    """

    raw_path: Path
    raw_bytes: int
    duplicate_of: Path | None = None

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_of is not None


def fetch_confirms(
    query_id: str,
    *,
    archive_dir: Path,
    from_date: str | None = None,
    to_date: str | None = None,
    account: str | None = None,
    force: bool = False,
    cooldown_s: int = FETCH_COOLDOWN_S,
) -> ConfirmFetch:
    """Download a Trade Confirmation query and archive it. No parse.

    Everything `fetch` protects is protected here too, and by the same code: the
    cross-process lock, the per-query cooldown, the content dedupe and the state
    stamp. The cooldown is keyed by QUERY ID, so the confirm query gets its own
    budget rather than sharing the statement's -- which is what makes polling this
    every half hour compatible with a daily statement sync.

    A shorter `cooldown_s` than the statement's default is the point of the
    parameter: confirms change through the session, where an Activity Statement is
    regenerated once a day. See `jobs.CONFIRM_COOLDOWN_S`.
    """
    with locked(archive_dir / FETCH_LOCK):
        if not force:
            _check_cooldown(archive_dir, query_id, cooldown_s)
        token = read_token(account)
        client = _client_factory(user_agent=USER_AGENT)
        log.info("requesting Flex confirms query %s", query_id)
        try:
            raw = client.download(
                token,
                query_id,
                max_retries=MAX_RETRIES,
                retry_interval=RETRY_INTERVAL,
                max_retry_interval=MAX_RETRY_INTERVAL,
                from_date=_norm_date(from_date),
                to_date=_norm_date(to_date),
            )
        except FlexError as exc:
            _reraise_if_token_rejected(exc)
            raise
        # ONE FILE PER DAY, overwritten by each poll, where the statement gets one
        # per fetch. The reason is in the payload: `whenGenerated` changes on every
        # request, so the bytes are never identical and the content dedupe cannot
        # collapse them -- polling every 25 minutes would archive fifteen files a
        # session and open fifteen `statements` rows for one day of fills.
        #
        # Nothing is lost by overwriting. A confirm payload is CUMULATIVE for its
        # period, so the last poll of the day is a superset of every earlier one,
        # and the Activity Statement supersedes all of it tomorrow anyway.
        path, duplicate_of = _archive(
            raw, archive_dir, prefix=CONFIRM_PREFIX, stamp_format="%Y%m%d",
        )
        if duplicate_of is not None:
            log.info("confirms identical to %s; not archiving a second copy",
                     path.name)
        else:
            log.info("archived %d bytes to %s", len(raw), path)
        _record_fetch(archive_dir, query_id, hashlib.sha256(raw).hexdigest(), path)
        return ConfirmFetch(
            raw_path=path, raw_bytes=len(raw), duplicate_of=duplicate_of,
        )


def _norm_date(value: str | None) -> str | None:
    """Normalise a period-override date to the YYYYMMDD Flex expects.

    Accepts YYYY-MM-DD too, since that is the more natural thing to type and
    a rejected request still costs us one against IBKR's lockout budget.
    """
    if value is None:
        return None
    compact = value.replace("-", "").strip()
    if len(compact) != 8 or not compact.isdigit():
        raise ValueError(
            f"date {value!r} is not YYYYMMDD or YYYY-MM-DD"
        )
    return compact


def fetch(
    query_id: str,
    *,
    archive_dir: Path,
    from_date: str | None = None,
    to_date: str | None = None,
    account: str | None = None,
    force: bool = False,
    cooldown_s: int = FETCH_COOLDOWN_S,
) -> FetchResult:
    """Download and parse a Flex query.

    `from_date`/`to_date` accept YYYYMMDD or YYYY-MM-DD and ask IBKR to
    override the period baked into the query template, sent as the `fd`/`td`
    parameters. Support is not guaranteed for every query type; when ignored,
    the template's own period applies.

    Two independent protections apply, addressing different costs:

    * Before the request, a local cooldown per query ID. An Activity
      Statement is regenerated once a day, so a second fetch inside the
      window cannot return new information and can only spend a request
      against IBKR's lockout allowance. Raises `FetchCooldown` without
      sending anything. `force=True` bypasses it.
    * After the request, content dedupe. If the bytes match a file already
      archived, no second copy is written. The request is already spent by
      then, so this protects the archive rather than the budget.

    The raw XML is archived before parsing, so a parse failure still leaves
    the response on disk rather than costing another request.

    HELD UNDER A CROSS-PROCESS LOCK FROM THE CHECK TO THE STAMP, because the
    cooldown was otherwise check-then-act and the budget it guards is real. The
    stamp is written only after a SUCCESSFUL download (so a transient failure does
    not lock out a retry), which means the window between "cooldown cleared" and
    "cooldown recorded" spans the whole request. That window is wide, not
    theoretical: `read_token` alone measured 8.2 SECONDS on this machine, and the
    download retries while IBKR generates the statement. Two threads behind a
    barrier both cleared the guard, and three call sites can enter it -- the Sync
    button, `optjournal fetch`, and `optjournal sync` (the noon cron). A sync
    firing while a page is open is an ordinary Tuesday, and the cost of losing that
    race is two requests spent against a lockout allowance.

    `threading.Lock` could not have fixed this: the cron and the server are
    different processes. `web.ServeConfig.sync_lock` remains, and is not
    redundant -- it fails FAST for a second browser tab with "a sync is already
    running", which is a better answer than making someone wait. This lock is the
    correctness floor underneath it.

    The lock covers `force=True` too. Forcing skips the COOLDOWN, which is a
    judgement about whether new data can exist; it does not make two simultaneous
    downloads writing one archive directory a good idea.
    """
    with locked(archive_dir / FETCH_LOCK):
        return _fetch_locked(
            query_id, archive_dir=archive_dir, from_date=from_date,
            to_date=to_date, account=account, force=force, cooldown_s=cooldown_s,
        )


def _fetch_locked(
    query_id: str,
    *,
    archive_dir: Path,
    from_date: str | None = None,
    to_date: str | None = None,
    account: str | None = None,
    force: bool = False,
    cooldown_s: int = FETCH_COOLDOWN_S,
) -> FetchResult:
    """The fetch itself. Call `fetch`, which holds the lock."""
    if not force:
        _check_cooldown(archive_dir, query_id, cooldown_s)

    token = read_token(account)
    # ONE SEAM NAME. `_client_factory` rather than naming the class here, because
    # the class name IS the stub point: `tests/test_locks.py` replaces
    # `flex.FlexClient` to keep the cross-process lock tests off the network, and
    # introducing `_TimeoutFlexClient` at the call site silently broke that -- the
    # stub still applied to a name nothing called, and two subprocesses went to the
    # real IBKR endpoint. Caught by that test failing; it would otherwise have been
    # a suite that quietly started making network calls.
    client = _client_factory(user_agent=USER_AGENT)

    log.info("requesting Flex query %s", query_id)
    try:
        raw = client.download(
            token,
            query_id,
            max_retries=MAX_RETRIES,
            retry_interval=RETRY_INTERVAL,
            max_retry_interval=MAX_RETRY_INTERVAL,
            from_date=_norm_date(from_date),
            to_date=_norm_date(to_date),
        )
    except FlexError as exc:
        _reraise_if_token_rejected(exc)
        raise

    path, duplicate_of = _archive(raw, archive_dir)
    if duplicate_of is not None:
        log.info("statement identical to %s; not archiving a second copy", path.name)
    else:
        log.info("archived %d bytes to %s", len(raw), path)

    # Recorded after a successful download so a failed request does not start
    # a cooldown -- otherwise one transient error would lock out retries.
    _record_fetch(archive_dir, query_id, hashlib.sha256(raw).hexdigest(), path)

    # Parse from the archived file: py_ibkr's public entry point takes a
    # path, and this keeps fetch() and load() on the same code path.
    return FetchResult(
        response=parse_xml_file(str(path)),
        raw_path=path,
        raw_bytes=len(raw),
        duplicate_of=duplicate_of,
    )


def load(path: Path) -> FlexQueryResponse:
    """Parse a previously archived statement, making no network request."""
    return parse_xml_file(str(path))
