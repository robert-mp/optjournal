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
import os
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from py_ibkr import FlexError, FlexLockoutError, FlexRateLimitError

from optjournal import __version__, browser
from optjournal.analysis import analyse, format_report
from optjournal.archive import newest_statement, prune_archive
from optjournal.bars import audit_perishable, backfill_bars, bars_manifest
from optjournal.compat import unknown_codes
from optjournal.config import (
    DEFAULT_ARCHIVE,
    DEFAULT_DB,
    DEFAULT_DEMO_DB,
    DEFAULT_DEMO_DIR,
)
from optjournal.db import connect, migrate, open_journal
from optjournal.flex import FetchCooldown, TokenMissing, fetch, load
from optjournal.history import build_history
from optjournal.ingest import ASSET_FILTER_ALL, ingest_file
from optjournal.render import (
    render_history,
    render_orders,
    render_positions,
    render_statements,
    render_summary,
)
from optjournal.serialize import (
    costs_data,
    history_data,
    orders_data,
    positions_data,
    statements_data,
    summary_data,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_NO_DATA = 3
#: IBKR asked us to back off. Distinct from EXIT_ERROR so the daily cron can
#: stay silent on throttling and only alert on a genuine failure.
EXIT_THROTTLED = 4

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
  optjournal costs --json                  cost report as JSON
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


def _open_db(args) -> sqlite3.Connection:
    """A migrated connection the caller owns. Prefer `open_journal` for new
    code; this exists for commands whose connection outlives one block."""
    conn = connect(args.db)
    migrate(conn)
    return conn


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
    conn = _open_db(args) if args.db.exists() else None
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


def cmd_show(args) -> int:
    path = _resolve_path(args)
    if path is None:
        return _no_statements(args)
    data = summary_data(load(path), path)
    data["source_file"] = path.name
    _emit(data, render_summary(data), args.json)
    return EXIT_OK


def cmd_costs(args) -> int:
    path = _resolve_path(args)
    if path is None:
        return _no_statements(args)
    reports = [analyse(s) for s in load(path).FlexStatements]
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
        write_demo_statement,
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

    payload = {
        "query_name": QUERY_NAME, "statement": str(path), "db": str(db),
        "trades": result.trades_inserted, "cash": result.cash_inserted,
        "positions": result.positions_written,
        "option_bars": option_bars,
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

    conn = _open_db(args)
    results = [
        ingest_file(conn, p, assets=assets, reingest=args.reingest) for p in paths
    ]
    totals = {
        t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        for t in ("trades", "cash_transactions", "position_snapshots", "securities")
    }

    data = {
        "db": str(args.db),
        "assets": ",".join(assets) or "ALL",
        "files": [dataclasses.asdict(r) for r in results],
        "totals": totals,
    }
    lines = [f"Ingesting {len(paths)} statement(s) -> {args.db}"
             f"  [assets={data['assets']}]"]
    for r in results:
        if r.already_ingested:
            lines.append(f"  {r.source_file}: unchanged, skipped")
            continue
        lines.append(
            f"  {r.source_file}: trades +{r.trades_inserted}"
            f" (dup {r.trades_skipped_existing},"
            f" filtered {r.trades_filtered_out})"
            f"  cash +{r.cash_inserted} (dup {r.cash_skipped_existing})"
            f"  positions {r.positions_written}"
            f"  securities {r.securities_written}"
        )
        for w in r.warnings:
            lines.append(f"    ! {w}")
    lines.append("  totals: " + ", ".join(f"{k}={v}" for k, v in totals.items()))
    _emit(data, "\n".join(lines), args.json)
    return EXIT_OK


def cmd_orders(args) -> int:
    data = orders_data(_open_db(args))
    _emit(data, render_orders(data), args.json)
    return EXIT_OK if data else EXIT_NO_DATA


def cmd_positions(args) -> int:
    data = positions_data(_open_db(args))
    _emit(data, render_positions(data), args.json)
    return EXIT_OK if data else EXIT_NO_DATA


def cmd_history(args) -> int:
    scope = None if args.assets.strip().upper() == "ALL" else args.assets.strip().upper()
    report = build_history(_open_db(args), asset_category=scope)
    data = history_data(report)
    _emit(data, render_history(data), args.json)
    return EXIT_OK if report.episodes else EXIT_NO_DATA


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
    conn = _open_db(args)
    live = getattr(args, "live", False)

    def day(epoch: int) -> str:
        return datetime.fromtimestamp(epoch, UTC).date().isoformat()

    if getattr(args, "audit", False):
        result = audit_perishable(conn)
        data = dataclasses.asdict(result) | {"ok": result.ok}
        if not result.market_traded:
            lines = [f"{result.day}: the market did not trade, nothing to audit"]
        elif not result.covered and not result.missing:
            lines = [f"{result.day}: no contract was eligible for hourly collection"]
        elif result.missing:
            lines = [
                f"{result.day}: NO hourly option bars for "
                f"{len(result.missing)} of {len(result.covered) + len(result.missing)}"
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
        data = [dataclasses.asdict(r) for r in requests]
        lines = [f"{len(requests)} window(s) derived, nothing fetched"]
        lines += [
            f"  {r.kind:<10} {r.symbol:<20} {r.bar_size}  "
            f"{day(r.start)} -> {day(r.end)}"
            + ("  live-only" if r.perishable else "")
            for r in requests
        ]
        _emit(data, "\n".join(lines), args.json)
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
    try:
        serve(
            db_path=db,
            archive_dir=archive_dir,
            query_id=args.query_id,
            assets=assets,
            host=args.host,
            port=args.port,
        )
    except ValueError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return EXIT_CONFIG
    except OSError as exc:
        print(f"\nCould not bind {args.host}:{args.port}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def cmd_sync(args) -> int:
    """Fetch the latest statement, fold it in, and report only what is new.

    This is the command the daily cron runs, so it is built to be quiet when
    nothing changed and to distinguish IBKR throttling from a real failure --
    a cron that cannot tell those apart either spams or hides outages.
    """
    query_id = args.query_id or os.environ.get("OPTJOURNAL_QUERY_ID")
    if not query_id:
        print(
            "No Flex query ID. Pass it as an argument or set "
            "OPTJOURNAL_QUERY_ID.",
            file=sys.stderr,
        )
        return EXIT_CONFIG

    started = datetime.now(UTC).isoformat(timespec="seconds")
    assets = _asset_filter(args.assets)

    result = fetch(
        query_id,
        archive_dir=args.archive,
        from_date=args.from_date,
        to_date=args.to_date,
        force=args.force,
    )
    conn = _open_db(args)
    ingested = ingest_file(conn, result.raw_path, assets=assets)

    # `first_seen_at` is stamped per row at insert, so anything at or after this
    # run's start timestamp is genuinely new to the journal rather than a row
    # re-presented by an overlapping statement.
    new_trade_rows = [
        dict(r)
        for r in conn.execute(
            "SELECT trade_date, symbol, buy_sell, open_close, quantity, trade_price,"
            " ib_commission, currency FROM trades WHERE first_seen_at >= ?"
            " ORDER BY COALESCE(date_time, trade_date)",
            (started,),
        )
    ]
    new_cash = conn.execute(
        "SELECT COUNT(*) AS n FROM cash_transactions WHERE first_seen_at >= ?",
        (started,),
    ).fetchone()["n"]

    data = {
        "started_at": started,
        "query_id": query_id,
        "raw_path": str(result.raw_path),
        "raw_bytes": result.raw_bytes,
        "already_ingested": ingested.already_ingested,
        # A COUNT under `new_trades`, matching `web._do_sync` and the page's
        # SyncResponse typedef. This key used to hold the row LIST here and the
        # count there -- one name, two types, across two sync implementations
        # that compute the same figure from the same table. Nothing broke,
        # because each consumer only ever met one producer (the cron reads this
        # payload, the page reads web's), which is exactly what makes it a trap:
        # the first reader to meet the other shape would have iterated an int or
        # formatted a list.
        "new_trades": len(new_trade_rows),
        # The rows themselves, under a name that says it is a list. The cron
        # prints one line per fill from these.
        "new_trade_rows": new_trade_rows,
        "new_cash": new_cash,
        "positions_written": ingested.positions_written,
        "warnings": ingested.warnings,
        "changed": bool(new_trade_rows or new_cash),
    }

    lines = [f"sync {query_id}  {result.raw_bytes:,} bytes -> {result.raw_path.name}"]
    if ingested.already_ingested:
        lines.append("  statement byte-identical to a previous fetch, nothing to do")
    elif not data["changed"]:
        lines.append(
            f"  no new activity  (positions refreshed:"
            f" {ingested.positions_written})"
        )
    else:
        lines.append(
            f"  {len(new_trade_rows)} new trade(s), {new_cash} new cash row(s)"
        )
        for t in new_trade_rows:
            lines.append(
                f"    {t['trade_date']}  {t['symbol']:<24}"
                f" {t['open_close'] or '-'} {t['buy_sell'] or '-':<4}"
                f" qty {t['quantity']:>5} @ {t['trade_price']}"
                f"  comm {t['ib_commission']}  {t['currency']}"
            )
    for w in ingested.warnings:
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

    p = sub.add_parser("serve", parents=[common],
                       help="local web UI (loopback only, no auth)")
    p.add_argument("--query-id", dest="query_id",
                   help="Flex Query ID; without it the Sync button is disabled")
    p.add_argument("--port", type=int, default=8765, help="default: 8765")
    p.add_argument("--host", default="127.0.0.1",
                   help="loopback addresses only (default: 127.0.0.1)")
    p.add_argument("--assets", default="ALL", metavar="LIST",
                   help="asset categories a UI sync stores (default: ALL)")
    p.add_argument("--demo", action="store_true",
                   help=f"serve the synthetic data from `optjournal demo`"
                        f" ({DEFAULT_DEMO_DB})")
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
