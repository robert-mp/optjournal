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

import dataclasses
import http.server
import ipaddress
import json
import logging
import socket
import sqlite3
import threading
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from optjournal import __version__
from optjournal.analysis import analyse
from optjournal.db import connect, migrate
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
from optjournal.ingest import ASSET_FILTER_OPTIONS, ingest_file
from optjournal.render import (
    costs_data,
    history_data,
    newest_statement,
    orders_data,
    positions_data,
    statements_data,
)
from optjournal.stats import available_months, month_stats, stats_data

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
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    """
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
        quotes[code] = {
            "code": code,
            # Stored rate is native -> base, so invert for base -> native.
            "per_base": 1.0 / float(row["fx_rate_to_base"]),
            "as_of": str(row["report_date"] or ""),
            "source": "position snapshot",
        }
    return list(quotes.values())


def build_state(
    *,
    db_path: Path,
    archive_dir: Path,
    query_id: str | None,
    asset_category: str = "OPT",
    month: str | None = None,
) -> dict[str, Any]:
    """Everything the page renders, in one JSON-safe payload.

    Opens its own connection: sqlite3 objects cannot cross threads and the
    server is threaded, so a shared handle would fail intermittently under the
    one condition nobody tests for.
    """
    conn = connect(db_path)
    try:
        migrate(conn)
        months = available_months(conn, asset_category)
        selected = month if month in months else None
        state: dict[str, Any] = {
            "version": __version__,
            "generated_at": _now(),
            "asset_category": asset_category,
            "db": str(db_path),
            "archive": str(archive_dir),
            "months": months,
            "selected_month": selected,
            "stats": stats_data(
                month_stats(conn, selected, asset_category=asset_category)
            ),
            "all_time": stats_data(
                month_stats(conn, None, asset_category=asset_category)
            ),
            "positions": positions_data(conn),
            "orders": orders_data(conn),
            "history": history_data(build_history(conn, asset_category=asset_category)),
            "statements": statements_data(archive_dir, conn),
        }
        base_ccy = str(state["stats"].get("base_currency") or "")
        state["fx"] = {"base": base_ccy, "quotes": _fx_quotes(conn, base_ccy)}
    finally:
        conn.close()

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

    conn = connect(db_path)
    try:
        migrate(conn)
        ingested = ingest_file(conn, result.raw_path, assets=assets)
        new_trades = conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE first_seen_at >= ?", (started,)
        ).fetchone()["n"]
        new_cash = conn.execute(
            "SELECT COUNT(*) AS n FROM cash_transactions WHERE first_seen_at >= ?",
            (started,),
        ).fetchone()["n"]
    finally:
        conn.close()

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


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = f"optjournal/{__version__}"
    # Config injected by serve(); class attributes keep the handler picklable
    # and avoid a closure-over-mutable-state bug.
    db_path: Path
    archive_dir: Path
    query_id: str | None
    assets: tuple[str, ...]
    _sync_lock = threading.Lock()

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
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/api/state":
            try:
                self._json(200, build_state(
                    db_path=self.db_path,
                    archive_dir=self.archive_dir,
                    query_id=self.query_id,
                    month=(params.get("month") or [None])[0],
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
        if not self.query_id:
            self._json(400, {
                "ok": False, "kind": "config",
                "message": "No Flex query ID configured. Start with "
                           "`optjournal serve --query-id <id>`.",
            })
            return
        # Serialised: two concurrent syncs would each spend a request and race
        # on the same archive directory.
        if not self._sync_lock.acquire(blocking=False):
            self._json(409, {"ok": False, "kind": "busy",
                             "message": "A sync is already running."})
            return
        try:
            self._json(200, _do_sync(
                db_path=self.db_path,
                archive_dir=self.archive_dir,
                query_id=self.query_id,
                assets=self.assets,
            ))
        finally:
            self._sync_lock.release()


def serve(
    *,
    db_path: Path,
    archive_dir: Path,
    query_id: str | None = None,
    assets: tuple[str, ...] = ASSET_FILTER_OPTIONS,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """Serve the UI until interrupted. Loopback only, by construction."""
    if not _is_loopback(host):
        raise ValueError(
            f"refusing to bind {host!r}: this UI has no authentication and "
            f"exposes an entire brokerage account. Loopback only."
        )

    _Handler.db_path = db_path
    _Handler.archive_dir = archive_dir
    _Handler.query_id = query_id
    _Handler.assets = tuple(assets)

    class _Server(http.server.ThreadingHTTPServer):
        daemon_threads = True
        address_family = socket.AF_INET

    with _Server((host, port), _Handler) as httpd:
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
#: Read once at import: it is ~20KB and never changes at runtime.
PAGE = (Path(__file__).resolve().parent / "page.html").read_text(encoding="utf-8")
