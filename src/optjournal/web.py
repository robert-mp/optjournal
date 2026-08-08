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

TWO different attackers, and the loopback bind only stops one. It keeps the
journal off the NETWORK; it does nothing about YOUR OWN BROWSER, which will POST
here on behalf of any page you have open. With no auth and a request-spending
endpoint, that means a site in another tab could push you toward an IBKR lockout
while the journal runs. Found by testing it rather than by reasoning: a POST
carrying `Origin: https://evil.example` ran a real sync. So `do_POST` checks the
origin BEFORE it routes, which is what makes a write endpoint added later safe by
default instead of safe by remembering. See `_origin_is_same`.

That check compares scheme-host-AND-PORT against the socket the server bound,
because the first version compared only the hostname and a page served on
`http://127.0.0.1:8799` walked straight through it -- demonstrated in a real
browser, which is the only place it was visible. "Loopback" is not one origin;
every local port is its own, so anything that can serve a single file locally
would otherwise have write access.

The page reads a single /api/state payload rather than one endpoint per panel.
At this data volume the whole journal is a few KB of JSON, so one round trip is
simpler than five and the panels can never disagree with each other.
"""

from __future__ import annotations

import http.server
import ipaddress
import json
import logging
import re
import socket
import sqlite3
import threading
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from optjournal import __version__
from optjournal.analysis import analyse
from optjournal.archive import newest_statement
from optjournal.bars import (
    ReplayLeg,
    delta_around,
    epoch_et,
    replay_bars,
    replay_model,
)
from optjournal.db import open_journal
from optjournal.events import (
    EventFetchError,
    EventRateLimited,
    fetch_events,
    store_events,
)
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
from optjournal.marketdata import BarFetchError, fetch_quote
from optjournal.serialize import (
    costs_data,
    history_data,
    market_data,
    orders_data,
    positions_data,
    statements_data,
    watchlist_data,
)
from optjournal.stats import (
    EQUITY_CATEGORY,
    EQUITY_TRADES,
    annual_stats,
    available_months,
    cohort_data,
    fx_quotes,
    month_range,
    month_stats,
    monthly_stats,
    odte_cohorts,
    scope_for,
    stats_data,
)
from optjournal.strategies import (
    open_position_count,
    position_groups,
    strategy_groups,
)

__all__ = ["build_state", "serve", "serve_ephemeral"]

log = logging.getLogger(__name__)

#: Only loopback. Checked rather than documented, because the cost of getting
#: this wrong is publishing an unauthenticated brokerage dashboard onto a
#: network.
def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


#: A symbol is letters, digits, dot, dash -- enough for BRK.B and foreign
#: listings, and not enough for a path or a quote. Not a ticker universe:
#: see `_watchlist_write` on why this journal has no such list.
_SYMBOL_OK = re.compile(r"^[A-Za-z0-9.\-]+$")


def _origin_is_same(origin: str | None, *, host: str, port: int) -> bool:
    """Whether a POST's `Origin` is THIS server, port included.

    Binding loopback keeps the journal off the NETWORK. It does nothing about
    your own browser, which will POST here on behalf of any page you have open --
    and these writes are not idempotent reads. `/api/sync` spends an IBKR request
    against a lockout budget, so an unguarded endpoint means a page in another tab
    can push you toward a lockout while the journal runs. Confirmed, not
    theorised: a POST carrying `Origin: https://evil.example` ran a real sync and
    moved `last_fetch`.

    THE PORT IS PART OF THE ORIGIN, and leaving it out was a real hole in the
    first version of this function -- caught by a browser test, not by reasoning.
    Checking only that the hostname was loopback let a page served by ANY local
    process through: an attacker page on `http://127.0.0.1:8799` POSTed to the
    journal on 8792 and got past the guard. Anything that can serve one file on a
    high port -- a dev server, a `python -m http.server` in a downloads folder,
    another tool's UI -- could then spend the request budget. Same-origin means
    scheme, host AND port; two ports on one machine are two origins, which is
    exactly what the browser's own rules say.

    `None` is allowed, and that is the load-bearing decision. A browser ALWAYS
    sends `Origin` on a fetch POST -- verified in a real browser against a probe
    server, which reported `Origin: http://127.0.0.1:<port>` plus
    `Sec-Fetch-Site: same-origin` -- so a missing header means the caller is not a
    browser. curl and a future CLI are not the threat model; a page in a tab is.
    Refusing `None` would break the former and stop nothing.

    The bound host is compared through `_is_loopback` on BOTH sides rather than by
    string, so a journal served on `127.0.0.1` accepts its own page loaded as
    `localhost` -- the same server, and a browser sends whichever name was typed.
    `http://127.0.0.1.evil.com` still fails, because its hostname is not loopback.
    """
    if origin is None:
        return True
    parts = urllib.parse.urlsplit(origin)
    if not parts.hostname or parts.port != port:
        return False
    # Loopback-to-loopback rather than equality: 127.0.0.1, localhost and ::1 all
    # name this server, and which one appears depends on what was typed.
    if _is_loopback(host):
        return _is_loopback(parts.hostname)
    return parts.hostname == host


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _strikes_of(legs: list[ReplayLeg]) -> list[dict[str, Any]]:
    """One entry per contract, sided by how it was OPENED and spanning when held.

    The window is what makes a strike a SEGMENT rather than a full-width rule.
    Drawn edge to edge, a strike claims to have existed for the whole chart --
    including the session of context before entry, and every bar after a roll
    moved it somewhere else. The reference implementation draws segments for
    exactly this reason: the picture should say which levels were live when.

    Side comes from the opening fill because a closed contract holds the same
    strike twice, sold to open and bought to close. A strike you sold is a level
    you are defending and one you bought is a level you paid for; the chart
    encodes that difference, so reading it off the wrong fill inverts the meaning
    of every line.

    ``to`` is None while the contract is still held, which the page draws to the
    right edge. ``frm`` is None only for a snapshot-only contract, whose entry
    date is unknown -- drawn full width, because the honest statement there is
    "held throughout" rather than a guessed start.
    """
    out: list[dict[str, Any]] = []
    for leg in legs:
        # Running position, so the segment ends where the contract went flat
        # rather than at whichever fill happened to be last.
        quantity = leg.seed_quantity
        opened_at: int | None = None
        closed_at: int | None = None
        sold = leg.seed_quantity < 0
        for index, (stamp, delta_qty, _price) in enumerate(leg.fills):
            if index == 0:
                opened_at, sold = stamp, delta_qty < 0
            quantity += delta_qty
            if quantity == 0:
                closed_at = stamp
                break
        out.append({
            "strike": leg.strike,
            "put_call": leg.right,
            "side": "short" if sold else "long",
            "frm": opened_at,
            "to": closed_at,
        })
    return sorted(out, key=lambda row: row["strike"])


def _replay_legs(rows: list[dict[str, Any]]) -> list[ReplayLeg]:
    """One ReplayLeg per distinct contract, carrying its whole fill schedule.

    Grouped by conid rather than one leg per fill, because the mark-to-market
    walks a running position: a contract sold to open and bought to close is ONE
    leg with two fills, and treating it as two legs would hold both at once and
    double the position.

    Quantities are signed as the fill states them -- IBKR sends -3 for a sale --
    so cash flow and position both fall out of the same number without a
    buy_sell branch.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        conid = str(row.get("conid") or "")
        strike, expiry = row.get("strike"), row.get("expiry")
        if not conid or strike is None or not expiry:
            continue
        stamp = epoch_et(row.get("first_fill_at"))
        quantity, price = row.get("quantity"), row.get("avg_price")
        entry = grouped.setdefault(conid, {
            "conid": conid,
            "strike": float(strike),
            "right": str(row.get("put_call") or ""),
            "expiry": str(expiry),
            "multiplier": float(row.get("multiplier") or 100.0),
            "fills": [],
        })
        if stamp is not None and quantity is not None and price is not None:
            entry["fills"].append((stamp, float(quantity), float(price)))
    return [
        ReplayLeg(
            conid=entry["conid"], strike=entry["strike"], right=entry["right"],
            expiry=entry["expiry"], multiplier=entry["multiplier"],
            fills=tuple(sorted(entry["fills"])),
        )
        for entry in grouped.values()
    ]


def _snapshot_leg(row: dict[str, Any]) -> ReplayLeg:
    """The single leg a position snapshot row implies.

    One constructor because the strikes and the modelled marks need the SAME
    leg: built twice, the two could disagree about the seed quantity or price
    and the chart would draw a strike segment for a position the P&L series was
    not following.

    Seeded rather than filled: a row reaching this path has no fills anywhere --
    that is what makes it snapshot-only -- so the position is held flat across
    the window at the basis the snapshot states.
    """
    return ReplayLeg(
        conid=str(row.get("conid") or ""),
        strike=float(row.get("strike") or 0.0),
        right=str(row.get("put_call") or ""),
        expiry=str(row.get("expiry") or ""),
        multiplier=float(row.get("multiplier") or 100.0),
        seed_quantity=float(row.get("position") or 0.0),
        seed_price=float(row.get("cost_basis_price") or 0.0),
    )


def _annotations(
    lifecycle: dict[str, Any], marks: list[list[float]]
) -> list[dict[str, Any]]:
    """One card per EVENT on the timeline: what was done, what it cost, what it changed.

    An event, not a fill. A four-leg iron condor opened in one order is one
    decision and belongs on one card; splitting it per fill would turn a single
    act into four annotations that each look like a separate trade. The grouping
    is strategies.py's, already computed -- which is also how a roll arrives
    labelled as one: an order with both opening and closing legs classifies as
    "Roll" there, so the card needs no detection of its own and cannot disagree
    with the event caption shown elsewhere on the same page.

    ``kind`` is derived from the legs' open/close markers rather than parsed out
    of the label, because the label is prose meant for a human ("Short put
    close") and matching on it would break the moment classify() rewords.

    Delta before and after come from the modelled marks, so a roll states the
    exposure it removed. An OPENING event reports ``None -> x``: there was no
    position to have a delta.
    """
    out: list[dict[str, Any]] = []
    for event in lifecycle.get("events") or []:
        stamp = epoch_et(event.get("first_fill_at"))
        if stamp is None:
            continue
        legs = [
            leg
            for order in (event.get("orders") or [])
            for leg in (order.get("legs") or [])
        ]
        markers = {str(leg.get("open_close") or "").upper() for leg in legs}
        kind = (
            "roll" if len(markers) > 1
            else "close" if markers == {"C"}
            else "open"
        )
        before, after = delta_around(marks, stamp)
        out.append({
            "ts": stamp,
            "at": str(event.get("first_fill_at") or "")[:16],
            "label": event.get("label"),
            "kind": kind,
            # Only what a card shows. Passing the whole leg would ship account
            # ids and both currencies' worth of every figure into a tooltip.
            "legs": [
                {
                    "strike": leg.get("strike"),
                    "put_call": leg.get("put_call"),
                    "buy_sell": leg.get("buy_sell"),
                    "open_close": leg.get("open_close"),
                    "quantity": leg.get("quantity"),
                    "avg_price": leg.get("avg_price"),
                }
                for leg in legs
            ],
            "cash": event.get("proceeds"),
            "commission": event.get("commission"),
            # Realised P&L is meaningless on an opening event -- the episode layer
            # reports 0.0 there, and a card reading "realised $0.00" beside an
            # opening credit invites the reader to think the trade made nothing.
            "realized": event.get("realized_pnl") if kind != "open" else None,
            "delta_before": before,
            "delta_after": after,
        })
    return sorted(out, key=lambda row: row["ts"])


def _attach_replays(conn: sqlite3.Connection, state: dict[str, Any]) -> None:
    """Build one replay per trade and point rows at it by key.

    Replays live in a shared ``replays`` map rather than inline on each row,
    because a strangle's two position rows are ONE trade: a copy each would ship
    the underlying series twice and let a reader open two charts of the same
    position and wonder why they differ.

    Only an OPEN lifecycle claims a position row. Close a strike and reopen it
    and both lifecycles share a conid, so claiming by conid alone would aim a
    live position at the chart of a trade that already ended.

    A row no open lifecycle claims gets its own single-strike replay. That is the
    snapshot-only case -- the LEAP, which has no fills anywhere and is otherwise
    the position with the most history and no way to see it.

    Position ROWS gain no key. The page resolves a row to its lifecycle's replay
    through the same open-lifecycle-by-conid map it already builds to group the
    table, so a strangle's two legs open one chart showing both strikes. Writing
    the key onto the row instead would make the row scope-dependent -- the trade
    type filter changes which lifecycles exist, so the same position would carry
    a different key under a different filter, and the open book is the open book
    whatever subset of trades you are looking at.
    """
    replays: dict[str, dict[str, Any]] = {}

    for lifecycle in state["lifecycles"]:
        conids = [str(c) for c in (lifecycle.get("conids") or [])]
        opened, closed = lifecycle.get("opened_at"), lifecycle.get("closed_at")
        key = "lc:" + "-".join(conids) + "@" + str(opened or "")[:10]
        legs = [
            leg
            for event in (lifecycle.get("events") or [])
            for order in (event.get("orders") or [])
            for leg in (order.get("legs") or [])
        ]
        bars = replay_bars(
            conn, str(lifecycle.get("underlying") or ""),
            opened_at=opened, closed_at=closed,
        )
        # Grouped once and shared: the strikes and the marks must follow the same
        # legs, or a segment could be drawn for a position the P&L series was not
        # walking. Same reason `_snapshot_leg` is one constructor.
        replay_legs = _replay_legs(legs)
        # One vol solve behind both, via bars.replay_model. Solving per consumer
        # meant 20 solves for 10 replays and 60% of build_state inside them.
        band, marks = replay_model(
            conn, replay_legs, bars.points, underlying_conid=bars.conid,
        )
        replays[key] = {
            "key": key,
            "underlying": lifecycle.get("underlying"),
            "label": lifecycle.get("label"),
            "bar_size": bars.bar_size,
            "points": [[ts, close] for ts, close in bars.points],
            "strikes": _strikes_of(replay_legs),
            "opened_at": opened,
            "closed_at": closed,
            # Epochs, so the page never parses a timezone. Every journal stamp is
            # US Eastern (bars.epoch_et carries the evidence) and the chart
            # labels the same zone, so fills and bars share one timeline.
            "opened_ts": epoch_et(opened),
            "closed_ts": epoch_et(closed),
            "fills": sorted(
                {ts for ts in (epoch_et(leg.get("first_fill_at")) for leg in legs)
                 if ts is not None}
            ),
            # From the same solve the marks walk, so the band and the P&L series
            # cannot disagree about what the market charged for a contract.
            "band": band,
            "marks": marks,
            "events": _annotations(lifecycle, marks),
        }
        lifecycle["replay_key"] = key

    claimed = {
        str(conid): lifecycle["replay_key"]
        for lifecycle in state["lifecycles"]
        if lifecycle.get("status") == "open"
        for conid in (lifecycle.get("conids") or [])
    }
    for row in state["positions"]:
        conid = str(row.get("conid") or "")
        if conid in claimed:
            continue
        key = "pos:" + conid
        opened = row.get("open_date_time")
        bars = replay_bars(
            conn, str(row.get("underlying_symbol") or ""),
            opened_at=opened, closed_at=None,
        )
        # One leg, shared by the strikes and the marks below: built twice they
        # could disagree, and the chart would draw a segment for a position the
        # P&L series was not following.
        legs = [_snapshot_leg(row)]
        band, marks = replay_model(
            conn, legs, bars.points, underlying_conid=bars.conid,
        )
        replays[key] = {
            "key": key,
            "underlying": row.get("underlying_symbol"),
            "label": "Open position",
            "bar_size": bars.bar_size,
            "points": [[ts, close] for ts, close in bars.points],
            # A snapshot row has no fills, so its side comes from the signed
            # position and its window stays unknown -- drawn full width.
            "strikes": _strikes_of(legs),
            "opened_at": opened,
            "closed_at": None,
            # A snapshot row has no fills anywhere -- that is what makes it
            # snapshot-only -- so there is nothing to mark and no entry to mark
            # it from. Empty rather than guessed.
            "opened_ts": epoch_et(opened),
            "closed_ts": None,
            "fills": [],
            "band": band,
            "marks": marks,
            # No fills means no events to annotate. Explicitly empty rather than
            # absent, so the page reads one shape for every replay.
            "events": [],
        }

    state["replays"] = replays


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
        browsable = month_range(conn)
        # Any month in the account's lifetime is selectable, not just months
        # this scope has fills in. The previous rule (`month in months`) made a
        # month with no option fills silently fall back to the all-time view --
        # the reader picked May, every figure stayed identical, and nothing on
        # the page said why. An empty month now shows an honest zero month.
        # Months outside the account's lifetime still heal to all-time, so a
        # hand-edited `#month=1999-01` cannot render a calendar of nothing.
        selected = month if month in browsable else None
        # Fill counts per category, so the page can derive which Trade Types
        # buttons are offerable instead of asserting it in markup.
        asset_counts = {
            str(r["asset_category"]): r["n"]
            for r in conn.execute(
                "SELECT asset_category, COUNT(*) AS n FROM trades"
                " GROUP BY asset_category"
            )
        }
        # One fetch, three lenses: `orders`, `strategies` and `lifecycles`
        # all present the same rows, and neither grouping layer mutates
        # what it receives (pinned in test_strategies), so fetching per
        # consumer was three times the queries buying nothing.
        orders = orders_data(conn, scope.order_ids, view_category)
        state: dict[str, Any] = {
            "version": __version__,
            "generated_at": _now(),
            "asset_category": asset_category,
            "asset_counts": asset_counts,
            "db": str(db_path),
            "archive": str(archive_dir),
            "months": months,
            "month_range": browsable,
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
            "orders": orders,
            # The same orders folded into the strategies they were placed
            # as -- a strangle sold as two same-second orders is one group.
            "strategies": strategy_groups(orders),
            # ... and further linked into position lifecycles: the open and
            # the close of one position share an episode, so they are one
            # card. Exact linkage via episode trade ids, not a time window.
            "lifecycles": position_groups(
                orders,
                episodes=[*view_report.closed, *view_report.open],
                trade_to_order={
                    str(r["trade_id"]): str(r["ib_order_id"])
                    for r in conn.execute(
                        "SELECT trade_id, ib_order_id FROM trades"
                        " WHERE ib_order_id IS NOT NULL"
                    )
                },
            ),
            "history": history_data(report),
            "statements": statements_data(archive_dir, conn),
            # Read-only: the page never fetches the calendar. `optjournal market
            # --fetch` does, from the nightly cron, because the feed rate limits
            # (429 with a retry-after) and a tab reload is not a reason to spend
            # a request against it.
            "market": market_data(conn, now=datetime.now(UTC)),
            # Prices and realised vol from bars already stored, so this
            # spends nothing. `optjournal bars` is what fills them in.
            "watchlist": watchlist_data(conn),
        }
        # How many POSITIONS the open contracts form, which needs the lifecycle
        # grouping and so cannot be computed inside month_stats. Set on both
        # blocks because they share the Stats shape, and it is period-invariant
        # either way: the open book is the open book whatever month is selected.
        open_positions = open_position_count(
            state["lifecycles"],
            [e for e in view_report.open if scope.has_episode(e)],
        )
        for block in ("stats", "all_time"):
            state[block]["open_positions"] = open_positions
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
        state["fx"] = {"base": base_ccy, "quotes": fx_quotes(conn, base_ccy)}
        _attach_replays(conn, state)

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
        "reused_archive": result.is_duplicate,
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
        elif path.startswith("/static/"):
            # Same-origin only, and only the files shipped beside the page: the
            # CSP is `default-src 'self'`, and a path that could escape this
            # directory would turn a local journal viewer into a file server.
            name = path[len("/static/"):]
            asset = (Path(__file__).parent / "static" / name).resolve()
            root = (Path(__file__).parent / "static").resolve()
            if name and root in asset.parents and asset.is_file():
                self._send(200, asset.read_bytes(),
                           "text/javascript; charset=utf-8")
            else:
                self._json(404, {"error": "not found"})
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
        elif path == "/api/quotes":
            self._json(*self._quotes())
        else:
            self._json(404, {"error": "not found"})

    def _quotes(self) -> tuple[int, dict[str, Any]]:
        """Last-trade prices for the watched symbols, fetched on demand.

        A GET because it reads, but it is NOT free: one HTTP request per symbol to
        a public endpoint. That is exactly why it is a separate route rather than
        part of `/api/state` -- putting it there would spend N requests on every
        page load, every tab switch and every month filter, for a column only one
        tab shows. The page asks when the Watchlist opens and when Refresh is
        pressed, and nothing else triggers it.

        Never written to `price_bars`. A live intraday price is not a settled
        close, and storing one would feed a half-formed session into
        `realised_vol` -- a measurement quietly corrupted by a display feature.
        The quote lives in the response and nowhere else.

        A symbol that fails is reported as a null price rather than failing the
        whole request: one dead ticker should not blank the other three.
        """
        with open_journal(self.cfg.db_path) as conn:
            symbols = [str(row["symbol"]) for row in conn.execute(
                "SELECT symbol FROM watchlist ORDER BY symbol")]
        quotes: dict[str, Any] = {}
        failed: list[str] = []
        for symbol in symbols:
            try:
                quote = fetch_quote(symbol)
            except BarFetchError as exc:
                log.debug("quote %s failed: %s", symbol, exc)
                failed.append(symbol)
                continue
            quotes[symbol] = {
                "price": quote.price,
                "at": quote.at,
                "previous_close": quote.previous_close,
                "currency": quote.currency,
            }
        return 200, {
            "ok": True,
            "quotes": quotes,
            "failed": failed,
            #: The server's clock at the moment it answered, so the page renders
            #: an AGE rather than a timestamp it would have to trust its own clock
            #: to interpret. A quote is meaningless without one -- run on a
            #: Saturday every one of these is Friday's close, 22 hours old.
            "asked_at": int(datetime.now(UTC).timestamp()),
        }

    def _same_origin(self) -> bool:
        """`Origin` against the socket this server actually bound.

        From `server_address`, not the `Host` header: the socket is what the
        process is really listening on, while `Host` is client-supplied and so
        cannot be trusted to decide whether a client is trusted.
        """
        address = self.server.server_address
        bound_host = str(address[0]) if isinstance(address, tuple) else ""
        bound_port = int(address[1]) if isinstance(address, tuple) else 0
        return _origin_is_same(
            self.headers.get("Origin"), host=bound_host, port=bound_port
        )

    def _body(self, limit: int = 8192) -> dict[str, Any]:
        """The request's JSON object, or {}.

        Length-capped because `rfile.read` on a Content-Length the client chose is
        an unbounded allocation, and this server has no framework to do it for us.
        8KB is four orders of magnitude more than any body here needs.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > limit:
            return {}
        try:
            parsed = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _market_fetch(self) -> tuple[int, dict[str, Any]]:
        """Refresh the calendar from the feed. No IBKR request, no lock.

        Deliberately NOT behind the sync lock: that lock exists to stop two IBKR
        fetches racing on the archive, and this touches neither. It does hit a
        rate-limited feed, so a 429 comes back as its own `kind` and the page says
        so rather than reporting a failure -- nothing is lost, the same week is
        served tomorrow.
        """
        try:
            events = fetch_events()
        except EventRateLimited as exc:
            return 429, {"ok": False, "kind": "throttled", "message": str(exc)}
        except EventFetchError as exc:
            return 502, {"ok": False, "kind": "feed", "message": str(exc)}
        with open_journal(self.cfg.db_path) as conn:
            stored = store_events(conn, events)
        return 200, {"ok": True, "kind": "market", "fetched": len(events),
                     "stored": stored}

    def _watchlist_write(self) -> tuple[int, dict[str, Any]]:
        """Add or remove one watched symbol.

        One symbol per request rather than a submitted list, because the UI edits
        one row at a time and a list would need a merge rule (is an absent symbol
        a removal?) that nothing asks for.

        The symbol is upper-cased and length-checked but NOT validated against a
        ticker universe: this journal has no such list, and inventing one would
        reject a legitimate foreign listing. A symbol that does not exist simply
        stores a row with no bars, which the tab already renders as a dash.
        """
        body = self._body()
        symbol = str(body.get("symbol") or "").strip().upper()
        action = str(body.get("action") or "add")
        if not symbol or len(symbol) > 24 or not _SYMBOL_OK.match(symbol):
            return 400, {"ok": False, "kind": "symbol",
                         "message": f"{symbol or '(empty)'} is not a symbol"}
        if action not in ("add", "remove"):
            return 400, {"ok": False, "kind": "action",
                         "message": f"unknown action {action!r}"}
        note = body.get("note")
        with open_journal(self.cfg.db_path) as conn:
            if action == "remove":
                cursor = conn.execute(
                    "DELETE FROM watchlist WHERE symbol = ?", (symbol,))
                changed = cursor.rowcount
            else:
                # COALESCE, so re-adding a symbol does not blank an existing note
                # -- the same upsert rule `optjournal watch` uses.
                conn.execute(
                    "INSERT INTO watchlist (symbol, note, added_at) VALUES (?,?,?)"
                    " ON CONFLICT(symbol) DO UPDATE SET"
                    " note=COALESCE(excluded.note, note)",
                    (symbol, str(note) if note else None, _now()),
                )
                changed = 1
            conn.commit()
        return 200, {"ok": True, "kind": "watchlist", "action": action,
                     "symbol": symbol, "changed": changed}

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        path, _, _q = self.path.partition("?")
        # BEFORE the route check, so every write endpoint added later is covered
        # by default rather than by remembering. A 403 here costs a foreign page
        # nothing; letting it through costs an IBKR request.
        if not self._same_origin():
            self._json(403, {
                "ok": False, "kind": "origin",
                "message": "cross-origin writes are refused: this journal has no "
                           "authentication, so a page you have open could "
                           "otherwise spend your IBKR request budget.",
            })
            return
        if path == "/api/market/fetch":
            self._json(*self._market_fetch())
            return
        if path == "/api/watchlist":
            self._json(*self._watchlist_write())
            return
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


@contextmanager
def serve_ephemeral(
    *,
    db_path: Path,
    archive_dir: Path,
    query_id: str | None = None,
    assets: tuple[str, ...] = DEFAULT_ASSET_FILTER,
) -> Iterator[str]:
    """A real server on an OS-picked port, for the duration of the block.

    Yields the base URL. Wired through the same `ServeConfig` and `_Handler` as
    `serve()`, so a caller exercises the production request path rather than a
    lookalike -- which is the whole point, and the reason this lives here rather
    than in whichever caller needed it first.

    It exists because the sweep and the browser-render test had each built this
    twelve-line spin-up for themselves, byte-identical down to the docstring
    phrase "not a lookalike", and both reached through `web._Handler` -- a
    private name, so the copies could not even be called wrong, only kept in
    step by hand. Port 0 in both, so neither collides with a journal already
    serving on 8765.

    Unlike `serve()` this does not print, does not block, and does not refuse a
    non-loopback host, because it never binds one: 127.0.0.1 is hardcoded.
    """
    cfg = ServeConfig(
        db_path=db_path,
        archive_dir=archive_dir,
        query_id=query_id,
        assets=tuple(assets),
    )

    class _Ephemeral(http.server.ThreadingHTTPServer):
        daemon_threads = True
        address_family = socket.AF_INET

    httpd = _Ephemeral(("127.0.0.1", 0), partial(_Handler, cfg))
    port = httpd.socket.getsockname()[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()


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
