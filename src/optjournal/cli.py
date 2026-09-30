"""Command line entry point.

Structure: this module is wiring only. Data extraction lives in the domain
modules, presentation in `render.py`, so every command can emit a human
table or `--json` from one source of truth.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import re
import sqlite3
import sys
import tempfile
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from py_ibkr import FlexError, FlexLockoutError, FlexRateLimitError

from optjournal import __version__, browser, install, logs, settings
from optjournal.analysis import analyse, format_report
from optjournal.archive import newest_statement, prune_archive
from optjournal.bars import (
    audit_perishable,
    backfill_bars,
    bars_manifest,
)
from optjournal.clock import MARKET_TZ, parse_day
from optjournal.compat import unknown_codes
from optjournal.config import (
    DATA_HOME,
    DEFAULT_ARCHIVE,
    DEFAULT_DB,
    DEFAULT_DEMO_DB,
    DEFAULT_DEMO_DIR,
    ROOT,
)
from optjournal.costs import CostScope, build_costs
from optjournal.db import connect, open_journal
from optjournal.events import (
    DEFAULT_COUNTRIES,
    DEFAULT_IMPACTS,
    EventFetchError,
    EventRateLimited,
    default_scope,
    fetch_events,
    store_events,
    upcoming,
)
from optjournal.flex import (
    KEYRING_SERVICE,
    FetchCooldown,
    StatementUnreadable,
    TokenMissing,
    TokenRejected,
    TokenWriteRefused,
    fetch,
    fetch_confirms,
    load,
    read_token,
    write_token,
)
from optjournal.history import build_history
from optjournal.ingest import (
    ASSET_FILTER_ALL,
    ingest_confirms,
    ingest_file,
)
from optjournal.jobs import (
    CONFIRM_COOLDOWN_S,
    record_manual_sync,
    record_run,
)
from optjournal.render import (
    render_friction,
    render_history,
    render_orders,
    render_positions,
    render_statements,
    render_summary,
    render_watchlist,
)
from optjournal.serialize import (
    broker_costs_data,
    costs_data,
    history_data,
    orders_data,
    positions_data,
    statements_data,
    summary_data,
    watchlist_data,
)
from optjournal.sync import sync_journal

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_NO_DATA = 3
#: IBKR asked us to back off. Distinct from EXIT_ERROR so the daily cron can
#: stay silent on throttling and only alert on a genuine failure.
EXIT_THROTTLED = 4
#: `serve` stopped so the launcher can start it again: an update or a journal
#: import is waiting (see `launcher/app.py`). 75 is sysexits' EX_TEMPFAIL.
EXIT_RESTART = 75

EPILOG = """\
examples:
  optjournal fetch 1591754                 download the configured Flex query
  optjournal fetch 1591754 --from 20250801 --to 20260731
  optjournal sync 1591754                  fetch + ingest + report what is new
  optjournal statements                    what is archived, and ingested
  optjournal ingest                        fold all archived statements into the DB
  optjournal ingest --assets OPT           options only (narrower than the default)
  optjournal orders                        option orders, partial fills collapsed
  optjournal positions                     current option book
  optjournal history                       closed-position P&L, round trip by round trip
  optjournal costs --json                  one statement's cost report as JSON
  optjournal friction                      what the broker cost, whole journal
  optjournal friction --assets OPT CASH    options and conversions only
  optjournal serve --query-id 1591754      local web UI with a Sync now button
  optjournal demo                          synthetic data in a scratch archive and DB
  optjournal serve --demo                  serve that synthetic data instead

path arguments default to the most recently archived statement.
exit codes: 0 ok, 1 error, 2 config, 3 no data, 4 throttled by IBKR.
"""


def _emit(data: Any, text: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str, sort_keys=True))
    else:
        print(text)


def _resolve_path(args) -> Path | None:
    """Explicit path, else the newest archived statement."""
    if getattr(args, "path", None):
        return args.path
    return newest_statement(args.archive)


def _asset_filter(raw: str) -> tuple[str, ...]:
    """Decode a `--assets` value into the tuple `ingest` and `serve` expect.

    "ALL" is a sentinel rather than a category, so it becomes
    `ASSET_FILTER_ALL`; anything else is a comma list of IBKR category codes,
    upper-cased because the codes are (`OPT`, `STK`) and a lower-case `opt`
    would silently match nothing.

    One decoder because three subcommands took the same option and each spelled
    the same four lines out -- the kind of copy where a fix to the parsing lands
    in one command and not the others.
    """
    return (
        ASSET_FILTER_ALL
        if raw.strip().upper() == "ALL"
        else tuple(a.strip().upper() for a in raw.split(",") if a.strip())
    )


# ------------------------------------------------------------------- commands


def cmd_fetch(args) -> int:
    result = fetch(
        args.query_id,
        archive_dir=args.archive,
        from_date=args.from_date,
        to_date=args.to_date,
        force=args.force,
    )
    data = summary_data(result.response, result.raw_path)
    data["raw_path"] = str(result.raw_path)
    data["raw_bytes"] = result.raw_bytes
    header = f"{result.raw_bytes:,} bytes -> {result.raw_path}"
    _emit(data, f"{header}\n{render_summary(data)}", args.json)
    return EXIT_OK


def cmd_statements(args) -> int:
    conn = connect(args.db) if args.db.exists() else None
    data = statements_data(args.archive, conn)
    # A daily cron adds roughly one distinct statement per trading day, so the
    # full list stops being readable within weeks. JSON is never truncated,
    # because a machine consumer wants the whole set.
    limit = 0 if (args.all or args.json) else args.limit
    _emit(data, render_statements(data, limit=limit), args.json)
    return EXIT_OK if data else EXIT_NO_DATA


def cmd_prune(args) -> int:
    """Collapse byte-identical archive duplicates. Dry run unless --apply."""
    # `ExitStack` rather than a bare `with`, because pruning an archive with no
    # journal beside it is a supported run: `prune_archive` takes None and skips
    # the provenance re-pointing. The stack closes the connection when there is
    # one and holds nothing when there is not, so both paths get the same
    # rollback-on-exit guarantee without a second spelling of the open.
    with ExitStack() as stack:
        conn = (
            stack.enter_context(open_journal(args.db)) if args.db.exists() else None
        )
        result = prune_archive(args.archive, conn, apply=args.apply)

    data = {
        "applied": result.applied,
        "files_removed": result.files_removed,
        "bytes_reclaimed": result.bytes_reclaimed,
        "statement_rows_removed": result.statement_rows_removed,
        "rows_repointed": result.rows_repointed,
        "groups": [
            {
                "digest": g.digest[:12],
                "keep": g.keep.name,
                "redundant": [p.name for p in g.redundant],
            }
            for g in result.groups
        ],
        "subsumed": [
            {"file": inner, "covered_by": outer} for inner, outer in result.subsumed
        ],
    }

    verb = "Removed" if result.applied else "Would remove"
    lines: list[str] = []
    if not result.groups:
        lines.append("No byte-identical duplicates in the archive.")
    else:
        lines.append(
            f"{verb} {result.files_removed} duplicate file(s), "
            f"{result.bytes_reclaimed:,} bytes"
        )
        for g in result.groups:
            lines.append(f"  keep {g.keep.name}  (sha {g.digest[:12]})")
            for path in g.redundant:
                lines.append(f"    drop {path.name}")
        if result.applied and result.rows_repointed:
            moved = ", ".join(
                f"{k}={v}" for k, v in sorted(result.rows_repointed.items())
            )
            lines.append(f"  provenance re-pointed to the retained copy: {moved}")
            lines.append(
                f"  statement rows removed: {result.statement_rows_removed}"
            )
        if not result.applied:
            lines.append("  dry run -- pass --apply to delete")

    if result.subsumed:
        lines.append("")
        lines.append(
            f"{len(result.subsumed)} file(s) have a period fully inside another "
            f"statement:"
        )
        for inner, outer in result.subsumed:
            lines.append(f"  {inner}  covered by  {outer}")
        lines.append(
            "  Not deleted. A wider date range does not prove a superset of the"
        )
        lines.append(
            "  data -- a template with fewer sections would cover more days with"
        )
        lines.append("  less content. Remove by hand if you are sure.")

    _emit(data, "\n".join(lines), args.json)
    return EXIT_OK


def _unreadable(exc: StatementUnreadable) -> int:
    """One line for a statement file that cannot be read, not a parser traceback."""
    print(f"Unreadable statement: {exc}", file=sys.stderr)
    return EXIT_ERROR


def cmd_show(args) -> int:
    path = _resolve_path(args)
    if path is None:
        return _no_statements(args)
    try:
        response = load(path)
    except StatementUnreadable as exc:
        return _unreadable(exc)
    data = summary_data(response, path)
    data["source_file"] = path.name
    _emit(data, render_summary(data), args.json)
    return EXIT_OK


def cmd_costs(args) -> int:
    path = _resolve_path(args)
    if path is None:
        return _no_statements(args)
    try:
        response = load(path)
    except StatementUnreadable as exc:
        return _unreadable(exc)
    reports = [analyse(s) for s in response.FlexStatements]
    _emit(
        [costs_data(r) for r in reports],
        "\n\n".join(format_report(r) for r in reports),
        args.json,
    )
    return EXIT_OK


def cmd_demo(args) -> int:
    """Generate a synthetic statement and ingest it into a scratch database.

    Kept away from `raw/` and `journal.db` by `assert_not_real`: the archive is
    the provenance root for every report and a statement costs an IBKR request
    to replace, so a fake one landing there would be indistinguishable from a
    real one afterwards.
    """
    from optjournal.demo import (
        QUERY_NAME,
        reset_demo_rows,
        write_demo_bars,
        write_demo_journal,
        write_demo_statement,
        write_demo_watchlist,
    )

    out, db = args.out, args.db
    path = write_demo_statement(out, db)
    with open_journal(db) as conn:
        # Replace rather than add to. Trades dedupe on identifiers the generator
        # derives deterministically, so a changed contract would arrive under an
        # existing trade_id and be ignored, leaving the database describing a
        # statement it was not built from.
        reset_demo_rows(conn)
        result = ingest_file(conn, path, reingest=True)
        # Computed from whatever UNDERLYING bars are already stored, so this is
        # a no-op on a fresh database and fills in once `bars` has run. Ordered
        # after the ingest because the contracts it prices come from it.
        option_bars = write_demo_bars(conn)
        # Watched rows, so the Watchlist tab has something to render in
        # `serve --demo` and in every sweep run. Additive and outside
        # `reset_demo_rows`: `watchlist` is the user-input table, so a re-run must
        # not be able to delete a symbol a reader added to their demo database.
        watched = write_demo_watchlist(conn)
        # The write-ups, for the same reason as the watchlist: without a
        # seeded entry the journal layer has no rendered state anywhere, so
        # the badge, the form and the adherence vocabulary are drawn by
        # nothing that runs. Two of nine cards, deliberately -- see
        # `DEMO_JOURNAL` on why the un-written state has to be on screen too.
        journalled = write_demo_journal(conn)

    payload = {
        "query_name": QUERY_NAME, "statement": str(path), "db": str(db),
        "trades": result.trades_inserted, "cash": result.cash_inserted,
        "positions": result.positions_written,
        "option_bars": option_bars,
        "watched": watched,
        "journalled": journalled,
    }
    lines = [
        f"wrote {path.name}  ({path.stat().st_size:,} bytes)",
        f"  {result.trades_inserted} fills, {result.cash_inserted} cash rows,"
        f" {result.positions_written} open positions,"
        f" {result.equity_summaries_written} NAV rows",
        f"  database: {db}",
    ]
    lines += [
        f"  {option_bars} synthetic option bar(s), priced off the stored"
        " underlying series"
        if option_bars else
        "  0 synthetic option bars: no underlying series stored yet. Run"
        f" `optjournal bars --db {db}` for the real NVDA/SPY history, then"
        " re-run this to price the options against it."
    ]
    lines += [
        f"  {journalled} write-up(s) seeded on the Trades tab"
        if journalled else
        "  0 write-ups seeded: the demo already holds them (a re-run never "
        "overwrites a note you wrote)",
    ]
    lines += [
        f"  {watched} watched symbol(s) added"
        if watched else
        "  watchlist already seeded (a re-run never removes a symbol you added)"
    ]
    lines += [
        "",
        "synthetic data -- closed round trips, a vertical spread, a roll, an",
        "expiry, an assignment, a 0DTE trade and a credited multi-fill order.",
        "",
        "  optjournal serve --demo --port 8792"
        + ("" if (out, db) == (DEFAULT_DEMO_DIR, DEFAULT_DEMO_DB)
           else f"  # or: --db {db} --archive {out}"),
    ]
    _emit(payload, "\n".join(lines), args.json)
    return EXIT_OK


def cmd_ingest(args) -> int:
    assets = _asset_filter(args.assets)
    paths = args.paths or sorted(args.archive.glob("activity-*.xml"))
    if not paths:
        return _no_statements(args)

    results = []
    # SKIPPED, not fatal: one unreadable file used to stop every run at that
    # file, so nothing archived after it was ever ingested. Named in the summary
    # and in the exit code, so a skip never reads as a clean run.
    unreadable: list[dict[str, str]] = []
    with open_journal(args.db) as conn:
        for p in paths:
            try:
                results.append(
                    ingest_file(conn, p, assets=assets, reingest=args.reingest))
            except StatementUnreadable as exc:
                unreadable.append({"file": Path(p).name, "reason": str(exc)})
        totals = {
            t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            for t in ("trades", "cash_transactions", "position_snapshots", "securities")
        }

    data = {
        "db": str(args.db),
        "assets": ",".join(assets) or "ALL",
        "files": [dataclasses.asdict(r) for r in results],
        "unreadable": unreadable,
        "totals": totals,
    }
    lines = [f"Ingesting {len(paths)} statement(s) -> {args.db}"
             f"  [assets={data['assets']}]"]
    for r in results:
        if r.already_ingested:
            lines.append(f"  {r.source_file}: unchanged, skipped")
            continue
        # Superseded rows are named only when there are any. Every statement
        # archived before Trade Confirmations existed reports zero, and a counter
        # that reads 0 on every line of every run teaches the eye to skip the line.
        settled = (f" settled {r.trades_superseded}," if r.trades_superseded else "")
        lines.append(
            f"  {r.source_file}: trades +{r.trades_inserted}"
            f" ({settled}dup {r.trades_skipped_existing},"
            f" filtered {r.trades_filtered_out})"
            f"  cash +{r.cash_inserted} (dup {r.cash_skipped_existing})"
            f"  positions {r.positions_written}"
            f"  securities {r.securities_written}"
        )
        for w in r.warnings:
            lines.append(f"    ! {w}")
    for u in unreadable:
        lines.append(f"  {u['file']}: UNREADABLE, skipped")
        lines.append(f"    ! {u['reason']}")
    lines.append("  totals: " + ", ".join(f"{k}={v}" for k, v in totals.items()))
    if unreadable:
        lines.append(f"  {len(unreadable)} file(s) could not be read; see above")
    _emit(data, "\n".join(lines), args.json)
    return EXIT_ERROR if unreadable else EXIT_OK


def cmd_orders(args) -> int:
    with open_journal(args.db) as conn:
        data = orders_data(conn)
    _emit(data, render_orders(data), args.json)
    return EXIT_OK if data else EXIT_NO_DATA


def cmd_positions(args) -> int:
    with open_journal(args.db) as conn:
        data = positions_data(conn)
    _emit(data, render_positions(data), args.json)
    return EXIT_OK if data else EXIT_NO_DATA


def cmd_history(args) -> int:
    scope = None if args.assets.strip().upper() == "ALL" else args.assets.strip().upper()
    with open_journal(args.db) as conn:
        report = build_history(conn, asset_category=scope)
    data = history_data(report)
    _emit(data, render_history(data), args.json)
    return EXIT_OK if report.episodes else EXIT_NO_DATA


def _month_or_year(text: str) -> str:
    """`friction --month`: `2026-08` or `2026`, refused at the parser otherwise.

    The period is a prefix match on stored ISO dates, so anything else matched
    nothing and read as "no data" (exit 3) instead of a typo.
    """
    if re.fullmatch(r"\d{4}(-(0[1-9]|1[0-2]))?", text):
        return text
    raise argparse.ArgumentTypeError(
        f"{text!r} is not a month like 2026-08 or a year like 2026")


def cmd_friction(args) -> int:
    """What the broker cost, from the DATABASE rather than one statement.

    The sibling of `costs`, and the difference is the question each answers.
    `costs` reports one statement's own costs -- the right thing when the question
    is about a statement, and the only way to read a section no column carries.
    This reports the JOURNAL's: every ingested fill, narrowable to a set of asset
    categories, which is what the web page shows.

    Emits JSON through the same serializer the page reads, so `--json` here and
    the tab cannot disagree about a figure.
    """
    scope = CostScope.of(args.assets)
    with open_journal(args.db) as conn:
        report = build_costs(conn, scope=scope, period=args.month)
    data = broker_costs_data(report)
    _emit(data, render_friction(data), args.json)
    return EXIT_OK if report.fills or report.unattributable.base else EXIT_NO_DATA


def cmd_watch(args) -> int:
    """Manage and show the watchlist.

    `add`/`rm` mutate, a bare `watch` shows. Subcommand-free on purpose: three
    sibling commands for one three-row table would be more surface than the
    feature has, and `watch AAPL` reading as "add AAPL" is the shape a reader
    already expects from `git branch`.

    Every MEASURED figure here comes from bars this journal already stores, so this
    spends no request: the price, realised vol, its rank inside the symbol's own
    trailing year, and B-Xtrender over both daily closes and ISO weeks. A figure
    whose window is not yet full shows a dash rather than a zero, and the footnotes
    name the count each dash is waiting on -- `optjournal bars` is what fills them
    in.

    TWO FIELDS ARE TYPED, and they take the same key-present semantics as the
    endpoint (`web._watchlist_write`), for the same reason: a flag not passed leaves
    the stored value alone, while `--clear-note` and `--earnings ''` write NULL.
    Absence and emptiness are different requests -- collapsing them is how a note
    became impossible to clear -- and on a CLI the flag's presence is what says
    which one this is. `--note` and `--clear-note` are mutually exclusive at the
    parser, since a command carrying both is asking for two things at once.

    A malformed `--earnings` is refused rather than stored: a format check only,
    through the same `clock.parse_day` the endpoint uses, so the two surfaces cannot
    disagree about what a date is.
    """
    # Stripped before anything looks at it, so the two surfaces agree: a value
    # emptied to spaces clears rather than storing whitespace the reader can neither
    # see nor delete, and `--earnings '  2026-08-27 '` is the date it looks like.
    earnings = None if args.earnings is None else args.earnings.strip()
    if earnings and parse_day(earnings) is None:
        print(
            f"\n--earnings wants a YYYY-MM-DD day; {args.earnings!r} is not one."
            f" (An earnings date is typed, so it is checked for spelling rather"
            f" than against a calendar this journal does not have.)",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    # Which fields this invocation is writing, decided before the loop so the SQL
    # is built once. A flag absent from the command line is absent from here, which
    # is what leaves the stored value alone.
    fields: dict[str, str | None] = {}
    if args.clear_note:
        fields["note"] = None
    elif args.note is not None:
        fields["note"] = args.note.strip() or None
    if earnings is not None:
        fields["earnings_on"] = earnings or None
    if fields and not args.add:
        print(
            "\nNothing to write to: name the symbol as well, e.g."
            " `optjournal watch NVDA --earnings 2026-08-27`.",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    with open_journal(args.db) as conn:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        # Only the fields this invocation named are assigned on conflict, so a bare
        # re-add is a no-op rather than a blanking -- the endpoint's rule, spelled
        # the same way, because both write the same row.
        updates = ", ".join(f"{column}=excluded.{column}" for column in fields)
        for symbol in (args.add or []):
            conn.execute(
                "INSERT INTO watchlist (symbol, note, earnings_on, added_at)"
                " VALUES (:symbol, :note, :earnings_on, :added_at)"
                " ON CONFLICT(symbol) DO "
                + (f"UPDATE SET {updates}" if updates else "NOTHING"),
                {"symbol": symbol.upper(), "added_at": now,
                 "note": fields.get("note"),
                 "earnings_on": fields.get("earnings_on")},
            )
        for symbol in (args.rm or []):
            conn.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol.upper(),))
        if args.add or args.rm:
            conn.commit()

        rows = watchlist_data(conn)
    _emit(rows, render_watchlist(rows), args.json)
    return EXIT_OK


def cmd_market(args) -> int:
    """The economic calendar: what is coming, and what the journal already holds.

    Reading and fetching are separate flags rather than one command that always
    fetches, because they answer different questions and only one touches the
    network. `--fetch` is what the nightly cron runs; a bare `market` is what a
    reader runs, and it works offline.

    Defaults to `events.DEFAULT_COUNTRIES` / `DEFAULT_IMPACTS` -- USD plus the
    feed's global rows, High and Medium -- which is the slice that moves an
    options book, measured against a real week. Named from those constants rather
    than spelled out here, so this and the web view cannot describe different
    filters. `--all` is there because the table holds ten countries and the
    default should narrow the VIEW, never the STORE (the same rule ingest learned
    the hard way).
    """
    with open_journal(args.db) as conn:
        result: dict[str, object] = {}
        lines: list[str] = []

        if args.fetch:
            try:
                fetched_events = fetch_events()
            except EventRateLimited as exc:
                # EXIT_THROTTLED, not EXIT_ERROR: the same distinction the IBKR
                # path draws, so a nightly cron stays silent on a back-off and
                # alerts only on something that actually changed.
                print(f"calendar: {exc}", file=sys.stderr)
                # `nothing`, not `failed`: the feed pushed back, nothing was lost,
                # and the same week is served later. A ledger that called this a
                # failure would accumulate consecutive_failures for a working
                # system.
                record_run(conn, "market", status="nothing",
                           detail=f"rate limited: {exc}")
                return EXIT_THROTTLED
            except EventFetchError as exc:
                print(f"calendar fetch failed: {exc}", file=sys.stderr)
                record_run(conn, "market", status="failed", detail=str(exc)[:400])
                return EXIT_ERROR
            stored = store_events(conn, fetched_events)
            result["fetched"] = len(fetched_events)
            result["stored"] = stored
            lines.append(
                f"calendar {len(fetched_events)} event(s) -> {stored} stored"
            )
            record_run(conn, "market", status="ok" if stored else "nothing",
                       detail=f"{len(fetched_events)} fetched, {stored} stored",
                       done=stored, total=len(fetched_events))

        now = datetime.now(UTC)
        start = int(now.timestamp())
        end = int((now + timedelta(days=args.days)).timestamp())
        countries = () if args.all_events else DEFAULT_COUNTRIES
        impacts = () if args.all_events else DEFAULT_IMPACTS
        events = upcoming(conn, start=start, end=end,
                          countries=countries, impacts=impacts)
        result["events"] = events

    # From the shared helper rather than spelled out, because this line said
    # "USD high-impact" while DEFAULT_IMPACTS held two grades -- a label that
    # narrates a filter it no longer applies is worse than no label.
    scope = "all" if args.all_events else default_scope()
    lines.append(f"\nNext {args.days} day(s), {scope}: {len(events)} event(s)")
    if not events:
        lines.append("  (none stored -- run `optjournal market --fetch`)")
    for event in events:
        when = datetime.fromtimestamp(event["starts_at"], MARKET_TZ)
        # The feed's judgement, attributed. Same rule as the AutoFX markup: an
        # estimate presented as ours would read as a measurement.
        figures = " ".join(
            f"{label} {event[key]}"
            for label, key in (("fc", "forecast"), ("prev", "previous"))
            if event[key]
        )
        lines.append(
            f"  {when:%a %b %-d %H:%M} {event['country']:<4}"
            f" {event['impact']:<7} {event['title']}"
            + (f"   [{figures}]" if figures else "")
        )
    if events:
        lines.append("\n  impact is the feed's assessment, not this journal's")

    _emit(result, "\n".join(lines), args.json)
    return EXIT_OK


def cmd_bars(args) -> int:
    """Fetch the price bars this journal's own positions imply.

    Idempotent by construction, so running it again is cheap: bars for a closed
    session never change, and the upsert makes a repeat a no-op. `--dry-run`
    prints the derived windows without spending a request, which is the way to
    see what a run would ask for before it asks.

    `--live` narrows the run to what cannot be collected later -- the intraday
    bars of a still-open option, which the source serves only while the session
    is running. That is the market-hours poll; a full run is for everything else.

    `--audit` fetches nothing and asks the opposite question: did the last
    session's perishable bars actually land? The live poll swallows a failed
    fetch on purpose, so this is the only thing that notices a session where
    every poll failed. Exit 3 means there was nothing to check.
    """
    live = getattr(args, "live", False)

    def day(epoch: int) -> str:
        return datetime.fromtimestamp(epoch, UTC).date().isoformat()

    with open_journal(args.db) as conn:
        if getattr(args, "audit", False):
            result = audit_perishable(conn)
            data = dataclasses.asdict(result) | {"ok": result.ok}
            if not result.market_traded:
                lines = [f"{result.day}: the market did not trade, nothing to audit"]
            elif not result.covered and not result.missing:
                lines = [
                    f"{result.day}: no contract was eligible for hourly collection"
                ]
            elif result.missing:
                lines = [
                    f"{result.day}: NO hourly option bars for "
                    f"{len(result.missing)} of "
                    f"{len(result.covered) + len(result.missing)}"
                    " eligible contract(s) -- that session is unrecoverable"
                ]
                lines += [f"  MISSING: {symbol}" for symbol in result.missing]
                lines += [f"  ok:      {symbol}" for symbol in result.covered]
            else:
                lines = [
                    f"{result.day}: hourly option bars present for all "
                    f"{len(result.covered)} eligible contract(s)"
                ]
            _emit(data, "\n".join(lines), args.json)
            if not result.market_traded or not (result.covered or result.missing):
                return EXIT_NO_DATA
            return EXIT_ERROR if result.missing else EXIT_OK

        if args.dry_run:
            requests = bars_manifest(conn, perishable_only=live)
            dry_run_data = [dataclasses.asdict(r) for r in requests]
            lines = [f"{len(requests)} window(s) derived, nothing fetched"]
            lines += [
                f"  {r.kind:<10} {r.symbol:<20} {r.bar_size}  "
                f"{day(r.start)} -> {day(r.end)}"
                + ("  live-only" if r.perishable else "")
                for r in requests
            ]
            _emit(dry_run_data, "\n".join(lines), args.json)
            return EXIT_OK if requests else EXIT_NO_DATA

        outcome = backfill_bars(conn, perishable_only=live)
        data = dataclasses.asdict(outcome)
        lines = [
            f"{outcome.written} bar(s) stored across {outcome.requested} window(s)"
            + (f", {outcome.skipped} with no bars at that granularity"
               if outcome.skipped else "")
        ]
        lines += [f"  FAILED: {failure}" for failure in outcome.failures]
        _emit(data, "\n".join(lines), args.json)
        # Recorded HERE rather than in the cron, because a cron runs under
        # MeshClaw's interpreter and cannot import this package at all --
        # `import py_ibkr` there is a ModuleNotFoundError, which is why the crons
        # shell out in the first place. See jobs.py.
        #
        # Three statuses where the old ledger had one: `failed` when a window
        # failed, `nothing` when the run was legitimately empty, `ok` when bars
        # landed. That distinction is the whole point -- crons.json read `ok` for
        # two days while this command wrote no bars at all.
        record_run(
            conn, "bars_live" if live else "bars_daily",
            status=("failed" if outcome.failures
                    else "ok" if outcome.written else "nothing"),
            detail=("; ".join(outcome.failures)[:400] if outcome.failures
                    else f"{outcome.written} bar(s), {outcome.skipped} empty"),
            done=outcome.written, total=outcome.requested,
        )
    if outcome.failures:
        return EXIT_ERROR
    return EXIT_OK if outcome.written else EXIT_NO_DATA


def cmd_sweep(args) -> int:
    """Render every page both journals can show and assert what each must hold.

    Deliberately not part of `pytest`: it launches a browser once per page, so
    it costs a minute or two where the suite costs ten seconds. The checks
    themselves ARE in the suite -- `tests/test_sweep.py` feeds each one a
    broken fragment and asserts it fails -- so what runs here is trusted
    machinery over real data rather than unvalidated assertions.

    Both journals by default, because they cover different ground: the real one
    is the only source of true rates and mixed currencies, and the demo is the
    only one holding closed round trips, rolls, spreads and a commission
    credit.
    """
    from optjournal import sweep as sweep_mod

    if not browser.browsers():
        print(
            "\nNo Chrome/Chromium found. The sweep needs a browser engine to"
            " render the page; `pytest` covers everything that does not.",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    journals: list[tuple[str, Path, Path, str | None]] = []
    if not args.demo_only:
        journals.append(("real", args.db or DEFAULT_DB,
                         args.archive or DEFAULT_ARCHIVE, args.query_id))
    if not args.real_only:
        journals.append(("demo", DEFAULT_DEMO_DB, DEFAULT_DEMO_DIR, None))

    results = []
    with tempfile.TemporaryDirectory(prefix="optj-sweep-") as tmp:
        for name, db, archive_dir, query_id in journals:
            if not Path(db).exists():
                print(f"skipping {name}: no journal at {db}", file=sys.stderr)
                continue
            results.append(sweep_mod.sweep_journal(
                name=name, db_path=Path(db), archive_dir=Path(archive_dir),
                profile=Path(tmp) / f"profile-{name}", query_id=query_id,
            ))

    if not results:
        return _no_statements(args) if hasattr(args, "archive") else EXIT_NO_DATA

    payload = [
        {"journal": journal, "page": label,
         "checks": {n: {"status": v.status, "detail": v.detail} for n, v in checks}}
        for r in results for journal, label, checks in r.pages
    ]
    _emit(payload, sweep_mod.format_report(results), args.json)
    return EXIT_ERROR if any(r.failures for r in results) else EXIT_OK


def cmd_mutate(args) -> int:
    """Inject known defects and report which tests notice each.

    Deliberately not part of `pytest`, for the same reason as `sweep`: it clones
    the repo and runs the whole suite once per mutant, so it costs minutes where
    the suite costs seconds.

    What it answers is not "what is covered" but "what would a real bug cost". A
    defect caught by nothing is an unguarded invariant -- that is how a 0.4-share
    residual booking a partial close as a closed round trip was found, having
    passed 579 tests. A defect caught by fifteen tests means fourteen are coupled
    to something they are not about.

    Judge equivalence before believing an uncaught result: some changes have no
    observable effect, and for those "no test caught it" says nothing.
    """
    from optjournal import mutate

    outcomes = mutate.run_all(
        source=ROOT, workdir=args.workdir, only=tuple(args.only or ()),
        jobs=args.jobs,
    )
    data = [
        {"defect": o.mutant.key, "module": o.mutant.module,
         "breaks": o.mutant.breaks, "status": o.status,
         "failed": o.failed, "tests": list(o.tests), "detail": o.detail}
        for o in outcomes
    ]
    _emit(data, mutate.format_report(outcomes), args.json)
    # A mutant nothing caught, or a measurement that could not be trusted, is
    # what a human needs to look at. Exit 1 so a scripted run can say so.
    if any(o.status != "measured" or o.failed == 0 for o in outcomes):
        return EXIT_ERROR
    return EXIT_OK


def cmd_serve(args) -> int:
    """Run the local web UI. Blocks until interrupted."""
    from optjournal.web import serve

    assets = _asset_filter(args.assets)
    # --db and --archive default to None on this subcommand, so an explicit path
    # always wins over --demo rather than being silently redirected.
    db = args.db or (DEFAULT_DEMO_DB if args.demo else DEFAULT_DB)
    archive_dir = args.archive or (DEFAULT_DEMO_DIR if args.demo else DEFAULT_ARCHIVE)
    if args.demo and args.query_id:
        # Sync writes the fetched statement into the served archive and ingests
        # it into the served database. Pointed at the demo pair, one click would
        # spend an IBKR request to put real trades in the same tables as
        # synthetic ones -- after which no figure in the journal means anything,
        # and the archive holds a real statement in a gitignored directory.
        raise ValueError(
            "--demo cannot be combined with --query-id: a sync would fetch real"
            " trades into the synthetic database. Serve the demo without a query"
            " id, or serve the real journal without --demo."
        )
    # THE SAME FALLBACK `sync` HAS, and its absence here was a real outage rather
    # than an inconsistency: `serve` now HOLDS THE SCHEDULER, so a query id that
    # only the CLI could see meant the supervised process ran `jobs._sync` with
    # `query_id=None` on every due tick, and the ledger filled with `failed -- no
    # Flex query id configured` while `optjournal sync` in a shell worked fine.
    # The query id is an identifier, not the secret -- the TOKEN is in the OS
    # keyring (`flex.read_token`) and `cron/optjournal_sync.py:104` has carried the
    # id in the repo all along. So the environment is a channel `serve` can share
    # with the cron rather than a place to hide something.
    #
    # READ AFTER the `--demo` check, deliberately: the guard above is about an
    # EXPLICIT flag, so a developer who exports the variable and then serves the
    # demo gets the demo, not a refusal and not a real fetch into synthetic tables.
    # `settings.query_id` holds the precedence (argument, environment, stored
    # file) so `serve`, `sync` and the cron cannot each carry their own version
    # of it -- and the STORED step is the one a launchd agent can actually see,
    # which the environment channel above never was.
    #
    # ONLY THE OVERRIDE is handed over, never the stored step. Resolving the whole
    # precedence here froze the stored id into the server and the scheduler for
    # the life of the process: an id saved in Settings later never reached the Run
    # button or the scheduled sync, and the page called the startup id an
    # override. The server and each job run read the stored step themselves.
    query_id = None if args.demo else settings.query_id_override(args.query_id)
    # A ROTATING LOG, FOR SERVE ONLY. This is the long-lived process -- the one
    # whose reconciler logs every tick -- and macOS rotates nothing for a launchd
    # agent's stdout, so a supervised `serve` would otherwise append to one file
    # forever. A one-shot `optjournal bars` needs no such thing and should not
    # leave a file behind.
    logs.configure(DATA_HOME)

    try:
        restart = serve(
            db_path=db,
            archive_dir=archive_dir,
            query_id=query_id,
            assets=assets,
            host=args.host,
            port=args.port,
            # OFF for the demo, unconditionally and regardless of the flag: the
            # demo journal must never fetch anything, and a scheduler pointed at a
            # synthetic archive would spend a real IBKR request to fill it.
            scheduler=bool(args.scheduler) and not args.demo,
            # And told, because the server resolves the stored query id per
            # request: `query_id=None` above did not stop a Sync click from
            # fetching the real statement into the demo.
            demo=bool(args.demo),
        )
    except ValueError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return EXIT_CONFIG
    except OSError as exc:
        print(f"\nCould not bind {args.host}:{args.port}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_RESTART if restart else EXIT_OK


def cmd_prepare(_args) -> int:
    """Bring the journal home before the server starts. The launcher runs this.

    Reports and never fails: a journal that could not be moved still opens from
    where it is, and the launcher starts the server either way.
    """
    for line in install.prepare():
        print(line)
    return EXIT_OK


def _git(*argv: str) -> tuple[int, str]:
    """Run one git command in the journal's own directory.

    `ROOT`, not the caller's cwd: `optjournal update` is meant to work from
    anywhere, and a `git pull` that silently updated whichever repository the
    shell happened to be sitting in would be a genuinely bad surprise.
    """
    import subprocess

    proc = subprocess.run(
        ["git", *argv], cwd=ROOT, capture_output=True, text=True, check=False,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _find_uv() -> str | None:
    """The `uv` that installs this app's dependencies, or None if there is none.

    `$UV` first, which `uv run` exports, so `uv run optjournal update` finds the
    uv that started it. Then PATH. Then where uv's installer puts it, which a
    shell that has not re-read its profile since the install (and a
    double-clicked Start file) may not have on PATH.
    """
    import os
    import shutil

    exported = os.environ.get("UV")
    if exported and Path(exported).is_file():
        return exported
    found = shutil.which("uv")
    if found:
        return found
    name = "uv.exe" if sys.platform == "win32" else "uv"
    for folder in (Path.home() / ".local" / "bin", Path.home() / ".cargo" / "bin"):
        if (folder / name).is_file():
            return str(folder / name)
    return None


#: Run by `update` in a NEW process once the pull is done. This process loaded
#: the old `db.py` before pulling, so migrating in-process ran the old code's
#: migrations and reported the old schema version.
_MIGRATE = (
    "import sys; from pathlib import Path; from optjournal.db import connect, migrate; "
    "c = connect(Path(sys.argv[1])); migrate(c); "
    "print(c.execute('SELECT MAX(version) FROM schema_version').fetchone()[0]); c.close()"
)


def cmd_update(args) -> int:
    """Fast-forward this journal to the latest published commit.

    THE UPDATE MECHANISM FOR A GIT INSTALL, which is what this is: the code is a
    clone, so `git pull` is the delivery channel and there is no second one to
    build. What this adds over typing `git pull` is the three things that have to
    happen with it -- dependencies resolved, schema migrated, and a refusal when
    the tree is not in a state where a fast-forward is safe.

    Refuses rather than merges, always. `--ff-only` is the whole safety model: a
    friend running this has no local commits to preserve, so anything that is not
    a fast-forward means their clone has diverged in a way a tool should not
    guess about. Uncommitted edits to tracked files are refused for the same
    reason: a pull that stashed someone's edits without being asked is a worse
    outcome than stopping. Untracked files are not edits (Finder writes
    `.DS_Store` into every folder it opens), and git itself refuses a pull that
    would overwrite one.

    Neither uv call rewrites `uv.lock`: a rewritten lock is a modified tracked
    file, which would then block every later update. The sync is `--locked`, so
    a release whose lock does not match its pyproject (a publishing mistake) is
    refused by name before anything is migrated, rather than installing the old
    lock and failing later on a missing module. The migration then runs
    `--frozen` against the lock the sync has just checked.

    The migration runs in a NEW process, after the pull and `uv sync`, so it is
    the new code's (see `_MIGRATE`). A failing one is reported here, rather than
    by the next `serve` at a moment nobody is watching.
    """
    if args.json:
        print("`update` reports its progress as it goes, so it has no --json output.",
              file=sys.stderr)
        return EXIT_CONFIG

    code, remote = _git("remote")
    if code != 0 or not remote:
        print(
            "No git remote, so there is nothing to update from. This command is "
            "for a clone installed from a repository; see the README.",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    # Local, so a clone that cannot be updated is told so without a network call.
    code, _ = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if code != 0:
        print("Nothing to update from: this clone is not on a branch that tracks a "
              "remote (a detached HEAD, or a branch of your own). Switch back to "
              "the branch you cloned, usually with `git switch main`.",
              file=sys.stderr)
        return EXIT_CONFIG

    code, dirty = _git("status", "--porcelain", "--untracked-files=no")
    if code == 0 and dirty:
        print("Refusing to update: this working tree has uncommitted changes.\n"
              f"{dirty}\n\nCommit or discard them first.", file=sys.stderr)
        return EXIT_CONFIG

    code, out = _git("fetch", "--quiet")
    if code != 0:
        print(f"Could not reach the remote: {out}", file=sys.stderr)
        return EXIT_ERROR

    _, head = _git("rev-parse", "--short=9", "HEAD")
    _, counts = _git("rev-list", "--left-right", "--count", "HEAD...@{u}")
    ahead, behind = (int(n) for n in counts.split())
    if not behind:
        extra = (f", with {ahead} local commit(s) the remote does not have"
                 if ahead else "")
        print(f"Already up to date ({head}){extra}.")
        return EXIT_OK
    if ahead:
        print(f"Refusing to update: this clone and the remote have diverged "
              f"({ahead} local commit(s), {behind} new on the remote), so a "
              "fast-forward is not possible. Sort that out by hand: a tool "
              "guessing here would be guessing about your work.", file=sys.stderr)
        return EXIT_ERROR

    _, log = _git("log", "--oneline", "HEAD..@{u}")
    print(f"{behind} new commit(s):")
    print(log)
    if args.check:
        print("\n--check, so nothing was changed. Run `optjournal update` to apply.")
        return EXIT_OK

    # Before the pull: new code on the old dependencies is an install that may
    # not start, and without uv this command could not finish what it began.
    uv = _find_uv()
    if uv is None:
        print("Refusing to update: uv was not found ($UV, PATH, ~/.local/bin), "
              "so the new code's dependencies could not be installed. Run this "
              "as `uv run optjournal update`.", file=sys.stderr)
        return EXIT_CONFIG

    code, out = _git("pull", "--ff-only", "--quiet")
    if code != 0:
        print(f"\nFast-forward refused, so nothing changed: {out}", file=sys.stderr)
        return EXIT_ERROR

    # Dependencies BEFORE the schema: a migration added in the new commits may
    # import something the old lockfile does not have, and `uv sync` failing
    # after a partial migration is the one ordering that leaves a journal in a
    # state neither commit describes.
    import subprocess

    print("\nResolving dependencies...")
    synced = subprocess.run([uv, "sync", "--locked", "--quiet"], cwd=ROOT, check=False)
    if synced.returncode != 0:
        print("`uv sync` failed, so the journal was not migrated. The code is "
              "updated but its dependencies are not. If uv says the lockfile needs "
              "to be updated, the release was published with a stale uv.lock: wait "
              "for a fixed release and run `optjournal update` again.",
              file=sys.stderr)
        return EXIT_ERROR

    db = args.db or DEFAULT_DB
    if db.exists():
        migrated = subprocess.run(
            [uv, "run", "--frozen", "--quiet", "python", "-c", _MIGRATE, str(db)],
            cwd=ROOT, capture_output=True, text=True, check=False)
        if migrated.returncode != 0:
            print(f"\nThe code is updated, but migrating the journal failed, and "
                  f"optjournal will not open it until that is fixed:\n"
                  f"{migrated.stderr.strip()}", file=sys.stderr)
            return EXIT_ERROR
        print(f"Schema at version {migrated.stdout.strip()}.")

    _, now = _git("rev-parse", "--short=9", "HEAD")
    print(f"\nUpdated to {now}. Restart `optjournal serve` to pick it up: "
          "the server re-reads the page on every request but loads its Python "
          "once, at startup.")
    return EXIT_OK


def _prompt_token(existing: bool) -> str | None:
    """Ask for the Flex token without echoing it, or None to keep what is there.

    `getpass`, not `input`: a Flex token is a bearer credential for a brokerage
    account, and echoing it puts it in the scrollback of a terminal that may be
    shared or screen-shared. It is also why nothing here prints the value back.
    """
    import getpass

    hint = " (blank keeps the stored one)" if existing else ""
    while True:
        entered = getpass.getpass(f"IBKR Flex token{hint}: ").strip()
        if entered:
            return entered
        if existing:
            return None
        print("  A token is required. Client Portal → Settings → Flex Web Service.")


def cmd_setup(args) -> int:
    """Store the Flex token and query id, so a fresh install works after this.

    The one command a new journal needs, and it exists because the setup it
    replaces was a macOS-specific `security add-generic-password` invocation
    copied out of a README plus an id remembered in a shell. Neither survives
    handing this journal to somebody else, which is the case this is for.

    The token goes to the OS keyring through `keyring` rather than the `security`
    binary: same store on macOS, but it also works on Linux and Windows, and it
    cannot leave the secret in shell history the way a `-w '<token>'` argument
    does.

    Non-interactive by flag as well as interactive by prompt (`--token-stdin`,
    `--query-id`, `--no-verify`), because the first thing anyone automating an
    install needs is a way to run this without a TTY.
    """
    import getpass as _getpass

    import keyring

    account = _getpass.getuser()
    stored_token = keyring.get_password(KEYRING_SERVICE, account)
    stored_qid = settings.read().get("query_id")

    if args.token_stdin:
        token = sys.stdin.read().strip() or None
        if not token and not stored_token:
            print("No token on stdin, and none stored.", file=sys.stderr)
            return EXIT_CONFIG
    elif args.query_id and not sys.stdin.isatty():
        # A query id was given with no TTY to prompt on: configure what we can
        # and leave the token alone rather than blocking on input nobody can
        # provide. Silently prompting into a dead stdin is how a scripted
        # install hangs forever.
        token = None
    else:
        token = _prompt_token(bool(stored_token))

    query_id = args.query_id
    if not query_id and sys.stdin.isatty():
        shown = f" [{stored_qid}]" if stored_qid else ""
        query_id = input(f"Flex Query ID{shown}: ").strip() or None
    # The query id is SAVED FIRST. A settings file that cannot be written (a
    # folder that could not be made, a read-only home) then stops the run
    # before the token is stored, rather than after, which would leave a
    # journal holding a token and no query id while this run exits as failed.
    if query_id:
        try:
            settings.update(query_id=query_id)
        except OSError as exc:
            print(f"Could not save the query id to {settings.path_for()}: {exc}\n"
                  "Nothing was stored. Fix that and run `optjournal setup` again.",
                  file=sys.stderr)
            return EXIT_ERROR

    if token:
        # Through `flex.write_token`, not `keyring` directly: the settings page
        # writes the same entry, and two callers spelling the service name for
        # themselves is how one of them ends up storing a token the other cannot
        # find. It also strips the newline a pasted token arrives with.
        write_token(token, account)

    effective_qid = settings.query_id(query_id)
    have_token = bool(token or stored_token)
    print()
    print(f"token      {'stored in the OS keyring' if have_token else 'MISSING'}"
          f" (service {KEYRING_SERVICE}, account {account})")
    print(f"query id   {effective_qid or 'MISSING'}")
    print(f"settings   {settings.path_for()}")
    print(f"database   {DEFAULT_DB}")
    print(f"archive    {DEFAULT_ARCHIVE}")

    if not have_token or not effective_qid:
        print("\nIncomplete: run `optjournal setup` again.", file=sys.stderr)
        return EXIT_CONFIG

    # VERIFIED BY USE, not by inspecting the values. A token that is the right
    # shape and a query id that is a plausible number still fail together at
    # IBKR, and finding that out on the first real sync -- possibly from a cron,
    # days later -- is the failure this avoids. It costs one request against the
    # lockout budget, which is why it can be turned off.
    if args.verify:
        print("\nVerifying against IBKR (one request)...")
        try:
            read_token(account)
            result = fetch(effective_qid, archive_dir=DEFAULT_ARCHIVE)
        except FetchCooldown as exc:
            print(f"  skipped: {exc}")
        else:
            print(f"  ok: {result.raw_bytes:,} bytes -> {result.raw_path.name}")
            print("\nNext:  optjournal ingest && optjournal serve")
            return EXIT_OK
    print("\nNext:  optjournal sync && optjournal serve")
    return EXIT_OK


def cmd_confirms(args) -> int:
    """Fetch today's Trade Confirmations and fold them in. Same-session fills.

    The intraday counterpart to `sync`: the Activity Statement is T+1, so a fill
    made this morning reaches the journal tomorrow, where a confirm reaches it
    within minutes. Both write the same `trades` rows and `ingest.SOURCE_RANK`
    decides which wins, so running this never costs you settled figures.

    A separate command rather than a flag on `sync`, because the two are different
    queries on different cadences with different budgets -- and because a cron that
    wants one must not be able to accidentally get the other.
    """
    query_id = settings.confirm_query_id(args.query_id)
    if not query_id:
        print(
            "No Trade Confirmation query ID. Create the query in Client Portal "
            "(Performance & Reports -> Flex Queries -> the + under Trade "
            "Confirmation Flex Query Templates), then set it in the page under "
            "Settings, pass it as an argument, or export "
            "$OPTJOURNAL_CONFIRM_QUERY_ID.",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    with open_journal(args.db) as conn:
        base = _journal_base_currency(conn)
        if not base:
            print(
                "No ingested statement to read the base currency from. Run "
                "`optjournal sync` first: a confirm carries no base-currency "
                "conversion, so the journal has to know what it is converting to.",
                file=sys.stderr,
            )
            return EXIT_CONFIG
        result = fetch_confirms(
            query_id, archive_dir=args.archive,
            from_date=args.from_date, to_date=args.to_date, force=args.force,
            # `force` already skips the cooldown check; zeroing the window as well
            # said the same thing twice, and two spellings of one intent are how
            # they eventually disagree.
            cooldown_s=CONFIRM_COOLDOWN_S,
        )
        ingested = ingest_confirms(
            conn, result.raw_path, base_currency=base,
            assets=_asset_filter(args.assets),
        )

    data = {
        "query_id": query_id,
        "archive": result.raw_path.name,
        "raw_bytes": result.raw_bytes,
        "reused_archive": result.is_duplicate,
        "new_trades": ingested.trades_inserted,
        "updated_trades": ingested.trades_superseded,
        "already_known": ingested.trades_skipped_existing,
        "warnings": ingested.warnings,
    }
    lines = [
        f"confirms {query_id}  {result.raw_bytes:,} bytes -> {result.raw_path.name}",
        f"  {ingested.trades_inserted} new, {ingested.trades_superseded} updated, "
        f"{ingested.trades_skipped_existing} already known",
    ]
    # Every same-session fill is PROVISIONAL, and saying so once here is cheaper
    # than a reader discovering tomorrow that a figure moved.
    if ingested.trades_inserted:
        lines.append("  same-session fills: base-currency figures are estimated at "
                     "a live FX rate until the Activity Statement lands")
    lines.extend(f"  ! {w}" for w in ingested.warnings)
    _emit(data, "\n".join(lines), args.json)
    return EXIT_OK


def _journal_base_currency(conn) -> str | None:
    """The account's base currency, from the newest ingested statement."""
    row = conn.execute(
        "SELECT base_currency FROM statements WHERE base_currency IS NOT NULL"
        " ORDER BY ingested_at DESC LIMIT 1"
    ).fetchone()
    return str(row["base_currency"]) if row and row["base_currency"] else None


def cmd_sync(args) -> int:
    """Fetch the latest statement, fold it in, and report only what is new.

    This is the command the daily cron runs, so it is built to be quiet when
    nothing changed and to distinguish IBKR throttling from a real failure --
    a cron that cannot tell those apart either spams or hides outages.
    """
    query_id = settings.query_id(args.query_id)
    if not query_id:
        print(
            "No Flex query ID. Run `optjournal setup`, pass it as an argument, "
            "or set OPTJOURNAL_QUERY_ID.",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    # ONE SYNC PATH, shared with `POST /api/sync` and the `sync` job. This used to
    # be a second implementation of the same sequence, and the two had already
    # drifted: `new_trades` held the row LIST here and a COUNT in web.py -- one
    # name, two types, computed from the same table. Nothing broke only because
    # each consumer had met just one producer.
    with open_journal(args.db) as conn:
        try:
            data = sync_journal(
                conn=conn,
                archive_dir=args.archive,
                query_id=query_id,
                assets=_asset_filter(args.assets),
                from_date=args.from_date,
                to_date=args.to_date,
                force=args.force,
            )
        except (FetchCooldown, TokenMissing, TokenRejected) as exc:
            # RECORDED BEFORE RE-RAISING, so `main`'s handlers still decide the exit
            # code and the message. A hand-run sync used to be invisible to the
            # ledger, which is how a backed-off job stayed backed off while the
            # command line was syncing perfectly.
            record_manual_sync(conn, exc)
            raise
        record_manual_sync(conn, data)
    new_trade_rows = data["new_trade_rows"]

    lines = [f"sync {query_id}  {data['raw_bytes']:,} bytes -> {data['archive']}"]
    lines.append(f"  {data['summary']}")
    for t in new_trade_rows:
        lines.append(
            f"    {t['trade_date']}  {t['symbol']:<24}"
            f" {t['open_close'] or '-'} {t['buy_sell'] or '-':<4}"
            f" qty {t['quantity']:>5} @ {t['trade_price']}"
            f"  comm {t['ib_commission']}  {t['currency']}"
        )
    if data["snapshot"]:
        lines.append(f"  snapshot {data['snapshot']}")
    for w in data["warnings"]:
        lines.append(f"  ! {w}")
    _emit(data, "\n".join(lines), args.json)
    return EXIT_OK


def _no_statements(args) -> int:
    print(
        f"No archived statements in {args.archive}.\n"
        f"Run `optjournal fetch <query-id>` first.",
        file=sys.stderr,
    )
    return EXIT_NO_DATA


# -------------------------------------------------------------------- parsing


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="emit JSON")
    verbosity = common.add_mutually_exclusive_group()
    verbosity.add_argument(
        "-v", "--verbose", action="store_true", help="log progress to stderr"
    )
    verbosity.add_argument(
        "-q", "--quiet", action="store_true", help="suppress warnings"
    )

    archive = argparse.ArgumentParser(add_help=False)
    archive.add_argument(
        "--archive", type=Path, default=DEFAULT_ARCHIVE, help="raw statement archive"
    )

    database = argparse.ArgumentParser(add_help=False)
    database.add_argument(
        "--db", type=Path, default=DEFAULT_DB, help="journal database"
    )

    ap = argparse.ArgumentParser(
        prog="optjournal",
        description="Options trading journal backed by IBKR Flex.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--version", action="version", version=f"optjournal {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    p = sub.add_parser("fetch", parents=[common, archive],
                       help="download and archive a Flex query")
    p.add_argument("query_id", help="Flex Query ID from Client Portal")
    p.add_argument("--from", dest="from_date", metavar="DATE",
                   help="YYYYMMDD or YYYY-MM-DD period override")
    p.add_argument("--to", dest="to_date", metavar="DATE",
                   help="YYYYMMDD or YYYY-MM-DD period override")
    p.add_argument("--force", action="store_true",
                   help="bypass the local per-query fetch cooldown")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("statements", parents=[common, archive, database],
                       help="list archived statements and their ingest state")
    p.add_argument("--limit", type=int, default=15, metavar="N",
                   help="show only the newest N (default: 15)")
    p.add_argument("--all", action="store_true", help="show every statement")
    p.set_defaults(func=cmd_statements)

    p = sub.add_parser("prune", parents=[common, archive, database],
                       help="collapse byte-identical duplicate statements")
    p.add_argument("--apply", action="store_true",
                   help="actually delete; omit for a dry run")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("show", parents=[common, archive],
                       help="structural summary of a statement")
    p.add_argument("path", type=Path, nargs="?", help="defaults to newest")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("costs", parents=[common, archive],
                       help="fee, FX and withholding cost report")
    p.add_argument("path", type=Path, nargs="?", help="defaults to newest")
    p.set_defaults(func=cmd_costs)

    p = sub.add_parser("friction", parents=[common, database],
                       help="what the broker cost, from the journal (any scope)")
    p.add_argument("--assets", nargs="*", metavar="CAT",
                   help="asset categories to include (default: every category)")
    p.add_argument("--month", metavar="YYYY-MM", type=_month_or_year,
                   help="narrow to one month or year (default: the whole journal)")
    p.set_defaults(func=cmd_friction)

    p = sub.add_parser("ingest", parents=[common, archive, database],
                       help="fold archived statements into the database")
    p.add_argument("paths", type=Path, nargs="*", help="defaults to all archived")
    p.add_argument("--assets", default="ALL", metavar="LIST",
                   help="asset categories to store, or ALL (default: ALL)")
    p.add_argument("--reingest", action="store_true",
                   help="re-process files already ingested")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("demo", parents=[common],
                       help="generate synthetic data in a scratch archive and DB")
    p.add_argument("--out", type=Path, default=DEFAULT_DEMO_DIR,
                   help=f"archive directory (default: {DEFAULT_DEMO_DIR})")
    p.add_argument("--db", type=Path, default=DEFAULT_DEMO_DB,
                   help=f"database (default: {DEFAULT_DEMO_DB})")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("orders", parents=[common, database],
                       help="option orders with partial fills collapsed")
    p.set_defaults(func=cmd_orders)

    p = sub.add_parser("positions", parents=[common, database],
                       help="current option book")
    p.set_defaults(func=cmd_positions)

    p = sub.add_parser("history", parents=[common, database],
                       help="closed-position P&L, one row per round trip")
    p.add_argument("--assets", default="OPT", metavar="LIST",
                   help="asset category to report, or ALL (default: OPT)")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("watch", parents=[common, database],
                       help="watchlist: prices, realised vol and its 1y rank, "
                            "B-Xtrender daily and weekly, your earnings dates "
                            "and your context")
    p.add_argument("add", nargs="*", metavar="SYMBOL",
                   help="symbols to add; with none, just shows the list")
    p.add_argument("--rm", nargs="+", metavar="SYMBOL", help="symbols to remove")
    # Mutually exclusive, because "set this note" and "remove the note" are two
    # requests and a command carrying both has not said which it wants. Passing
    # neither leaves an existing note alone, which is what makes a bare re-add safe.
    note = p.add_mutually_exclusive_group()
    note.add_argument("--note", help="a note to attach to the symbols named")
    note.add_argument("--clear-note", action="store_true",
                      help="remove the note from the symbols named")
    p.add_argument("--earnings", metavar="YYYY-MM-DD",
                   help="the next earnings date for the symbols named, as YOU "
                        "know it -- no source this journal reaches publishes one. "
                        "Pass '' to remove a date")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("market", parents=[common, database],
                       help="economic calendar: fetch this week, or show what is stored")
    p.add_argument("--fetch", action="store_true",
                   help="pull this week from the feed and store it; without this, "
                        "reads only what the journal already holds")
    p.add_argument("--days", type=int, default=7, metavar="N",
                   help="window to show, from today (default: 7)")
    p.add_argument("--all", dest="all_events", action="store_true",
                   help="every country and impact, not just the default slice")
    p.set_defaults(func=cmd_market)

    p = sub.add_parser("bars", parents=[common, database],
                       help="backfill price bars for the windows positions imply")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="print the derived windows without fetching anything")
    mode.add_argument("--live", action="store_true",
                      help="only the intraday bars of open options, which the "
                           "source serves during the session and never after")
    mode.add_argument("--audit", action="store_true",
                      help="report whether the last session's perishable option "
                           "bars actually landed; fetches nothing")
    p.set_defaults(func=cmd_bars)

    p = sub.add_parser("prepare", parents=[common],
                       help="move the journal to its home folder (the Start file runs this)")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("update", parents=[common, database],
                       help="fast-forward to the latest published commit")
    p.add_argument("--check", action="store_true",
                   help="report what is new without changing anything")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("setup", parents=[common],
                       help="store the Flex token and query id (run me first)")
    p.add_argument("--query-id", dest="query_id", default=None,
                   help="Flex Query ID to store; prompted for when omitted")
    p.add_argument("--token-stdin", action="store_true",
                   help="read the token from stdin instead of prompting, for "
                        "scripted installs")
    p.add_argument("--no-verify", dest="verify", action="store_false",
                   help="skip the confirming fetch, which spends one IBKR "
                        "request against the lockout budget")
    p.set_defaults(func=cmd_setup, verify=True)

    p = sub.add_parser("sync", parents=[common, archive, database],
                       help="fetch, ingest and report new activity (for cron)")
    p.add_argument("query_id", nargs="?",
                   help="Flex Query ID; falls back to $OPTJOURNAL_QUERY_ID")
    p.add_argument("--from", dest="from_date", metavar="DATE",
                   help="YYYYMMDD or YYYY-MM-DD period override")
    p.add_argument("--to", dest="to_date", metavar="DATE",
                   help="YYYYMMDD or YYYY-MM-DD period override")
    p.add_argument("--assets", default="ALL", metavar="LIST",
                   help="asset categories to store, or ALL (default: ALL)")
    p.add_argument("--force", action="store_true",
                   help="bypass the local per-query fetch cooldown")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("confirms", parents=[common, archive, database],
                       help="fetch today's Trade Confirmations (same-session fills)")
    p.add_argument("query_id", nargs="?",
                   help="Trade Confirmation Flex Query ID; falls back to "
                        "$OPTJOURNAL_CONFIRM_QUERY_ID or the stored setting")
    p.add_argument("--from", dest="from_date", metavar="DATE",
                   help="YYYYMMDD or YYYY-MM-DD period override")
    p.add_argument("--to", dest="to_date", metavar="DATE",
                   help="YYYYMMDD or YYYY-MM-DD period override")
    p.add_argument("--assets", default="ALL", metavar="LIST",
                   help="asset categories to store, or ALL (default: ALL)")
    p.add_argument("--force", action="store_true",
                   help="bypass the local per-query fetch cooldown")
    p.set_defaults(func=cmd_confirms)

    p = sub.add_parser("serve", parents=[common],
                       help="local web UI (loopback only, no auth)")
    p.add_argument("--query-id", dest="query_id",
                   help="Flex Query ID; falls back to $OPTJOURNAL_QUERY_ID, "
                        "then to the one saved in Settings, read per request. "
                        "With none, the Sync button is disabled and the "
                        "scheduled sync job fails")
    p.add_argument("--port", type=int, default=8765, help="default: 8765")
    p.add_argument("--host", default="127.0.0.1",
                   help="loopback addresses only (default: 127.0.0.1)")
    p.add_argument("--assets", default="ALL", metavar="LIST",
                   help="asset categories a UI sync stores (default: ALL)")
    p.add_argument("--demo", action="store_true",
                   help=f"serve the synthetic data from `optjournal demo`"
                        f" ({DEFAULT_DEMO_DB})")
    # ON by default, because `serve` IS the application now: with the scheduler off
    # the journal collects nothing unless a human presses a button, which is the
    # arrangement this whole plan replaces. `--no-scheduler` exists for serving a
    # copy of the journal to look at, where firing jobs would write to a database
    # the reader does not intend to change.
    p.add_argument("--no-scheduler", dest="scheduler", action="store_false",
                   help="serve read-only: no jobs run on a schedule (they can "
                        "still be run by hand from the page)")
    # serve declares its OWN path arguments, defaulting to None, instead of
    # inheriting the shared `archive`/`database` parents and overriding their
    # defaults. argparse's set_defaults mutates the *shared action objects*,
    # so the override leaked into every other subcommand -- `optjournal
    # ingest` and the nightly `sync` crashed on archive=None. None here means
    # "not given", which is what lets --demo supply the paths without
    # guessing whether a path equal to the default was typed deliberately.
    p.add_argument("--archive", type=Path, default=None,
                   help=f"raw statement archive (default: {DEFAULT_ARCHIVE})")
    p.add_argument("--db", type=Path, default=None,
                   help=f"journal database (default: {DEFAULT_DB})")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("mutate", parents=[common],
                       help="inject known defects, report which tests catch each")
    p.add_argument("--only", action="append", metavar="KEY",
                   help="run just this defect (repeatable); default is all")
    mutant_workdir = Path(tempfile.gettempdir()) / "optjournal-mutants"
    p.add_argument("--workdir", type=Path, default=mutant_workdir,
                   help=f"where clones are built (default: {mutant_workdir})")
    # Serial by default: a concurrent run interleaves the per-mutant progress
    # lines, and a hang is easier to read about alone. Measured 3.8x at 4 and a
    # further 1.8x at 8, with identical outcomes -- see `mutate.run_all`.
    p.add_argument("--jobs", "-j", type=int, default=1, metavar="N",
                   help="run N mutants concurrently (default: 1; each gets its "
                        "own clone, so ~8 suits a 10-core machine)")
    p.set_defaults(func=cmd_mutate)

    p = sub.add_parser("sweep", parents=[common],
                       help="render every page in a browser and check it")
    p.add_argument("--query-id", dest="query_id", default=None,
                   help="Flex Query ID, so the Sync button renders as it does live")
    p.add_argument("--real-only", action="store_true", help="skip the demo journal")
    p.add_argument("--demo-only", action="store_true", help="skip the real journal")
    # Same None-default reasoning as `serve`: an explicit path must win, and
    # set_defaults on a shared parent would leak into every other subcommand.
    p.add_argument("--archive", type=Path, default=None,
                   help=f"raw statement archive (default: {DEFAULT_ARCHIVE})")
    p.add_argument("--db", type=Path, default=None,
                   help=f"journal database (default: {DEFAULT_DB})")
    p.set_defaults(func=cmd_sweep)

    return ap


def _configure_logging(args) -> None:
    """Quiet by default. Progress and unknown-code notices need -v."""
    if args.verbose:
        level = logging.INFO
    elif args.quiet:
        level = logging.ERROR
    else:
        level = logging.WARNING
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args)

    try:
        code = args.func(args)
    except TokenMissing as exc:
        print(f"\n{exc}", file=sys.stderr)
        return EXIT_CONFIG
    except (TokenRejected, TokenWriteRefused) as exc:
        # Alongside TokenMissing, and a config exit for the same reason: the
        # environment needs one thing from the reader -- a new token, or one
        # command -- and the message says which. A traceback here would bury it,
        # and EXIT_ERROR would tell a cron to retry something no retry can fix.
        print(f"\n{exc}", file=sys.stderr)
        return EXIT_CONFIG
    except FetchCooldown as exc:
        # Nothing was sent, so no request was spent. Same handling as IBKR
        # throttling, but the message distinguishes "we did not ask" from
        # "IBKR pushed back", which matters when diagnosing.
        print(f"\nSkipped: {exc}", file=sys.stderr)
        return EXIT_THROTTLED
    except (FlexRateLimitError, FlexLockoutError) as exc:
        # Not a failure: IBKR throttles repeat generation of the same query.
        # Callers (the daily cron) treat this as "try again later".
        print(f"\nThrottled by IBKR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_THROTTLED
    except FlexError as exc:
        print(
            f"\nFlex request failed: {type(exc).__name__}: {exc}", file=sys.stderr
        )
        return EXIT_ERROR
    except ValueError as exc:
        # Refusals: a non-loopback serve host, or demo data aimed at the real
        # archive or database. The caller gave a bad argument, not a broken
        # environment, so this is a config exit rather than an error.
        print(f"\nRefused: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except sqlite3.OperationalError as exc:
        print(f"\nDatabase error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except FileNotFoundError as exc:
        print(f"\nNot found: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if unknown_codes and not args.quiet and not args.json:
        print(
            f"\nnote: IBKR sent trade codes py_ibkr does not declare: "
            f"{', '.join(sorted(unknown_codes))}",
            file=sys.stderr,
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
