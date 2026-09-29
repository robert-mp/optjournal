"""One sync path: fetch the newest statement, fold it in, snapshot.

ITS OWN MODULE BECAUSE OF THE IMPORT GRAPH, and that is not a filing decision.
This lived in `web.py` for one commit, and `jobs.py` reached it through a deferred
`from optjournal.web import sync_journal` inside a function -- which works, and
which `tests/test_layering.py` correctly called a cycle: `cli -> jobs -> web ->
jobs`. A deferred import is a workaround for a graph that is wrong, and the README
already says TYPE_CHECKING-style dodges are a design smell; the honest fix is that
this function was never a web concern. It needs `flex`, `ingest` and `db`, all of
which sit below `jobs`, so it belongs here and everything imports downhill.

THREE CALLERS, ONE IMPLEMENTATION: `POST /api/sync`, `optjournal sync`, and the
`sync` job. They used to be two implementations of one sequence and had already
drifted -- `new_trades` held a COUNT in one and the row LIST in the other, one name
and two types computed from the same table. Nothing broke only because each
consumer had met just one producer, which is exactly what makes that shape a trap
rather than a wart.

IT RAISES rather than returning an error dict. `FetchCooldown` and `TokenMissing`
are the two outcomes a caller must distinguish, and each caller needs a different
shape for them: an HTTP body with `retry_after_s` as a number, a CLI exit code, a
ledger status. Flattening them here forced every caller to re-derive the
distinction from a string, and that is how the 2026-08-07 keychain failure became
a bare exit 1 that reached nobody.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from py_ibkr import FlexError

from optjournal.archive import account_opened
from optjournal.clock import MARKET_TZ
from optjournal.flex import fetch
from optjournal.ingest import DEFAULT_ASSET_FILTER, ingest_file

__all__ = [
    "SNAPSHOTS_KEPT", "SNAPSHOT_DIR", "first_sync_window", "history_chunks",
    "import_history", "sync_journal",
]

log = logging.getLogger(__name__)

#: How far the first sync reaches back, as an inclusive span. IBKR serves at most
#: ~365 days per Flex request; the two full-year statements in `raw/` are
#: 20250801-20260731 and 20250810-20260803, both accepted.
FIRST_SYNC_SPAN_DAYS = 365


def first_sync_window(today: date) -> tuple[str, str]:
    """The `fd`/`td` pair for a journal's first sync: the last year, as YYYYMMDD.

    WHY A NEW JOURNAL NEEDS ONE: the saved query is `Last30CalendarDays`, so
    without an override a new user's first sync returns a month and the older
    eleven are never asked for.

    Three IBKR rules shape it, and each broken one costs a request against the
    lockout budget: both dates or neither, `td` no later than yesterday, and no
    weekend dates. py_ibkr's CLI applies the last two; `FlexClient.download`,
    which `flex.fetch` calls, does not. So the end rolls BACK to a weekday; the
    start needs no roll, because 364 days is exactly 52 weeks and it lands on
    the end's own weekday.
    """
    to = _weekday_back(today - timedelta(days=1))
    start = to - timedelta(days=FIRST_SYNC_SPAN_DAYS - 1)
    return start.strftime("%Y%m%d"), to.strftime("%Y%m%d")


def _weekday_back(day: date) -> date:
    """`day`, or the Friday before it when it falls on a weekend."""
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _weekday_forward(day: date) -> date:
    """`day`, or the Monday after it when it falls on a weekend."""
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _is_new_journal(conn: sqlite3.Connection) -> bool:
    """No statement has ever been ingested.

    `statements`, not `trades`: an account with a quiet year has statements and
    no trades, and keying on trades would re-request the year on every sync.
    Confirms also write `statements` rows, but `optjournal confirms` refuses to
    run before a statement exists, so an empty table still means a new user.
    """
    return conn.execute("SELECT 1 FROM statements LIMIT 1").fetchone() is None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


#: Snapshots kept beside the journal. Enough to reach back past a bad sync
#: without unbounded growth: the journal is ~2 MB, so eight is ~16 MB.
SNAPSHOTS_KEPT = 8

#: Where `VACUUM INTO` writes. Beside the database rather than inside `raw/`,
#: because `raw/` is the provenance root for BROKER-SUPPLIED files and a snapshot
#: is derived data.
SNAPSHOT_DIR = "snapshots"


def sync_journal(
    *,
    conn: sqlite3.Connection,
    archive_dir: Path,
    query_id: str,
    assets: tuple[str, ...] = DEFAULT_ASSET_FILTER,
    from_date: str | None = None,
    to_date: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Fetch the newest statement, fold it in, snapshot. THE one sync path.

    ONE FUNCTION, THREE CALLERS: `POST /api/sync`, `optjournal sync`, and the
    `sync` job. They previously did different things, which is this project's
    recurring bug shape rather than an inconvenience -- `new_trades` held a COUNT
    in one and the row LIST in the other, one name and two types, and nothing
    broke only because each consumer had met just one producer.

    RAISES rather than returning an error dict, unlike the `_do_sync` it replaces.
    That is the point of the rewrite: `FetchCooldown` and `TokenMissing` are the
    two outcomes callers must distinguish, and each caller wants a different
    shape for them -- an HTTP body, an exit code, a ledger status. Flattening them
    into a dict here forced every caller to re-derive the distinction from a
    string, which is how the 2026-08-07 keychain failure became exit 1.

    Takes an open CONNECTION rather than a path, so the job can run inside the
    transaction that already holds its `running` claim.

    It ends with a snapshot, and the placement is deliberate: beside the write it
    protects, so it cannot be the thing that silently stopped running. Same
    argument that turned `bars-audit` from a cron into a page-load field.

    WHAT IT IS FOR IS THE PERISHABLE HALF, and the split is lopsided enough after
    this change that "it backs up `price_bars`" would name the wrong thing.
    Counted on the real journal: 1,816 `price_bars` rows, of which **329 are hourly
    option bars** that no later run can recover -- an option's intraday series
    exists only while its session is running (the README's asymmetric retention).
    Everything else in that table comes back for the cost of one keyless request.

    Widening `bars.WATCH_LOOKBACK_DAYS` from 60 to 1100 multiplies the RE-FETCHABLE
    half and leaves the perishable count untouched, which is the point worth
    stating rather than the growth. The arithmetic, from the probe behind that
    constant (755 daily closes per symbol at 1100 days) against six watched symbols
    holding ~270 rows between them today: ~4,500 rows where there were ~270, so the
    table lands near 6,000 once the daily job has run at the new window, roughly
    three quarters of it daily closes. Those are two measurements multiplied, not a
    count of the table as it stands, and it is written that way on purpose -- the
    figure that matters is unchanged either way: **329**, plus `market_events` and
    `watchlist`, the two tables holding what no source will re-serve (a feed's
    week, and what you typed). A `raw/` backup would protect none of it: the Flex
    query is `Last30CalendarDays`, so every statement comes back for a request.
    """
    started = _now()
    first_sync = from_date is None and to_date is None and _is_new_journal(conn)
    if first_sync:
        # Still ONE request under the same lock and cooldown, so the lockout
        # guard is untouched; it only asks that request for a longer period.
        from_date, to_date = first_sync_window(datetime.now(MARKET_TZ).date())
        log.info("new journal: requesting %s to %s", from_date, to_date)
    result = fetch(
        query_id, archive_dir=archive_dir,
        from_date=from_date, to_date=to_date, force=force,
    )
    ingested = ingest_file(conn, result.raw_path, assets=assets)
    # `first_seen_at` is stamped per row at insert, so anything at or after this
    # run's start is genuinely new rather than a row re-presented by an
    # overlapping statement.
    new_trade_rows = [
        dict(row) for row in conn.execute(
            "SELECT trade_date, symbol, buy_sell, open_close, quantity,"
            " trade_price, ib_commission, currency FROM trades"
            " WHERE first_seen_at >= ? ORDER BY COALESCE(date_time, trade_date)",
            (started,),
        )
    ]
    new_cash = conn.execute(
        "SELECT COUNT(*) AS n FROM cash_transactions WHERE first_seen_at >= ?",
        (started,),
    ).fetchone()["n"]
    changed = bool(new_trade_rows or new_cash)

    snapshot = None
    if changed:
        # Only when something changed: a snapshot per no-op sync would be seven
        # identical copies a week, and the retention would then evict the one
        # taken before the change that mattered.
        snapshot = _snapshot(conn)

    summary = (
        "statement byte-identical to a previous fetch, nothing to do"
        if ingested.already_ingested
        else f"{len(new_trade_rows)} new trade(s), {new_cash} new cash row(s)"
        if changed
        else f"no new activity (positions refreshed: {ingested.positions_written})"
    )
    if first_sync:
        summary = f"first sync, fetched {from_date} to {to_date}: {summary}"
    return {
        "ok": True,
        "kind": "synced",
        "started_at": started,
        "query_id": query_id,
        "archive": result.raw_path.name,
        "raw_path": str(result.raw_path),
        "raw_bytes": result.raw_bytes,
        "reused_archive": result.is_duplicate,
        "already_ingested": ingested.already_ingested,
        "duplicate_of": ingested.duplicate_of,
        #: A COUNT. `new_trade_rows` is the list, under a name that says so.
        "new_trades": len(new_trade_rows),
        "new_trade_rows": new_trade_rows,
        "new_cash": new_cash,
        "positions_written": ingested.positions_written,
        "warnings": ingested.warnings,
        "changed": changed,
        "snapshot": None if snapshot is None else snapshot.name,
        "summary": summary,
    }


#: Calendar years IBKR keeps before the current one. "Four previous calendar
#: years plus the current one, the same as any saved Flex query":
#: `docs/trade-confirmations.md`, Retention.
HISTORY_YEARS_KEPT = 4

#: Seconds between two chunks of a history import. IBKR's published pacing is
#: 10 requests a minute per token, and one chunk is at least two (SendRequest,
#: GetStatement). Thirty seconds keeps five chunks near four a minute when every
#: statement is ready at once, which is the case that would otherwise burst.
HISTORY_PAUSE_S = 30


def history_chunks(
    covered_from: date | None, opened: date | None, today: date,
) -> list[tuple[str, str]]:
    """The requests a history import makes, NEWEST FIRST, as (fd, td) YYYYMMDD.

    It walks back from the oldest statement the journal holds, so a second
    import asks only for what the first did not reach, and an import that already
    reached the floor asks for nothing. Gaps between statements are not this
    function's to fill: the daily sync's 30-day overlap keeps the recent past
    whole.

    The floor is the later of IBKR's retention (1 January, four years back) and
    the account's opening day. Each chunk holds IBKR's rules the way
    `first_sync_window` does: at most 365 days inclusive, no weekend dates, and
    nothing later than yesterday.

    ADJACENT CHUNKS SHARE A DAY, and so does the first chunk with the oldest
    statement. Ending a chunk the day before the next one starts leaves a hole
    whenever that start is a Monday: the day before is a Sunday, which rolls back
    to Friday, and a cash row dated that weekend is in neither request. Ingest is
    an idempotent upsert, so the shared day costs nothing.
    """
    floor = date(today.year - HISTORY_YEARS_KEPT, 1, 1)
    if opened is not None:
        floor = max(floor, opened)
    if covered_from is not None and covered_from <= floor:
        return []
    end = covered_from if covered_from is not None else today - timedelta(days=1)
    chunks: list[tuple[str, str]] = []
    while True:
        end = _weekday_back(end)
        reach = end - timedelta(days=FIRST_SYNC_SPAN_DAYS - 1)
        start = _weekday_forward(max(reach, floor))
        if end < floor or start > end:
            return chunks
        chunks.append((start.strftime("%Y%m%d"), end.strftime("%Y%m%d")))
        if reach <= floor:
            return chunks
        end = start


def history_plan(
    conn: sqlite3.Connection, archive_dir: Path, *, today: date | None = None,
) -> list[tuple[str, str]]:
    """`history_chunks` for this journal: what an import would ask for now."""
    row = conn.execute(
        "SELECT MIN(from_date) AS earliest FROM statements"
        " WHERE source_file LIKE 'activity-%'"
    ).fetchone()
    opened = account_opened(archive_dir)
    return history_chunks(
        date.fromisoformat(row["earliest"]) if row and row["earliest"] else None,
        datetime.strptime(opened, "%Y%m%d").date() if opened else None,
        today or datetime.now(MARKET_TZ).date(),
    )


def import_history(
    *,
    conn: sqlite3.Connection,
    archive_dir: Path,
    query_id: str,
    assets: tuple[str, ...] = DEFAULT_ASSET_FILTER,
    pause_s: float = HISTORY_PAUSE_S,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Fetch and ingest everything IBKR still holds that the journal does not.

    One chunk per `history_plan` entry, newest first, so the most useful year
    lands first and a run that stops early has still done the best part.

    `force=True` on every fetch, and only here. The cooldown exists because a
    second Activity Statement inside fifteen minutes cannot hold anything new;
    a chunk for a different period can, and the reader confirmed the request
    count before starting. The lock is still taken per fetch.

    STOPS AT THE FIRST REFUSED CHUNK rather than skipping it. Older chunks would
    meet the same refusal (retention, an account younger than its statement
    claims) and each would spend a request to learn it. Credentials are not a
    stop but a raise: no chunk can succeed without them, and the job maps them
    to `failed` the way it does for the daily sync.
    """
    started = _now()
    plan = history_plan(conn, archive_dir)
    fetched: list[str] = []
    stopped: str | None = None
    for i, (fd, td) in enumerate(plan):
        if i:
            sleep(pause_s)
        log.info("history import: requesting %s to %s", fd, td)
        try:
            result = fetch(query_id, archive_dir=archive_dir,
                           from_date=fd, to_date=td, force=True)
        except FlexError as exc:
            stopped = f"{fd} to {td} refused: {exc}"
            log.warning("history import stopped: %s", stopped)
            break
        ingest_file(conn, result.raw_path, assets=assets)
        fetched.append(f"{fd}-{td}")

    new_trades = conn.execute(
        "SELECT COUNT(*) AS n FROM trades WHERE first_seen_at >= ?", (started,),
    ).fetchone()["n"]
    new_cash = conn.execute(
        "SELECT COUNT(*) AS n FROM cash_transactions WHERE first_seen_at >= ?",
        (started,),
    ).fetchone()["n"]
    snapshot = _snapshot(conn) if new_trades or new_cash else None

    if not plan:
        summary = "history already complete back to what IBKR keeps"
    else:
        summary = (f"{len(fetched)} of {len(plan)} year(s) fetched: "
                   f"{new_trades} new trade(s), {new_cash} new cash row(s)")
        if stopped:
            summary += f"; stopped at {stopped}"
    return {
        "ok": True,
        "kind": "history",
        "planned": len(plan),
        "fetched": fetched,
        "stopped": stopped,
        "new_trades": new_trades,
        "new_cash": new_cash,
        "snapshot": None if snapshot is None else snapshot.name,
        "summary": summary,
    }


def _snapshot(conn: sqlite3.Connection) -> Path | None:
    """`VACUUM INTO` a timestamped copy beside the journal. Returns its path.

    One stdlib call: no git, no configurable repo root that could point at the
    wrong repository, and unlike a file copy it is consistent without stopping
    writers -- `VACUUM INTO` reads through one transaction.

    Failures are LOGGED AND SWALLOWED for the same reason `record_run`'s are: a
    backup that fails must not fail the sync it is protecting. The sync's return
    value carries `snapshot: null` when it did, so the page can say so rather than
    the absence being invisible.
    """
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None or not row["file"]:
        return None                       # in-memory journal: nothing to snapshot
    live = Path(row["file"])
    out = live.parent / SNAPSHOT_DIR
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = out / f"{live.stem}-{stamp}.db"
    try:
        out.mkdir(parents=True, exist_ok=True)
        # Parameter binding is not available to VACUUM INTO, so the path is
        # quoted as an SQL string literal. It is derived from the journal's own
        # filename plus a strftime stamp -- no user input reaches it -- and a
        # single quote in a directory name would still be escaped correctly.
        conn.execute(f"VACUUM INTO '{str(target).replace(chr(39), chr(39) * 2)}'")
    except (sqlite3.Error, OSError) as exc:
        log.warning("could not snapshot the journal: %s", exc)
        return None
    _prune_snapshots(out, live.stem)
    return target


def _prune_snapshots(directory: Path, stem: str) -> int:
    """Keep the newest `SNAPSHOTS_KEPT`. Returns how many were deleted.

    Sorted by NAME, not mtime: the stamp is in the filename in a format that
    sorts chronologically, and a name cannot be changed by a file copy the way an
    mtime can.
    """
    try:
        existing = sorted(directory.glob(f"{stem}-*.db"))
    except OSError:
        return 0
    deleted = 0
    for stale in existing[:-SNAPSHOTS_KEPT] if len(existing) > SNAPSHOTS_KEPT else []:
        try:
            stale.unlink()
            deleted += 1
        except OSError as exc:
            log.warning("could not remove old snapshot %s: %s", stale.name, exc)
    return deleted


