"""Local web UI for the options journal.

Deliberately a stdlib HTTP server and one self-contained HTML page. The whole
project has no runtime dependency beyond py_ibkr and keyring, and a journal
that reads a 220KB SQLite file for a two-position book does not justify a web
framework, a build step or a node_modules.

SECURITY. This binds to 127.0.0.1 only and has no authentication. That is a
deliberate pair: the page exposes an entire brokerage account -- positions,
realised P&L, account costs -- and one endpoint spends real IBKR requests, so
it must never be reachable off-host. The bind address is passed explicitly
rather than defaulted, and `serve()` refuses anything that is not a loopback
address. Do not put this behind a reverse proxy without adding auth first.

The page reads a single /api/state payload rather than one endpoint per panel.
At this data volume the whole journal is a few KB of JSON, so one round trip is
simpler than five and the panels can never disagree with each other.
"""

from __future__ import annotations

import http.server
import ipaddress
import json
import logging
import socket
import sqlite3
import threading
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from typing import Any

from optjournal import __version__
from optjournal.analysis import analyse
from optjournal.db import open_journal
from optjournal.flex import (
    FETCH_COOLDOWN_S,
    FetchCooldown,
    TokenMissing,
    cooldown_remaining,
    fetch,
    last_fetch,
    load,
)
from optjournal.history import build_history
from optjournal.ingest import DEFAULT_ASSET_FILTER, ingest_file
from optjournal.serialize import (
    costs_data,
    history_data,
    newest_statement,
    orders_data,
    positions_data,
    statements_data,
)
from optjournal.stats import (
    EQUITY_CATEGORY,
    EQUITY_TRADES,
    annual_stats,
    available_months,
    cohort_data,
    month_stats,
    monthly_stats,
    odte_cohorts,
    scope_for,
    stats_data,
)

__all__ = ["build_state", "serve"]

log = logging.getLogger(__name__)

#: Only loopback. Checked rather than documented, because the cost of getting
#: this wrong is publishing an unauthenticated brokerage dashboard onto a
#: network.
def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _fx_quotes(conn, base: str) -> list[dict[str, Any]]:
    """Alternative display currencies, with the rate converting base into each.

    A quote here is a *presentation* rate, not a reconciliation. Every `*_base`
    figure in this payload was converted by IBKR at its own trade or snapshot
    date, so no single rate reproduces them all -- on this account the
    order-implied USD rate (0.87952, trade date) and the snapshot rate (0.86732)
    differ by 1.4%. Displaying totals in a non-base currency therefore restates
    them at one stated rate, and the page labels it that way rather than letting
    the numbers look like IBKR's own.

    The newest position snapshot is the only dated FX rate the statement gives
    us. With no snapshot there are no quotes, and the page hides the toggle
    rather than inventing a rate.

    Offered codes are restricted to currencies that appear on *option* trades.
    The snapshot table carries every currency the account holds anything in --
    after the equities re-ingest that meant SEK and KRW from stock positions --
    but this is an options journal, and restating its figures into a currency
    no option ever traded in is noise, not information. The snapshot remains
    the *rate* source; option trades define the *set*.
    """
    option_codes = {
        str(r["currency"] or "").upper()
        for r in conn.execute(
            "SELECT DISTINCT currency FROM trades WHERE asset_category = 'OPT'"
        )
    }
    rows = conn.execute(
        "SELECT currency, fx_rate_to_base, report_date FROM position_snapshots"
        " WHERE fx_rate_to_base IS NOT NULL AND fx_rate_to_base > 0"
        " ORDER BY report_date DESC"
    ).fetchall()
    quotes: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row["currency"] or "").upper()
        if not code or code == base.upper() or code in quotes:
            continue
        if code not in option_codes:
            continue
        quotes[code] = {
            "code": code,
            # Stored rate is native -> base, so invert for base -> native.
            "per_base": 1.0 / float(row["fx_rate_to_base"]),
            "as_of": str(row["report_date"] or ""),
            "source": "position snapshot",
        }
    return list(quotes.values())


def _month_range(conn: sqlite3.Connection) -> list[str]:
    """Every calendar month from the account's first activity to today, newest first.

    This is the *browsable* range, deliberately wider than `available_months`
    (months with fills in the current scope). The calendar walks it month by
    month, and the dropdown offers all of it: a month you held positions but
    did not trade is a real month of the account's life, and rendering it as
    an honest zero beats pretending it does not exist. Derived from any
    activity at all -- trades or cash rows -- so a fills-free account start
    still counts.
    """
    row = conn.execute(
        "SELECT MIN(d) FROM (SELECT MIN(trade_date) AS d FROM trades"
        " UNION ALL SELECT MIN(date_time) FROM cash_transactions)"
    ).fetchone()
    first = str(row[0] or "")[:7]
    if len(first) != 7:
        return []
    y, m = int(first[:4]), int(first[5:7])
    today = date.today()
    out: list[str] = []
    while (y, m) <= (today.year, today.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    out.reverse()
    return out


def build_state(
    *,
    db_path: Path,
    archive_dir: Path,
    query_id: str | None,
    asset_category: str = "OPT",
    month: str | None = None,
    trade_type: str | None = None,
) -> dict[str, Any]:
    """Everything the page renders, in one JSON-safe payload.

    Opens its own connection: sqlite3 objects cannot cross threads and the
    server is threaded, so a shared handle would fail intermittently under the
    one condition nobody tests for.

    `trade_type` drives the Trade Types control for the three views that carry
    the filter bar -- Dashboard, Calendar and Trades. Two kinds of selection
    hide behind one control: "odte" is a fill-level *scope* within options,
    while "equities" switches the *category* the summations run over, because
    stocks are not a subset of options trades. Either way it deliberately
    reaches no further: Positions, Costs, Annual and 0DTE render no filter
    bar, so they stay pinned to the journal's home category. The invariant is
    that a tab's figures change only in response to a control that tab
    displays.
    """
    with open_journal(db_path) as conn:
        # One history pass over the home category, reused by the scope, the
        # cohorts and every period row below.
        report = build_history(conn, asset_category=asset_category)
        if (trade_type or "").lower() == EQUITY_TRADES.key:
            view_category = EQUITY_CATEGORY
            view_report = build_history(conn, asset_category=view_category)
            scope = EQUITY_TRADES
        else:
            view_category = asset_category
            view_report = report
            scope = scope_for(conn, trade_type, asset_category=view_category,
                              report=view_report)
        months = available_months(conn, view_category, scope)
        month_range = _month_range(conn)
        # Any month in the account's lifetime is selectable, not just months
        # this scope has fills in. The previous rule (`month in months`) made a
        # month with no option fills silently fall back to the all-time view --
        # the reader picked May, every figure stayed identical, and nothing on
        # the page said why. An empty month now shows an honest zero month.
        # Months outside the account's lifetime still heal to all-time, so a
        # hand-edited `#month=1999-01` cannot render a calendar of nothing.
        selected = month if month in month_range else None
        # Fill counts per category, so the page can derive which Trade Types
        # buttons are offerable instead of asserting it in markup.
        asset_counts = {
            str(r["asset_category"]): r["n"]
            for r in conn.execute(
                "SELECT asset_category, COUNT(*) AS n FROM trades"
                " GROUP BY asset_category"
            )
        }
        state: dict[str, Any] = {
            "version": __version__,
            "generated_at": _now(),
            "asset_category": asset_category,
            "asset_counts": asset_counts,
            "db": str(db_path),
            "archive": str(archive_dir),
            "months": months,
            "month_range": month_range,
            "selected_month": selected,
            "trade_type": scope.key,
            "trade_type_label": scope.label,
            "stats": stats_data(
                month_stats(conn, selected, asset_category=view_category,
                            scope=scope, report=view_report)
            ),
            "all_time": stats_data(
                month_stats(conn, None, asset_category=view_category,
                            scope=scope, report=view_report)
            ),
            "positions": positions_data(conn),
            "orders": orders_data(conn, scope.order_ids, view_category),
            "history": history_data(report),
            "statements": statements_data(archive_dir, conn),
        }
        # Annual is all-time by construction and ignores both filters. The month
        # selector because a year-by-year table filtered to one month would have
        # a single row -- and the trade-type scope because this tab renders no
        # filter bar. A tab whose numbers move with a control it does not display
        # gives the reader nothing to explain the change with, which is the same
        # failure as a total that silently spans a wider scope than its label.
        state["annual"] = [
            stats_data(s) for s in annual_stats(conn, asset_category=asset_category)
        ]
        state["monthly"] = [
            stats_data(s) for s in monthly_stats(conn, asset_category=asset_category)
        ]
        # The Annual table's total row. Deliberately not `all_time`, which is the
        # Dashboard's figure and therefore scoped: under an active filter the
        # year rows stayed whole while that total shrank, so the table stopped
        # adding up -- destroying the one reconciliation it exists to show.
        state["annual_total"] = stats_data(
            month_stats(conn, None, asset_category=asset_category, report=report)
        )
        # Cohorts are the whole book by definition -- they exist to compare the
        # 0DTE subset against everything else, so scoping them to 0DTE would
        # leave nothing on the other side of the comparison.
        odte, rest, unknown_dte = odte_cohorts(
            conn, asset_category=asset_category, report=report
        )
        state["odte"] = {
            "cohort": cohort_data(odte),
            "rest": cohort_data(rest),
            "unknown_dte": unknown_dte,
            # Whether the filter is offerable at all, derived rather than
            # asserted in the page.
            "selectable": odte.episodes > 0,
        }
        base_ccy = str(state["stats"].get("base_currency") or "")
        state["fx"] = {"base": base_ccy, "quotes": _fx_quotes(conn, base_ccy)}

    newest = newest_statement(archive_dir)
    if newest is not None:
        try:
            reports = [analyse(s) for s in load(newest).FlexStatements]
            state["costs"] = [costs_data(r) for r in reports]
            state["costs_source"] = newest.name
        except Exception as exc:  # pragma: no cover - defensive
            # A malformed archive must not blank the whole page; the rest of
            # the state is read from SQLite and is still valid.
            log.warning("cost report unavailable: %s", exc)
            state["costs"] = []
            state["costs_error"] = str(exc)
    else:
        state["costs"] = []

    state["sync"] = {
        "query_id": query_id,
        "configured": bool(query_id),
        "last_fetch": last_fetch(archive_dir, query_id) if query_id else None,
        "cooldown_s": FETCH_COOLDOWN_S,
        "cooldown_remaining_s": (
            cooldown_remaining(archive_dir, query_id) if query_id else 0
        ),
    }
    return state


def _do_sync(
    *, db_path: Path, archive_dir: Path, query_id: str, assets: tuple[str, ...]
) -> dict[str, Any]:
    """Fetch, archive and ingest. Spends an IBKR request unless refused."""
    started = _now()
    try:
        result = fetch(query_id, archive_dir=archive_dir)
    except FetchCooldown as exc:
        return {
            "ok": False,
            "kind": "cooldown",
            "retry_after_s": exc.retry_after_s,
            "message": str(exc),
        }
    except TokenMissing as exc:
        return {"ok": False, "kind": "config", "message": str(exc)}

    with open_journal(db_path) as conn:
        ingested = ingest_file(conn, result.raw_path, assets=assets)
        new_trades = conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE first_seen_at >= ?", (started,)
        ).fetchone()["n"]
        new_cash = conn.execute(
            "SELECT COUNT(*) AS n FROM cash_transactions WHERE first_seen_at >= ?",
            (started,),
        ).fetchone()["n"]

    return {
        "ok": True,
        "kind": "synced",
        "archive": result.raw_path.name,
        "reused_archive": result.duplicate_of is not None,
        "already_ingested": ingested.already_ingested,
        "duplicate_of": ingested.duplicate_of,
        "new_trades": new_trades,
        "new_cash": new_cash,
        "warnings": ingested.warnings,
    }


@dataclass(frozen=True)
class ServeConfig:
    """Everything a request handler needs, bound at serve() time.

    Injected per instance rather than written onto the handler class. Class
    attributes are process-global mutable state: two serve() calls in one
    process (which the test suite performs routinely) would silently
    reconfigure each other's handlers, and nothing marks the writes as the
    dependency wiring they are. A frozen dataclass makes the configuration
    explicit, immutable, and local to one server.
    """

    db_path: Path
    archive_dir: Path
    query_id: str | None
    assets: tuple[str, ...]
    #: Serialised because two concurrent syncs would each spend an IBKR
    #: request and race on the same archive directory. Lives on the config --
    #: one lock per server -- not on the handler class, where it would be one
    #: lock per process.
    sync_lock: threading.Lock = field(default_factory=threading.Lock)


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = f"optjournal/{__version__}"

    def __init__(self, cfg: ServeConfig, *args: Any, **kwargs: Any) -> None:
        # Assigned before super().__init__, which handles the request inside
        # the constructor -- stdlib quirk, not a style choice.
        self.cfg = cfg
        super().__init__(*args, **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # No external resources are loaded, so lock that down rather than
        # relying on the page never gaining a <script src>.
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: Any) -> None:
        self._send(code, json.dumps(payload, default=str).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        path, _, query = self.path.partition("?")
        params = urllib.parse.parse_qs(query)
        if path in ("/", "/index.html"):
            self._send(200, page_html().encode(), "text/html; charset=utf-8")
        elif path == "/api/state":
            try:
                self._json(200, build_state(
                    db_path=self.cfg.db_path,
                    archive_dir=self.cfg.archive_dir,
                    query_id=self.cfg.query_id,
                    month=(params.get("month") or [None])[0],
                    trade_type=(params.get("type") or [None])[0],
                ))
            except sqlite3.OperationalError as exc:
                self._json(500, {"error": f"database not readable: {exc}"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        path, _, _q = self.path.partition("?")
        if path != "/api/sync":
            self._json(404, {"error": "not found"})
            return
        if not self.cfg.query_id:
            self._json(400, {
                "ok": False, "kind": "config",
                "message": "No Flex query ID configured. Start with "
                           "`optjournal serve --query-id <id>`.",
            })
            return
        # Serialised: two concurrent syncs would each spend a request and race
        # on the same archive directory.
        if not self.cfg.sync_lock.acquire(blocking=False):
            self._json(409, {"ok": False, "kind": "busy",
                             "message": "A sync is already running."})
            return
        try:
            self._json(200, _do_sync(
                db_path=self.cfg.db_path,
                archive_dir=self.cfg.archive_dir,
                query_id=self.cfg.query_id,
                assets=self.cfg.assets,
            ))
        finally:
            self.cfg.sync_lock.release()


def serve(
    *,
    db_path: Path,
    archive_dir: Path,
    query_id: str | None = None,
    assets: tuple[str, ...] = DEFAULT_ASSET_FILTER,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """Serve the UI until interrupted. Loopback only, by construction."""
    if not _is_loopback(host):
        raise ValueError(
            f"refusing to bind {host!r}: this UI has no authentication and "
            f"exposes an entire brokerage account. Loopback only."
        )

    # Read once here and discard. The page is now read per request, so without
    # this a missing or unreadable page.html would not surface until someone
    # loaded the browser and got a 500. Failing at startup keeps the fast
    # signal the old import-time read gave us.
    page_html()

    cfg = ServeConfig(
        db_path=db_path,
        archive_dir=archive_dir,
        query_id=query_id,
        assets=tuple(assets),
    )

    class _Server(http.server.ThreadingHTTPServer):
        daemon_threads = True
        address_family = socket.AF_INET

    # ThreadingHTTPServer instantiates its handler class per request; partial
    # prepends the config, which is the stdlib-sanctioned way to inject
    # dependencies into a BaseHTTPRequestHandler.
    with _Server((host, port), partial(_Handler, cfg)) as httpd:
        actual = httpd.socket.getsockname()[1]
        print(f"optjournal UI on http://{host}:{actual}")
        print("  loopback only, no authentication -- do not expose this port")
        if not query_id:
            print("  no --query-id given, so Sync now is disabled")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


#: The page is a separate file so it can be edited with HTML/CSS tooling and
#: diffed sensibly, rather than living as a multi-hundred-line string literal.
PAGE_PATH = Path(__file__).resolve().parent / "page.html"


def page_html() -> str:
    """The page markup, read fresh on every call.

    Deliberately not cached at import, which is what it used to do. The stale
    copy cost real time: a server left running from an earlier session kept
    serving a pre-edit page, three restart attempts silently failed to bind
    the port and so appeared to change nothing, and a screenshot taken to
    verify a UI change showed the old layout -- which would have been reported
    as the change not working rather than as a stale process.

    Re-reading is ~40KB from the page cache on a loopback-only, single-user
    tool where the page is fetched once per load. That is orders of magnitude
    cheaper than the failure mode it removes, and it means editing the file is
    enough: reload the browser and the edit is there.
    """
    return PAGE_PATH.read_text(encoding="utf-8")
