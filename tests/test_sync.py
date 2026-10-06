"""The one sync path, and the snapshot that rides on it.

`sync.py` exists because of the IMPORT GRAPH rather than for tidiness. This code
lived in `web.py` for one commit, and `jobs.py` reached it through a deferred
`from optjournal.web import sync_journal` inside a function. That works, and
`tests/test_layering.py` correctly reported it as a cycle -- `cli -> jobs -> web ->
jobs`. A deferred import is a workaround for a wrong graph, so the function moved
to where its dependencies already are: `flex` and `ingest`, both below `jobs`.

What the tests here are about:

* THREE CALLERS, ONE IMPLEMENTATION. `POST /api/sync`, `optjournal sync` and the
  `sync` job. The first two were separate implementations of one sequence and had
  already drifted -- `new_trades` was a COUNT in one and the row LIST in the other,
  one name and two types from the same table.
* THE SNAPSHOT CAPTURES WHAT CANNOT BE REFETCHED. This replaces the 100-line
  `backup.py` the draft plan proposed, and the reason it shrank is worth keeping:
  the draft treated "raw/ is the provenance root" as "raw/ is irreplaceable".
  Measured, it is not -- the daily Flex query is `Last30CalendarDays` and two
  archived statements are full-year pulls, so all 168 trades, 101 cash rows, 63
  snapshots and 261 NAV rows come back from IBKR for the cost of one request. What
  cannot come back is not in `raw/` at all: 329 hourly option bars, `market_events`,
  `watchlist`. A `git add raw/*.xml` protects the cheapest artefact in the project
  and none of the expensive ones.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from conftest import RAW_DIR


@pytest.fixture()
def populated(populated_db) -> Path:
    """A journal with every archived statement ingested.

    The shared fixture under the name this file's assertions read, matching
    test_web.py -- these tests moved from there with `sync_journal`.
    """
    return populated_db



# --------------------------------------------------------------------------
# The snapshot (SCHEDULER_PLAN.md step 5d).
#
# This replaces the 100-line `backup.py` the draft plan proposed, and the reason
# it shrank is worth keeping: the draft treated "`raw/` is the provenance root" as
# "`raw/` is irreplaceable". Measured, it is not -- the daily Flex query is
# `Last30CalendarDays` and two archived statements are full-year pulls, so all 168
# trades, 101 cash rows, 63 snapshots and 261 NAV rows come back from IBKR for the
# cost of one request. What CANNOT come back is not in `raw/` at all: 329 hourly
# option bars, `market_events`, `watchlist`. A `git add raw/*.xml` protects the
# cheapest artefact in the project and none of the expensive ones.
# --------------------------------------------------------------------------


def test_the_snapshot_is_a_complete_readable_journal_not_just_a_file(populated):
    """`VACUUM INTO` must produce something a reader could actually restore from.

    Verified against the REAL archive rather than a hand-built row, and checked
    three ways -- the file exists, `PRAGMA integrity_check` passes, and every row
    count matches the live journal. A backup that exists and cannot be opened is
    worse than none, because it is believed.

    On the real journal this measured 3.32 MB -> 2.46 MB with all 329 hourly option
    bars preserved and `integrity_check` ok.
    """
    from optjournal.db import connect  # noqa: PLC0415 - local to this test
    from optjournal.sync import _snapshot  # noqa: PLC0415 - private by design

    conn = connect(populated)
    tables = ("trades", "cash_transactions", "position_snapshots", "statements")
    before = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
        for table in tables
    }
    snapshot = _snapshot(conn)
    conn.close()

    assert snapshot is not None and snapshot.is_file(), "no snapshot was written"
    restored = connect(snapshot)
    assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok", (
        "the snapshot is corrupt, which is worse than absent because it is trusted"
    )
    after = {
        table: restored.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
        for table in tables
    }
    restored.close()
    assert after == before, f"the snapshot lost rows: {before} -> {after}"


def test_the_snapshot_captures_what_cannot_be_refetched(populated):
    """The whole justification, asserted on the tables that carry it.

    `price_bars` hourly rows are the ones the README says cannot be backfilled at
    any price -- an option's intraday series exists only while its own session
    runs. If a snapshot omitted them it would be protecting only the data IBKR
    would hand back for one request.
    """
    from optjournal.db import connect  # noqa: PLC0415 - local to this test
    from optjournal.sync import _snapshot  # noqa: PLC0415 - private by design

    conn = connect(populated)
    conn.execute(
        "INSERT OR REPLACE INTO price_bars (conid, symbol, bar_size, ts, close,"
        " source, fetched_at) VALUES ('1','X','1h',1786100000,1.0,'yahoo','2026-08-09')")
    conn.execute("INSERT OR IGNORE INTO watchlist (symbol, note, added_at)"
                 " VALUES ('SNAP', 'perishable', '2026-08-09')")
    conn.commit()
    snapshot = _snapshot(conn)
    conn.close()
    assert snapshot is not None

    restored = connect(snapshot)
    assert restored.execute(
        "SELECT COUNT(*) FROM price_bars WHERE bar_size = '1h'").fetchone()[0] >= 1, (
        "the snapshot holds no hourly bars, which are the rows that cannot be "
        "recollected at any price"
    )
    assert restored.execute(
        "SELECT COUNT(*) FROM watchlist WHERE symbol = 'SNAP'").fetchone()[0] == 1
    restored.close()


def test_snapshot_retention_keeps_the_newest_and_deletes_the_oldest(tmp_path):
    """Bounded, or the backup fills the disk it is protecting.

    Sorted by NAME rather than mtime: the stamp is in the filename in a format
    that sorts chronologically, and a name survives a file copy that would reset
    an mtime.
    """
    from optjournal.sync import (  # noqa: PLC0415 - private by design
        SNAPSHOTS_KEPT,
        _prune_snapshots,
    )

    for day in range(SNAPSHOTS_KEPT + 4):
        (tmp_path / f"journal-202608{day:02d}T000000Z.db").write_bytes(b"x")
    # A file that is not a snapshot of this journal must survive untouched.
    (tmp_path / "other-20260801T000000Z.db").write_bytes(b"x")

    deleted = _prune_snapshots(tmp_path, "journal")
    kept = sorted(p.name for p in tmp_path.glob("journal-*.db"))
    assert deleted == 4, f"deleted {deleted}, expected 4"
    assert len(kept) == SNAPSHOTS_KEPT
    assert kept[-1] == f"journal-202608{SNAPSHOTS_KEPT + 3:02d}T000000Z.db", (
        "pruning removed the newest snapshots rather than the oldest"
    )
    assert (tmp_path / "other-20260801T000000Z.db").is_file(), (
        "pruning reached a file belonging to a different journal"
    )


def test_taking_a_snapshot_also_prunes(populated):
    """The retention has to be REACHED, not merely correct.

    Added because an ablation found it: deleting the `_prune_snapshots` call from
    `_snapshot` left every test above green -- they exercise the pruner directly, so
    nothing noticed that snapshots would accumulate forever. A correct function
    nothing calls is the same defect as a wrong one, and this project has now found
    that shape in CSS (`.stats`'s orphaned overrides) and in the markup (`.tbl`) too.

    Drives the real `_snapshot` past the cap rather than reading its source, and
    stamps each call apart: the timestamp has second resolution, so a tight loop
    would ask `VACUUM INTO` for a file that already exists -- which it refuses, and
    the refusal is swallowed. Renaming each result is what makes the loop produce
    distinct snapshots the way a week of syncs would.
    """
    from optjournal.db import connect  # noqa: PLC0415 - local to this test
    from optjournal.sync import (  # noqa: PLC0415 - private by design
        SNAPSHOT_DIR,
        SNAPSHOTS_KEPT,
        _snapshot,
    )

    conn = connect(populated)
    out = populated.parent / SNAPSHOT_DIR
    for i in range(SNAPSHOTS_KEPT + 3):
        written = _snapshot(conn)
        assert written is not None, f"snapshot {i} was not written"
        # A distinct, chronologically-sorting name, as consecutive days would give.
        written.rename(out / f"{populated.stem}-2026{i + 10:04d}T000000Z.db")
    _snapshot(conn)                     # the call whose prune is under test
    held = sorted(out.glob(f"{populated.stem}-*.db"))
    conn.close()
    assert len(held) <= SNAPSHOTS_KEPT, (
        f"{len(held)} snapshots survived a cap of {SNAPSHOTS_KEPT} -- _snapshot is "
        "not pruning, so the backup grows until it fills the disk it protects"
    )


def test_a_failed_snapshot_does_not_fail_the_sync_it_protects(populated, monkeypatch):
    """Same policy as the ledger: the backup must not break the thing it backs up.

    A sync that spent an IBKR request and ingested a statement must not report
    failure because a disk was full. The return value carries `snapshot: null` so
    the absence is visible rather than silent -- which is the distinction the
    audit's `witnesses` field exists for, one layer down.
    """
    from optjournal.db import connect  # noqa: PLC0415 - local to this test
    from optjournal.sync import _snapshot  # noqa: PLC0415 - private by design

    conn = connect(populated)

    def explode(*_a, **_k):
        raise OSError("no space left on device")

    monkeypatch.setattr(Path, "mkdir", explode)
    assert _snapshot(conn) is None, (
        "a failed snapshot must return None, not raise into the sync"
    )
    conn.close()


def test_the_snapshot_is_taken_on_the_sync_path_and_not_as_its_own_job():
    """Placement is the argument, so it is worth an assertion.

    Beside the write it protects, it cannot be the thing that silently stopped
    running -- the same reasoning that turned `bars-audit` from a cron into a
    page-load field, and this project has now watched three scheduled jobs report
    health for two days while doing nothing.
    """
    import inspect  # noqa: PLC0415 - local to this test

    from optjournal.jobs import JOBS  # noqa: PLC0415 - local to this test
    from optjournal.sync import sync_journal  # noqa: PLC0415 - local

    assert "_snapshot(" in inspect.getsource(sync_journal), (
        "the sync no longer snapshots, so nothing captures the perishable bars"
    )
    assert not [job for job in JOBS if "snap" in job.name or "backup" in job.name], (
        "the snapshot became a scheduled job, which makes it something that can "
        "stop running unnoticed"
    )


def test_a_sync_that_changed_nothing_writes_no_snapshot(populated, monkeypatch):
    """Seven identical copies a week would evict the one worth keeping.

    Retention is 8, so an unconditional snapshot on a quiet week rolls the window
    past the change that mattered. Asserted through the real `sync_journal` with
    the fetch stubbed, because the CONDITION is the interesting part.

    `_now` IS PINNED TO THE FUTURE, and the reason is a real property worth knowing:
    "new" means `first_seen_at >= started`, and both are ISO stamps at SECOND
    resolution. So a row ingested in the same second the sync begins counts as new.
    Harmless in production -- a Flex fetch takes seconds and the fixture's ingest is
    a separate event -- but it made the first version of this test fail against a
    fixture that had just ingested. Pinning the clock states the intent (no rows
    arrived after this sync started) instead of racing a one-second boundary.
    """
    from optjournal import sync as mod  # noqa: PLC0415 - local to this test
    from optjournal.archive import newest_statement  # noqa: PLC0415 - local
    from optjournal.db import connect  # noqa: PLC0415 - local to this test

    archive = newest_statement(RAW_DIR)
    assert archive is not None, "the archive holds no statement to re-ingest"

    class _Fetched:
        raw_path = archive
        raw_bytes = archive.stat().st_size
        is_duplicate = True

    monkeypatch.setattr(mod, "fetch", lambda *_a, **_k: _Fetched())
    monkeypatch.setattr(mod, "_now", lambda: "2099-01-01T00:00:00+00:00")
    calls = []
    monkeypatch.setattr(mod, "_snapshot", lambda conn: calls.append(conn))

    conn = connect(populated)
    # Re-ingesting an already-ingested statement changes nothing by construction.
    result = mod.sync_journal(
        conn=conn, archive_dir=RAW_DIR, query_id="1591754",
    )
    conn.close()
    assert result["changed"] is False, "premise: this sync changed nothing"
    assert not calls, (
        "a no-op sync took a snapshot, so a quiet week evicts the snapshot from "
        "the day something actually happened"
    )
    assert result["snapshot"] is None


# --------------------------------------------------------------------------
# The first sync reaches back a year.
#
# The saved query is `Last30CalendarDays`, so a new user's first sync used to
# return a month. `sync_journal` now asks that one request for the last year when
# no statement has ever been ingested.
# --------------------------------------------------------------------------


def _stub_fetch(monkeypatch, calls: list[dict]):
    """Record what `sync_journal` asked for; answer with a real archived statement."""
    from optjournal import sync as mod  # noqa: PLC0415 - local to this test
    from optjournal.archive import newest_statement  # noqa: PLC0415 - local

    archive = newest_statement(RAW_DIR)
    assert archive is not None, "the archive holds no statement to ingest"

    class _Fetched:
        raw_path = archive
        raw_bytes = archive.stat().st_size
        is_duplicate = True

    def fetch(*_a, **kwargs):
        calls.append(kwargs)
        return _Fetched()

    monkeypatch.setattr(mod, "fetch", fetch)
    monkeypatch.setattr(mod, "_snapshot", lambda conn: None)
    return mod


def test_a_new_journal_asks_for_the_last_year(tmp_path, monkeypatch):
    from optjournal.db import open_journal  # noqa: PLC0415 - local to this test

    calls: list[dict] = []
    mod = _stub_fetch(monkeypatch, calls)
    with open_journal(tmp_path / "journal.db") as conn:
        result = mod.sync_journal(conn=conn, archive_dir=tmp_path, query_id="1")

    (asked,) = calls
    assert asked["from_date"] is not None and asked["to_date"] is not None, (
        "a new journal got the template's 30 days instead of a year"
    )
    assert result["summary"].startswith(
        f"first sync, fetched {asked['from_date']} to {asked['to_date']}"
    )


def _newest_statement_end(conn) -> date:
    (last,) = conn.execute("SELECT MAX(REPLACE(to_date, '-', '')) FROM statements"
                           " WHERE source_file LIKE 'activity-%'").fetchone()
    return datetime.strptime(last, "%Y%m%d").date()


def test_a_journal_with_statements_keeps_the_template_period(populated, monkeypatch):
    from optjournal.db import connect  # noqa: PLC0415 - local to this test

    calls: list[dict] = []
    mod = _stub_fetch(monkeypatch, calls)
    conn = connect(populated)
    today = _newest_statement_end(conn) + timedelta(days=3)
    result = mod.sync_journal(conn=conn, archive_dir=RAW_DIR, query_id="1", today=today)
    conn.close()

    (asked,) = calls
    assert asked["from_date"] is None and asked["to_date"] is None, (
        "a daily sync re-requested a year, spending a longer generation for nothing"
    )
    assert not result["summary"].startswith("first sync")


def test_a_journal_behind_by_more_than_the_query_asks_for_the_whole_gap(
    populated, monkeypatch
):
    """Shut for five weeks, the query's 30 days left the days before them unasked
    for, for good. One request still, for the days since the newest statement."""
    from optjournal.db import connect  # noqa: PLC0415 - local to this test

    calls: list[dict] = []
    mod = _stub_fetch(monkeypatch, calls)
    conn = connect(populated)
    last = _newest_statement_end(conn)
    result = mod.sync_journal(conn=conn, archive_dir=RAW_DIR, query_id="1",
                              today=last + timedelta(days=40))
    conn.close()

    (asked,) = calls
    start, end = (datetime.strptime(asked[k], "%Y%m%d").date()
                  for k in ("from_date", "to_date"))
    assert start <= last + timedelta(days=2) and start.weekday() < 5
    assert end >= last + timedelta(days=36) and end.weekday() < 5
    assert result["summary"].startswith("caught up a gap")


def test_the_gap_counts_activity_statements_only_and_is_capped_at_a_year(tmp_path):
    from conftest import add_statement, connect_migrated  # noqa: PLC0415

    from optjournal.sync import FIRST_SYNC_SPAN_DAYS, GAP_DAYS, gap_window  # noqa: PLC0415

    conn = connect_migrated(tmp_path / "j.db")
    add_statement(conn, source_file="activity-1.xml", sha256="a",
                  from_date="2025-01-01", to_date="2025-03-31")
    # A confirmation is one day of fills, not coverage of the days before it.
    add_statement(conn, source_file="confirm-20260601.xml", sha256="c",
                  from_date="20260601", to_date="20260601")
    conn.commit()
    today = date(2026, 6, 3)
    start, end = (datetime.strptime(d, "%Y%m%d").date() for d in gap_window(conn, today))
    assert end == date(2026, 6, 2)
    assert (end - start).days + 1 <= FIRST_SYNC_SPAN_DAYS
    assert start.weekday() < 5
    assert gap_window(conn, date(2025, 3, 31) + timedelta(days=GAP_DAYS)) is None


def test_dates_passed_explicitly_win_on_a_new_journal(tmp_path, monkeypatch):
    from optjournal.db import open_journal  # noqa: PLC0415 - local to this test

    calls: list[dict] = []
    mod = _stub_fetch(monkeypatch, calls)
    with open_journal(tmp_path / "journal.db") as conn:
        mod.sync_journal(conn=conn, archive_dir=tmp_path, query_id="1",
                         from_date="20260105", to_date="20260109")

    (asked,) = calls
    assert (asked["from_date"], asked["to_date"]) == ("20260105", "20260109")


@pytest.mark.parametrize(("today", "expected"), [
    # Tuesday: yesterday is Monday, a year back is a Tuesday.
    (date(2026, 9, 29), ("20250929", "20260928")),
    # Monday: yesterday is Sunday, so the end rolls back to Friday.
    (date(2026, 9, 28), ("20250926", "20260925")),
    # Sunday: the end rolls back to Friday, and the start lands 52 weeks
    # earlier on a Friday too.
    (date(2026, 8, 9), ("20250808", "20260807")),
])
def test_first_sync_window_edges(today, expected):
    from optjournal.sync import first_sync_window  # noqa: PLC0415 - local

    assert first_sync_window(today) == expected


def test_first_sync_window_holds_ibkrs_rules_for_every_day_of_a_year():
    from optjournal.sync import FIRST_SYNC_SPAN_DAYS, first_sync_window  # noqa: PLC0415

    start_of_year = date(2026, 1, 1)
    for offset in range(366):
        today = start_of_year + timedelta(days=offset)
        start, end = (datetime.strptime(d, "%Y%m%d").date()
                      for d in first_sync_window(today))
        assert end < today, f"{today}: td {end} is not before today"
        assert end.weekday() < 5 and start.weekday() < 5, f"{today}: weekend date"
        assert (end - start).days + 1 <= FIRST_SYNC_SPAN_DAYS, f"{today}: too long"


# --------------------------------------------------------------------------
# The history import: everything IBKR still holds before the oldest statement.
# --------------------------------------------------------------------------


def _days(chunks):
    return [(datetime.strptime(a, "%Y%m%d").date(), datetime.strptime(b, "%Y%m%d").date())
            for a, b in chunks]


def test_history_walks_back_to_the_account_opening_newest_first():
    from optjournal.sync import history_chunks  # noqa: PLC0415 - local

    chunks = history_chunks(date(2025, 8, 1), date(2022, 5, 19), date(2026, 9, 29))
    assert chunks[0][1] == "20250801", "the newest chunk must meet the oldest statement"
    assert chunks[-1][0] == "20220519", "the import stops at the account's opening"
    assert chunks == sorted(chunks, reverse=True), "newest year first"


def test_history_stops_at_ibkrs_retention_for_an_older_account():
    from optjournal.sync import history_chunks  # noqa: PLC0415 - local

    chunks = history_chunks(None, date(2015, 1, 5), date(2026, 9, 29))
    # 1 January 2022 is a Saturday, so the floor rolls forward to Monday.
    assert chunks[-1][0] == "20220103"
    assert chunks[0][1] == "20260928", "with no statement it ends yesterday"


def test_history_that_already_reaches_the_floor_asks_for_nothing():
    from optjournal.sync import history_chunks  # noqa: PLC0415 - local

    assert history_chunks(date(2022, 5, 19), date(2022, 5, 19), date(2026, 9, 29)) == []
    assert history_chunks(date(2021, 3, 1), None, date(2026, 9, 29)) == []


@pytest.mark.parametrize("covered_from", [
    date(2025, 8, 11),   # a Monday, the case that used to leave a weekend hole
    date(2025, 8, 1),
    date(2025, 8, 3),    # a Sunday
    None,
])
def test_history_chunks_hold_ibkrs_rules_and_leave_no_gap(covered_from):
    from optjournal.sync import FIRST_SYNC_SPAN_DAYS, history_chunks  # noqa: PLC0415

    today = date(2026, 9, 29)
    chunks = _days(history_chunks(covered_from, date(2022, 5, 19), today))
    assert chunks
    for start, end in chunks:
        assert start.weekday() < 5 and end.weekday() < 5, f"weekend in {start}-{end}"
        assert start <= end < today
        assert (end - start).days + 1 <= FIRST_SYNC_SPAN_DAYS, f"{start}-{end} too long"
    # Each older chunk must reach the newer one's start, or the days between them
    # are in no request at all.
    for (newer_start, _), (_, older_end) in zip(chunks, chunks[1:], strict=False):
        assert older_end >= newer_start - timedelta(days=1), (
            f"gap between {older_end} and {newer_start}")
    if covered_from is not None:
        assert chunks[0][1] >= covered_from - timedelta(days=3), (
            "gap before the oldest statement")


def _stub_history(monkeypatch, *, plan, refuse_after=None, busy_after=None):
    """`import_history` with the plan fixed and the network replaced."""
    from py_ibkr import FlexError  # noqa: PLC0415 - local to this test

    from optjournal import sync as mod  # noqa: PLC0415 - local to this test
    from optjournal.archive import newest_statement  # noqa: PLC0415 - local
    from optjournal.locks import LockTimeout  # noqa: PLC0415 - local

    archive = newest_statement(RAW_DIR)
    calls: list[dict] = []

    class _Fetched:
        raw_path = archive

    def fetch(*_a, **kwargs):
        if refuse_after is not None and len(calls) >= refuse_after:
            calls.append(kwargs)
            raise FlexError("1003: Statement is not available.")
        if busy_after is not None and len(calls) >= busy_after:
            calls.append(kwargs)
            raise LockTimeout("raw/.fetch.lock held by another fetch")
        calls.append(kwargs)
        return _Fetched()

    monkeypatch.setattr(mod, "history_plan", lambda *_a, **_k: plan)
    monkeypatch.setattr(mod, "fetch", fetch)
    monkeypatch.setattr(mod, "_snapshot", lambda conn: None)
    return mod, calls


def test_import_history_fetches_every_chunk_forced_and_paced(tmp_path, monkeypatch):
    from optjournal.db import open_journal  # noqa: PLC0415 - local to this test

    plan = [("20240802", "20250801"), ("20230804", "20240802")]
    mod, calls = _stub_history(monkeypatch, plan=plan)
    slept: list[float] = []
    with open_journal(tmp_path / "journal.db") as conn:
        result = mod.import_history(conn=conn, archive_dir=tmp_path, query_id="1",
                                    sleep=slept.append)

    assert [(c["from_date"], c["to_date"]) for c in calls] == plan
    assert all(c["force"] for c in calls), (
        "a chunk hit the 15-minute cooldown meant for re-fetching one statement")
    assert slept == [mod.HISTORY_PAUSE_S], "one pause between two chunks, none before"
    assert result["fetched"] == ["20240802-20250801", "20230804-20240802"]
    assert result["stopped"] is None


def test_import_history_stops_at_the_first_refusal(tmp_path, monkeypatch):
    from optjournal.db import open_journal  # noqa: PLC0415 - local to this test

    plan = [("20240802", "20250801"), ("20230804", "20240802"), ("20220805", "20230804")]
    mod, calls = _stub_history(monkeypatch, plan=plan, refuse_after=1)
    with open_journal(tmp_path / "journal.db") as conn:
        result = mod.import_history(conn=conn, archive_dir=tmp_path, query_id="1",
                                    sleep=lambda _s: None)

    assert len(calls) == 2, "an older chunk was asked for after a refusal"
    assert result["fetched"] == ["20240802-20250801"]
    assert "20230804 to 20240802 refused" in result["stopped"]
    assert "stopped at" in result["summary"]


def test_import_history_behind_another_fetch_says_what_it_had_already_done(
    tmp_path, monkeypatch,
):
    """A lock wait after the first chunk is a stop, like a refusal: that chunk
    spent its request and is ingested, and the job reading a raised LockTimeout
    as busy would have recorded "nothing asked". Before any chunk it IS nothing
    asked, so it is still raised for the caller to read as busy."""
    from optjournal.db import open_journal  # noqa: PLC0415 - local to this test
    from optjournal.locks import LockTimeout  # noqa: PLC0415

    plan = [("20240802", "20250801"), ("20230804", "20240802"), ("20220805", "20230804")]
    mod, calls = _stub_history(monkeypatch, plan=plan, busy_after=1)
    with open_journal(tmp_path / "journal.db") as conn:
        result = mod.import_history(conn=conn, archive_dir=tmp_path, query_id="1",
                                    sleep=lambda _s: None)
    assert len(calls) == 2
    assert result["fetched"] == ["20240802-20250801"]
    assert "20230804 to 20240802 not asked" in result["stopped"]

    mod, calls = _stub_history(monkeypatch, plan=plan, busy_after=0)
    with open_journal(tmp_path / "journal.db") as conn, pytest.raises(LockTimeout):
        mod.import_history(conn=conn, archive_dir=tmp_path, query_id="1",
                           sleep=lambda _s: None)


def test_the_apps_fetches_wait_for_the_lock_less_than_the_scheduler_can_stall(
):
    """The scheduler runs its jobs on one thread and the page calls it dead past
    `HEARTBEAT_STALE_S`, while its jobs and the page's Sync read a lock timeout
    as busy. So the app's default wait is short; only the command line, which
    has nothing else to do, waits out a whole fetch."""
    import inspect  # noqa: PLC0415 - local to this test

    from optjournal import flex, serialize, sync  # noqa: PLC0415

    for fn in (flex.fetch, flex.fetch_confirms, sync.sync_journal):
        wait = inspect.signature(fn).parameters["lock_timeout_s"].default
        assert wait == flex.FETCH_LOCK_WAIT_S < serialize.HEARTBEAT_STALE_S, fn
    assert flex.FETCH_LOCK_TIMEOUT_S > flex.FETCH_WORST_CASE_S


def test_import_history_with_nothing_owed_spends_nothing(tmp_path, monkeypatch):
    from optjournal.db import open_journal  # noqa: PLC0415 - local to this test

    mod, calls = _stub_history(monkeypatch, plan=[])
    with open_journal(tmp_path / "journal.db") as conn:
        result = mod.import_history(conn=conn, archive_dir=tmp_path, query_id="1")
    assert calls == []
    assert result["planned"] == 0 and "already complete" in result["summary"]
