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

import contextlib
import http.server
import ipaddress
import json
import logging
import re
import signal
import socket
import sqlite3
import threading
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from typing import Any

from optjournal import __version__, journal, replay
from optjournal import settings as prefs
from optjournal.analysis import analyse
from optjournal.archive import newest_statement
from optjournal.campaigns import position_count
from optjournal.clock import parse_day
from optjournal.costs import CostScope, build_costs
from optjournal.db import DEFAULT_BROKER, connect, open_journal
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
    last_fetch,
    load,
    read_token,
)
from optjournal.history import build_history
from optjournal.ingest import DEFAULT_ASSET_FILTER
from optjournal.iv import IvFetchError, fetch_iv_rank
from optjournal.iv import band as iv_band
from optjournal.jobs import (
    JOBS,
    JobBusy,
    Scheduler,
    UnknownJob,
    interrupted_runs,
    run_job,
)
from optjournal.jobs import (
    Context as JobContext,
)
from optjournal.marketdata import BarFetchError, fetch_quote
from optjournal.serialize import (
    audit_data,
    broker_costs_data,
    costs_data,
    history_data,
    jobs_data,
    journal_data,
    logbook_data,
    market_data,
    odte_context_data,
    orders_data,
    positions_data,
    statements_data,
    watchlist_data,
)
from optjournal.stats import (
    EQUITY_CATEGORY,
    EQUITY_TRADES,
    POSITION_SCORING,
    SCORINGS,
    annual_stats,
    available_months,
    campaigns_for,
    cohort_data,
    fx_quotes,
    month_range,
    month_stats,
    monthly_stats,
    odte_cohorts,
    odte_scope,
    scope_for,
    stats_data,
)
from optjournal.strategies import (
    position_groups,
    strategy_groups,
)
from optjournal.sync import sync_journal

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

#: How long `/api/settings/token` waits for the OS credential store before
#: answering "unreadable". Not a guess: `keyring.get_password` was measured on
#: this machine returning nothing at all within 10s while the keychain waited for
#: an unlock, so the read needs a deadline or the request never replies. Long
#: enough that a keychain which merely needs a moment still answers, short enough
#: that a button does not look stuck.
KEYRING_TIMEOUT_S = 4.0

#: How large a journal entry POST may be. Eight times `_body`'s default, because
#: eleven free-text fields -- two exit plans, two "why not", lessons, notes -- run
#: past 8KB for a reader who writes properly, and this is the one endpoint where
#: an over-long body must not be read as an empty one: `journal.save` deletes an
#: entry emptied of every field. See `_journal_write`, which refuses rather than
#: truncating.
JOURNAL_BODY_LIMIT = 65536

#: The keys `/api/journal` reads for itself. Everything else in the body is a
#: journal field, and `journal.FIELDS` is what judges it -- see `_journal_write`
#: on why this endpoint must not do its own filtering.
_JOURNAL_CONTROL = frozenset({"anchor", "broker"})

#: The watchlist columns a request may write, in the order the upsert names them.
#: A tuple rather than "whatever keys the body has", because these names are
#: interpolated into the SQL: what is writable is a decision of this module's, and
#: the derived and fetched figures on that table's tab have no column to write.
_WATCH_FIELDS = ("note", "earnings_on")


def _iv_ranks(symbols: list[str]) -> dict[str, Any]:
    """CBOE's IV rank per symbol, and the two absences told apart.

    THREE OUTCOMES, not two, which is the whole reason this is a function rather
    than a comprehension. A symbol can rank; or CBOE can decline to carry it, which
    is an ANSWER and belongs in `unranked`; or the request can fail, which is a
    FAILURE and belongs in `ranks_failed`. Collapsing the last two would let a
    server having a bad minute render as "this symbol has no options", which is a
    false statement about the reader's own watchlist. `iv.fetch_iv_rank` draws the
    same line one level down: None for a 403, raise for anything else.

    One dead symbol does not blank the others, matching `_quotes` above: the loop
    keeps going and the failure is named in the reply.

    `band` is computed HERE rather than in the page, so the cut point that the two
    filter chips are worded from lives once, in `iv.py`, beside the comment that
    says it is the reader's chosen line and not tastytrade's.
    """
    ranks: dict[str, Any] = {}
    unranked: list[str] = []
    ranks_failed: list[str] = []
    for symbol in symbols:
        try:
            got = fetch_iv_rank(symbol)
        except IvFetchError as exc:
            log.debug("iv rank %s failed: %s", symbol, exc)
            ranks_failed.append(symbol)
            continue
        if got is None:
            unranked.append(symbol)
            continue
        ranks[symbol] = {
            "rank": got.rank,
            "iv30": got.iv30,
            #: The bounds travel WITH the rank. The same 76.7 inside a two-point
            #: year is a different fact from one inside a seventy-point year, and
            #: without them on screen the figure cannot be reproduced or doubted.
            "low": got.low,
            "high": got.high,
            "band": iv_band(got.rank),
            #: CBOE's own stamp, not the moment of the fetch: outside market hours
            #: this endpoint keeps serving the last session's reading.
            "as_of": got.as_of,
        }
    return {"ranks": ranks, "unranked": unranked, "ranks_failed": ranks_failed}


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


#: What the Costs tab selects when the reader has not chosen. The journal's own
#: category, so the tab opens agreeing with every other tab rather than jumping to
#: an account-wide figure four times larger. Widening is one click, and the
#: unattributable fees are on screen at every scope, so nothing is hidden by it.
DEFAULT_COST_SCOPE = ("OPT",)

#: The Costs selector's fourth option. Not an asset category -- it is a subset of
#: options, classified per round trip -- so it resolves to fills rather than to a
#: `WHERE` on a column. Named here because the page, the query parameter and this
#: resolution must share one spelling.
ODTE_SELECTION = "0DTE"


def _broker_costs(
    conn: sqlite3.Connection,
    *,
    selection: list[str] | None,
    base_currency: str,
    report: Any,
    asset_category: str,
) -> dict[str, Any]:
    """The Costs tab's payload for one reader selection.

    Translates the wire vocabulary (a repeated `?cost=` parameter, four peer
    options) into the two independent narrowings `CostScope` models: asset
    categories, and an explicit fill set for 0DTE. They are different kinds of
    thing -- 0DTE is a subset of options rather than a sibling of them -- and
    this is the seam where that is reconciled, so neither the page nor the cost
    engine has to know both vocabularies.

    Selecting 0DTE alone means the 0DTE options, so the options category is
    implied: a fill set with no category would also admit a stock fill that
    happened to share an id, and asking for a subset of options is asking about
    options.
    """
    chosen = [str(c).upper() for c in (selection or ()) if str(c).strip()]
    if not chosen:
        chosen = list(DEFAULT_COST_SCOPE)
    wants_odte = ODTE_SELECTION in chosen
    categories = [c for c in chosen if c != ODTE_SELECTION]
    fill_ids = None
    if wants_odte:
        # Resolved by the episode layer, which owns the definition -- entry date
        # against expiry, per round trip. Recomputing it here would give the page
        # a second answer to the same question.
        fill_ids = odte_scope(
            conn, asset_category=asset_category, report=report
        ).trade_ids or frozenset()
        if not categories:
            categories = [asset_category]
    scope = CostScope.of(
        categories,
        fill_ids=fill_ids,
        subset=ODTE_SELECTION if wants_odte else "",
    )
    return broker_costs_data(
        build_costs(conn, scope=scope, base_currency=base_currency)
    )


def build_state(
    *,
    db_path: Path,
    archive_dir: Path,
    query_id: str | None,
    asset_category: str = "OPT",
    month: str | None = None,
    trade_type: str | None = None,
    cost_scope: list[str] | None = None,
    scoring: str | None = None,
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

    `scoring` is the exception that proves that rule rather than breaking it: it
    reaches EVERY period block, Annual and monthly included, because the control
    for it lives in the header beside the display currency and is therefore on
    screen wherever its effect is. A unit of account applied to one tab and not
    another would leave the Dashboard and the Annual table disagreeing about the
    same month -- which is the defect `campaigns.py` was written to remove, not
    one to reintroduce behind a toggle.
    """
    # The stored unit applies when the request names none, so a choice made in the
    # settings page survives a reload and a restart. An explicit request parameter
    # still wins: that is the page's own hash, i.e. what this reader last clicked.
    scoring = prefs.scoring(scoring)
    with open_journal(db_path) as conn:
        # RESOLVE ABANDONED RUNS FIRST, before anything reads `job_runs`.
        #
        # On page load rather than from a scheduled job, for the same reason the
        # perishable audit moved out of a cron: a watchdog that is itself scheduled
        # stops when the scheduler does, and this project has already watched three
        # cron jobs report health for two days while collecting nothing.
        #
        # The KERNEL answers it -- a `running` row whose per-job flock can be
        # acquired has no live holder, because flock releases on process death
        # including SIGKILL. No PID, no staleness threshold, and correct across
        # laptop sleep, where every wall-clock rule is wrong (44.6 hours of sleep
        # measured as excluded from `monotonic` on this machine). Four
        # sub-millisecond `LOCK_NB` attempts in the common case, and zero when no
        # row says `running`.
        interrupted_runs(conn, archive_dir=archive_dir)
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
        # The campaign linkage, built ONCE per report and handed to everything
        # that counts a decision: the lifecycle cards, the scoreboard, and the
        # open-position headline. One structure rather than three readings, which
        # is the point -- the Dashboard used to count a roll as two wins while
        # the Trades tab drew it as one card.
        #
        # Indexed against `report.episodes` verbatim, never a reordering of it: a
        # campaign holds positions into that exact list, and `month_stats`
        # resolves them the same way.
        view_episodes = view_report.episodes
        view_campaigns = campaigns_for(conn, view_category, view_episodes)
        # The Annual tab is unscoped and runs over the HOME category, so it needs
        # its own linkage whenever the view has been switched to equities.
        home_campaigns = (
            view_campaigns if view_category == asset_category
            else campaigns_for(conn, asset_category, report.episodes)
        )
        state: dict[str, Any] = {
            "version": __version__,
            "generated_at": _now(),
            # Developer-only surfaces on? Resolved per request from the env var or
            # the stored flag (`settings.dev`), never from anything the browser
            # sent -- so a page in another tab cannot turn it on over this
            # unauthenticated server. Off for every friend who never set it.
            "dev": prefs.dev(),
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
                            scope=scope, report=view_report,
                            campaign_list=view_campaigns, scoring=scoring)
            ),
            "all_time": stats_data(
                month_stats(conn, None, asset_category=view_category,
                            scope=scope, report=view_report,
                            campaign_list=view_campaigns, scoring=scoring)
            ),
            "positions": positions_data(conn),
            "orders": orders,
            # The same orders folded into the strategies they were placed
            # as -- a strangle sold as two same-second orders is one group.
            "strategies": strategy_groups(orders),
            # ... and further linked into position lifecycles: the open and
            # the close of one position share an episode, so they are one
            # card. The union is `campaigns.link`'s, shared with the scoreboard.
            "lifecycles": position_groups(
                orders,
                episodes=view_episodes,
                campaign_list=view_campaigns,
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
            # What the scheduler has done, and whether it is running at all.
            # Read-only here: the runner writes, the page renders.
            "scheduler": jobs_data(conn, now=datetime.now(UTC)),
            # Computed on EVERY load, unconditionally, and that is the design
            # rather than laziness: this used to be a cron, so the watchdog and
            # the thing it watched could stop together -- and did, for two days,
            # while three jobs reported `ok`. 2.95 ms measured on the real
            # journal, against a payload that already issues ~142 statements.
            "audit": audit_data(conn, now=datetime.now(UTC)),
            # The header's dateline. Deliberately outside `stats`/`all_time`:
            # how long the log has been kept is a fact about the JOURNAL, not
            # about a selected month or a trade-type scope, and a header figure
            # that moved with a filter the header does not display is the same
            # defect the Annual total was fixed for.
            "logbook": logbook_data(conn, today=date.today()),
            # What the reader wrote, by decision anchor. The whole map in one
            # payload because the Trades tab asks "was this written up" per card,
            # and the only table here that a re-ingest cannot rebuild is the one
            # worth reading in a single query.
            "journal": journal_data(conn),
        }
        # How many POSITIONS the open contracts form, which needs the campaign
        # grouping and so cannot be computed inside month_stats. Set on both
        # blocks because they share the Stats shape, and it is period-invariant
        # either way: the open book is the open book whatever month is selected.
        open_positions = position_count(
            view_campaigns, view_episodes, in_scope=scope.has_episode,
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
            stats_data(s) for s in annual_stats(
                conn, asset_category=asset_category,
                report=report, campaign_list=home_campaigns, scoring=scoring,
            )
        ]
        state["monthly"] = [
            stats_data(s) for s in monthly_stats(
                conn, asset_category=asset_category,
                report=report, campaign_list=home_campaigns, scoring=scoring,
            )
        ]
        # The Annual table's total row. Deliberately not `all_time`, which is the
        # Dashboard's figure and therefore scoped: under an active filter the
        # year rows stayed whole while that total shrank, so the table stopped
        # adding up -- destroying the one reconciliation it exists to show.
        state["annual_total"] = stats_data(
            month_stats(conn, None, asset_category=asset_category, report=report,
                        campaign_list=home_campaigns, scoring=scoring)
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
            # The pre-open planner: the day's expected range from the S&P close
            # and the VIX, both read from stored bars. None until a `bars` fetch
            # has landed them, and the page draws that absence as "run bars"
            # rather than an empty gauge -- it is context on top of the cohort
            # comparison, not a reason to blank the tab.
            "context": odte_context_data(conn, now=datetime.now(UTC)),
        }
        base_ccy = str(state["stats"].get("base_currency") or "")
        state["fx"] = {"base": base_ccy, "quotes": fx_quotes(conn, base_ccy)}
        replay.attach(conn, state)
        state["broker_costs"] = _broker_costs(
            conn,
            selection=cost_scope,
            base_currency=base_ccy or "EUR",
            report=report,
            asset_category=asset_category,
        )

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

    # WHERE the effective id came from, so the settings page can offer to edit it
    # only when editing would actually take effect. A `--query-id` flag or an
    # exported variable outranks the stored setting (see `settings.query_id`), and
    # a form that saved into a value something else overrides is a form that lies
    # about having worked.
    stored_qid = prefs.read().get("query_id")
    if query_id and query_id != (str(stored_qid).strip() if stored_qid else None):
        source = "override"
    elif query_id:
        source = "stored"
    else:
        source = "unset"
    state["settings"] = {
        "query_id": query_id,
        "query_id_source": source,
        "scoring": state["stats"]["scoring"],
        # Deliberately NOT a keyring lookup. `flex.read_token` reaches the OS
        # credential store, which has been measured at 8.2s on this machine when
        # the keychain needs unlocking -- once per page load, on every tab. The
        # page asks `GET /api/settings/token` from a button instead, so the cost
        # is paid by someone who wants the answer.
        "token": None,
    }
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
    """The `POST /api/sync` reply: `sync_journal` plus this endpoint's own shapes.

    Thin by design. The two `except` clauses are the whole reason it exists: the
    page needs `retry_after_s` as a number to render a countdown, and a cooldown
    is not an error the way a missing token is. `sync_journal` raises so that each
    caller can make that distinction in its own vocabulary.
    """
    try:
        with open_journal(db_path) as conn:
            return sync_journal(
                conn=conn, archive_dir=archive_dir, query_id=query_id, assets=assets,
            )
    except FetchCooldown as exc:
        return {
            "ok": False,
            "kind": "cooldown",
            "retry_after_s": exc.retry_after_s,
            "message": str(exc),
        }
    except TokenMissing as exc:
        return {"ok": False, "kind": "config", "message": str(exc)}


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
        # NOTHING HERE IS CACHEABLE. The page is read from disk per request and the
        # payload is a live brokerage account, so a cached copy is a stale copy in
        # both cases. Sent because the absence bit: with no headers at all a
        # browser may reuse the page indefinitely, and an edit to page.html then
        # appears to have no effect -- which cost real time during a fix, with the
        # server correctly serving new code to a tab still running the old.
        # Everything is loopback and a few KB, so there is no bandwidth to save.
        self.send_header("Cache-Control", "no-store, must-revalidate")
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
            ctype = STATIC_TYPES.get(asset.suffix)
            if name and ctype and root in asset.parents and asset.is_file():
                self._send(200, asset.read_bytes(), ctype)
            else:
                self._json(404, {"error": "not found"})
        elif path == "/api/state":
            try:
                self._json(200, build_state(
                    db_path=self.cfg.db_path,
                    archive_dir=self.cfg.archive_dir,
                    # RESOLVED per request, not taken from the frozen config: the
                    # settings page can save a new id while this process runs, and
                    # reading `cfg.query_id` here left the page showing "no query
                    # id" immediately after a save that had genuinely worked.
                    query_id=self._effective_query_id(),
                    month=(params.get("month") or [None])[0],
                    trade_type=(params.get("type") or [None])[0],
                    # Repeatable, so `?cost=OPT&cost=CASH` is a multi-select
                    # rather than a delimiter this layer has to invent and the
                    # page has to match. parse_qs already hands us the list.
                    cost_scope=params.get("cost"),
                    # Unvalidated here on purpose: `stats.scoring_or_default`
                    # owns the vocabulary, and a second check in this layer is a
                    # second place for the two to disagree about what a valid
                    # unit is.
                    scoring=(params.get("scoring") or [None])[0],
                ))
            except sqlite3.OperationalError as exc:
                self._json(500, {"error": f"database not readable: {exc}"})
        elif path == "/api/settings/token":
            self._json(*self._token_status())
        elif path == "/api/quotes":
            self._json(*self._quotes())
        elif path == "/api/jobs/run":
            self._json(*self._job_status(params))
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
                #: The company name rides HERE rather than in `/api/state`, and
                #: that is the split this route exists for: it is fetched fact,
                #: already inside the reply this request pays for, so it costs
                #: nothing extra -- while a per-symbol name on the state payload
                #: would spend a request on every page load and every tab switch.
                #: The consequence is designed for rather than hidden: a row shows
                #: its bare symbol until Refresh has run.
                "name": quote.name,
            }
        return 200, {
            "ok": True,
            "quotes": quotes,
            "failed": failed,
            #: IV ranks ride this route for the reason the route exists, and they
            #: are the most expensive thing on it: TWO requests per symbol, because
            #: CBOE serves the current implied vol and its trailing-year bounds from
            #: two different paths. So a Refresh on a six-symbol watchlist costs six
            #: quote requests plus twelve of these, which is why nothing on the state
            #: payload triggers it.
            #:
            #: A SEPARATE KEY, not a field on each quote, and the reason is
            #: provenance rather than tidiness: a quote is Yahoo's and a rank is
            #: CBOE's, and the page prints whose each figure is. Merging them into
            #: one per-symbol dict would put two sources under one name and make the
            #: attribution a thing the renderer has to remember rather than a thing
            #: the shape carries.
            **_iv_ranks(symbols),
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

    def _effective_query_id(self) -> str | None:
        """The id a sync would actually use, resolved per request.

        Read here rather than trusted from `self.cfg`, because the settings page
        can change it while this process runs and `ServeConfig` is frozen. The
        alternative was telling the reader to restart the server after saving,
        which for a setting this basic is not a workable answer.
        """
        return prefs.query_id(self.cfg.query_id)

    def _token_status(self) -> tuple[int, dict[str, Any]]:
        """Whether a Flex token is in the OS keyring. On demand, and bounded.

        Its own endpoint because the credential store is SLOW, and worse than
        slow: measured on this machine, `keyring.get_password` did not return AT
        ALL within 10s when the keychain wanted an unlock the caller could not
        answer. On `/api/state` that would hold the whole page hostage to a
        system dialog; here a button asks and a page load never does.

        Bounded by a thread with a deadline for the same measurement. A blocking
        read left the HTTP request open with no reply and the button spinning
        forever, so the wait is capped and a timeout is reported as
        `present: null` -- UNREADABLE, which is a different answer from missing.
        Telling someone their stored token is gone because a dialog was pending
        would send them to re-enter a credential that is already there.

        The worker is a daemon so a still-blocked read cannot keep the process
        alive; the OS resolves or cancels its own prompt in its own time.

        Reports PRESENCE, never the value, and never whether IBKR accepts it:
        only a real fetch can answer that, and that costs a request.
        """
        import getpass

        account = getpass.getuser()
        outcome: list[tuple[str, str]] = []

        def probe() -> None:
            try:
                read_token(account)
            except TokenMissing as exc:
                outcome.append(("absent", str(exc)))
            except Exception as exc:  # pragma: no cover - backend failures
                outcome.append(("error", str(exc)))
            else:
                outcome.append(("present", "a token is stored for this account"))

        worker = threading.Thread(target=probe, daemon=True)
        worker.start()
        worker.join(KEYRING_TIMEOUT_S)
        if not outcome:
            log.warning("keyring did not answer within %ss", KEYRING_TIMEOUT_S)
            return 200, {
                "ok": False, "kind": "keyring", "present": None,
                "account": account,
                "message": f"the OS keyring did not answer within "
                           f"{KEYRING_TIMEOUT_S}s, usually because it is waiting "
                           f"for you to unlock it. Check for a system prompt, or "
                           f"run `optjournal setup` in a terminal.",
            }
        kind, message = outcome[0]
        if kind == "error":  # pragma: no cover - backend failures
            log.warning("keyring unreadable: %s", message)
            return 200, {"ok": False, "kind": "keyring", "present": None,
                         "account": account, "message": message}
        return 200, {"ok": True, "kind": "token", "present": kind == "present",
                     "account": account, "message": message}

    def _settings_write(self) -> tuple[int, dict[str, Any]]:
        """Save preferences from the settings page.

        Only the keys `settings.update` knows, and it refuses the rest -- so a
        renamed field fails loudly here instead of silently storing a preference
        nothing reads.

        The token is NOT settable through this endpoint, and that is deliberate:
        it would put a brokerage credential in an HTTP body on a server with no
        authentication, where the browser would also keep it in form state and
        the request in devtools history. `optjournal setup` reads it from a
        no-echo prompt instead.
        """
        body = self._body()
        changes: dict[str, Any] = {}
        if "query_id" in body:
            raw = str(body.get("query_id") or "").strip()
            # Length- and shape-checked, not verified: only IBKR can say whether
            # a well-formed id exists, and an id that does not simply fails the
            # next fetch with a message that says so.
            if raw and (len(raw) > 32 or not raw.isdigit()):
                return 400, {"ok": False, "kind": "query_id",
                             "message": f"{raw!r} is not a Flex query id: "
                                        "Client Portal shows it as digits."}
            changes["query_id"] = raw or None
        if "scoring" in body:
            raw = str(body.get("scoring") or "").strip()
            if raw and raw not in SCORINGS:
                return 400, {"ok": False, "kind": "scoring",
                             "message": f"unknown scoreboard unit {raw!r}"}
            # The DEFAULT is stored as absence, matching the hash and the wire:
            # one spelling of "position" rather than two that can disagree.
            changes["scoring"] = None if raw in ("", POSITION_SCORING) else raw
        if not changes:
            return 400, {"ok": False, "kind": "empty",
                         "message": "no known setting in the request"}
        stored = prefs.update(**changes)
        return 200, {"ok": True, "kind": "settings", "stored": stored,
                     "query_id": self._effective_query_id()}

    def _watchlist_write(self) -> tuple[int, dict[str, Any]]:
        """Add or remove one watched symbol, and write the fields the body carries.

        One symbol per request rather than a submitted list, because the UI edits
        one row at a time and a list would need a merge rule (is an absent symbol
        a removal?) that nothing asks for.

        The symbol is upper-cased and length-checked but NOT validated against a
        ticker universe: this journal has no such list, and inventing one would
        reject a legitimate foreign listing. A symbol that does not exist simply
        stores a row with no bars, which the tab already renders as a dash.

        KEY-PRESENT SEMANTICS, and they are the fix rather than a convenience. A
        field ABSENT from the body is left alone; a field PRESENT and empty is
        written NULL. The two readings were previously collapsed by
        `str(note) if note else None`, which maps "" to None, which the upsert's
        `COALESCE(excluded.note, note)` then reads as "keep the old value" -- so a
        note could be set and never cleared, and the endpoint answered `ok` while
        doing nothing. Presence is what tells the two apart, and it has to be
        presence rather than emptiness because the add form deliberately sends no
        `note` key at all: that is what makes a bare re-add keep an existing note,
        which is behaviour with its own test.

        `earnings_on` is validated as a YYYY-MM-DD day and refused with kind
        `"date"` otherwise. A FORMAT check only, in the spirit of `_SYMBOL_OK`:
        this journal cannot know whether a company reports that day, and refusing
        a date for being implausible would be inventing a calendar it does not
        have. What it can know is that `27/08/2026` is not a day this journal
        writes, and that a countdown derived from `2026-13-45` would be arithmetic
        over something that does not exist -- see `clock.parse_day`.
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
        # The typed fields, from a LITERAL tuple rather than from the body's own
        # keys: the column names are interpolated into SQL below, so what may be
        # written is decided here and not by the caller.
        fields: dict[str, str | None] = {}
        for column in _WATCH_FIELDS:
            if column not in body:
                continue
            typed = str(body[column] or "").strip()
            if column == "earnings_on" and typed and parse_day(typed) is None:
                return 400, {
                    "ok": False, "kind": "date",
                    "message": f"{typed} is not a YYYY-MM-DD date. An earnings "
                               f"date is typed, so it is checked for spelling "
                               f"rather than against a calendar this journal "
                               f"does not have.",
                }
            # Blank means CLEAR. Stripped first, so a field emptied to spaces by a
            # textarea clears rather than storing whitespace that renders as a
            # value the reader cannot see or delete.
            fields[column] = typed or None
        with open_journal(self.cfg.db_path) as conn:
            if action == "remove":
                cursor = conn.execute(
                    "DELETE FROM watchlist WHERE symbol = ?", (symbol,))
                changed = cursor.rowcount
            else:
                # Only the columns the body actually carried are assigned, so an
                # absent one is untouched by construction rather than by a
                # COALESCE that cannot tell "" from missing. DO NOTHING when the
                # body carried none, which is the bare re-add.
                updates = ", ".join(
                    f"{column}=excluded.{column}"
                    for column in _WATCH_FIELDS if column in fields
                )
                conn.execute(
                    "INSERT INTO watchlist (symbol, note, earnings_on, added_at)"
                    " VALUES (:symbol, :note, :earnings_on, :added_at)"
                    " ON CONFLICT(symbol) DO "
                    + (f"UPDATE SET {updates}" if updates else "NOTHING"),
                    {"symbol": symbol, "added_at": _now(),
                     "note": fields.get("note"),
                     "earnings_on": fields.get("earnings_on")},
                )
                changed = 1
            conn.commit()
        return 200, {"ok": True, "kind": "watchlist", "action": action,
                     "symbol": symbol, "changed": changed}

    def _journal_write(self) -> tuple[int, dict[str, Any]]:
        """Write one decision's journal entry.

        The request carries the ANCHOR and the text, and nothing else. Which
        account the decision belongs to, which underlying, and when it opened are
        read here from the fills the anchor named -- broker facts, so a form has
        no business sending them and no way to send them wrong. It also means an
        anchor no fill matches is refused: the journal cannot hold writing about a
        decision this journal has never seen, and a row keyed on a typo would be
        invisible from every surface afterwards.

        KEY-PRESENT SEMANTICS, matching `_watchlist_write` and for the same
        reason: a field absent from the body is left alone, a field present and
        empty is cleared. That is what lets the entry form and the close review be
        two surfaces without either erasing the other's fields -- a whole-row
        write would mean the review blanks the plan it is reviewing.

        THE BODY LIMIT IS RAISED, and that is not a nicety. `_body` answers `{}`
        for a body over its cap, which here would parse as "no fields", which
        `journal.save` reads as an entry emptied -- so a reader whose lessons ran
        long would get `ok` back and find their writing deleted. The length is
        therefore checked explicitly and refused with 413, because this is the one
        table where a silent loss is unrecoverable: everything else in the
        database can be rebuilt from `raw/`.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > JOURNAL_BODY_LIMIT:
            return 413, {
                "ok": False, "kind": "too-long",
                "message": f"that entry is {length:,} bytes, over the "
                           f"{JOURNAL_BODY_LIMIT:,} this endpoint accepts. Nothing "
                           f"was saved and nothing was changed -- shorten it and "
                           f"save again, or the text would have been lost.",
            }
        body = self._body(limit=JOURNAL_BODY_LIMIT)
        anchor = str(body.get("anchor") or "").strip()
        broker = str(body.get("broker") or DEFAULT_BROKER).strip()
        if not anchor:
            return 400, {"ok": False, "kind": "anchor",
                         "message": "no decision named: this position has no "
                                    "fills in the archive, so there is no order "
                                    "to attach an entry to."}
        # Everything except this endpoint's own control keys, PASSED THROUGH rather
        # than filtered to `journal.FIELDS`. Filtering here would drop a name the
        # journal does not declare and answer `ok` -- so a typo in the page's form
        # would post successfully and the text would never be seen again, which for
        # this table means gone, since nothing can re-derive it. `journal.save`
        # decides what is writable, in one place, and refuses the rest loudly.
        values = {k: v for k, v in body.items() if k not in _JOURNAL_CONTROL}
        with open_journal(self.cfg.db_path) as conn:
            target = conn.execute(
                "SELECT account_id,"
                # Options carry the underlying; equities ARE it.
                " COALESCE(underlying_symbol, symbol) AS underlying,"
                " MIN(trade_date) AS opened_on"
                " FROM trades WHERE broker = ? AND ib_order_id = ?",
                (broker, anchor),
            ).fetchone()
            if target is None or target["account_id"] is None:
                return 404, {
                    "ok": False, "kind": "anchor",
                    "message": f"no fill in this journal was placed under order "
                               f"{anchor}, so there is no decision to write up.",
                }
            try:
                entry = journal.save(
                    conn, anchor, account_id=target["account_id"], values=values,
                    underlying_symbol=target["underlying"],
                    opened_on=target["opened_on"], broker=broker,
                )
            except journal.JournalError as exc:
                return 400, {"ok": False, "kind": "journal", "message": str(exc)}
        return 200, {
            "ok": True, "kind": "journal", "anchor": anchor,
            # None when the write emptied the entry, which the page renders as an
            # un-journalled card again rather than leaving a stale badge on it.
            "entry": entry.payload() if entry else None,
        }

    def _job_run(self) -> tuple[int, dict[str, Any]]:
        """Run one registered job now. The page's only write to the scheduler.

        The target is in the request BODY, not the path, matching `/api/watchlist`.
        That is what keeps `do_POST`'s routing to exact string comparisons, which is
        in turn what makes the `Origin` guard's position ahead of the router a
        STRUCTURAL guarantee for every endpoint added later rather than something
        each new route has to remember. A path like `/api/jobs/run/sync` would need
        prefix matching, and a prefix match is where an unguarded route hides.

        Returns 202, not 200: the run has completed by the time this returns (there
        is no worker thread until step 6), but the status the caller wants is in the
        ledger row, and `GET /api/jobs/run?id=` is where it lives. Answering 200
        with a body would invite the page to read an outcome from the wrong place.
        """
        body = self._body()
        name = str(body.get("job") or "").strip()
        try:
            with open_journal(self.cfg.db_path) as conn:
                run_id = run_job(
                    conn, name,
                    ctx=JobContext(
                        archive_dir=self.cfg.archive_dir,
                        db_path=self.cfg.db_path,
                        query_id=self.cfg.query_id,
                        assets=self.cfg.assets,
                    ),
                )
        except UnknownJob:
            return 400, {
                "ok": False, "kind": "unknown",
                "message": f"no job named {name or '(empty)'}",
                "jobs": [job.name for job in JOBS],
            }
        except JobBusy as exc:
            # 409 with the run to watch, so the page polls the run already in
            # flight instead of showing an error for a working system.
            return 409, {"ok": False, "kind": "busy", "job": exc.job,
                         "run_id": exc.run_id,
                         "message": f"{exc.job} is already running"}
        return 202, {"ok": True, "kind": "queued", "job": name, "run_id": run_id}

    def _job_status(self, params: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
        """One ledger row by id. Read-only, no migrate, ~1 ms.

        NO MIGRATE, deliberately: this is polled every second or two while a job
        runs, and `migrate` takes the cross-process flock the job's own writes need.
        `open_journal` is not used for the same reason.
        """
        try:
            run_id = int((params.get("id") or ["0"])[0])
        except ValueError:
            return 400, {"ok": False, "kind": "id", "message": "id must be an integer"}
        conn = connect(self.cfg.db_path)
        try:
            row = conn.execute(
                "SELECT id, job, fired_for, started_at, finished_at, status,"
                " detail, done, total FROM job_runs WHERE id = ?", (run_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return 404, {"ok": False, "kind": "missing",
                         "message": f"no run {run_id}"}
        return 200, {"ok": True, "kind": "run", **dict(row)}

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
        try:
            self._route_post(path)
        except sqlite3.OperationalError as exc:
            # A LOCKED DATABASE OTHERWISE ANSWERS NOTHING AT ALL, and that was
            # measured rather than assumed: `do_POST` caught nothing, and
            # `BaseHTTPRequestHandler` has no error handler, so the exception
            # escaped the handler and the connection was dropped. A real request
            # against a journal held by `BEGIN EXCLUSIVE` got
            # `RemoteDisconnected: Remote end closed connection without response`
            # after 16.06s -- one BUSY_TIMEOUT_MS -- while the same journal
            # answered `GET /api/state` in 0.03s, because WAL lets readers through.
            # The page then shows a browser network error, which names neither the
            # cause nor the fact that waiting would fix it.
            #
            # Guarded HERE, before routing, for the same reason as the `Origin`
            # check: every endpoint added later inherits it instead of remembering.
            log.warning("POST %s hit a locked database: %s", path, exc)
            self._json(503, {
                "ok": False, "kind": "busy",
                "message": "the journal is locked by another writer; try again in "
                           "a moment.",
            })
            return

    def _route_post(self, path: str) -> None:
        """The routing itself, so `do_POST` can wrap all of it in one guard."""
        if path == "/api/market/fetch":
            self._json(*self._market_fetch())
            return
        if path == "/api/watchlist":
            self._json(*self._watchlist_write())
            return
        if path == "/api/journal":
            self._json(*self._journal_write())
            return
        if path == "/api/jobs/run":
            self._json(*self._job_run())
            return
        if path == "/api/settings":
            self._json(*self._settings_write())
            return
        if path != "/api/sync":
            self._json(404, {"error": "not found"})
            return
        query_id = self._effective_query_id()
        if not query_id:
            self._json(400, {
                "ok": False, "kind": "config",
                "message": "No Flex query ID configured. Set one in Settings, "
                           "or run `optjournal setup`.",
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
                query_id=query_id,
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
    scheduler: bool = True,
) -> None:
    """Serve the UI until interrupted. Loopback only, by construction.

    `scheduler=True` starts the 60-second reconciler in this process, which is what
    makes `serve` the application rather than a viewer. In-process for one measured
    reason: the IBKR fetch cooldown is a check-then-act guard on a hard lockout
    budget, so the scheduled sync and the browser's Sync button being two threads in
    one process beats two blind processes sharing a file.
    """
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
    clock = Scheduler(ctx=JobContext(
        archive_dir=archive_dir, db_path=db_path, query_id=query_id,
        assets=tuple(assets),
    )) if scheduler else None

    with _Server((host, port), partial(_Handler, cfg)) as httpd:
        actual = httpd.socket.getsockname()[1]
        print(f"optjournal UI on http://{host}:{actual}")
        print("  loopback only, no authentication -- do not expose this port")
        if not query_id:
            # NAMES THE SCHEDULER, not just the button. The button being disabled is
            # visible in the page; the sync JOB failing on every due tick is only
            # visible to someone who opens the ledger, and that is the shape this
            # went wrong in -- a supervised serve logged `failed -- no Flex query id
            # configured` for as long as it ran while `optjournal sync` in a shell
            # worked, because only the CLI read $OPTJOURNAL_QUERY_ID.
            print("  no query id (--query-id or $OPTJOURNAL_QUERY_ID):"
                  " Sync now is disabled")
            if scheduler:
                print("  and the scheduled sync job will fail on every tick")
        if clock is None:
            print("  scheduler OFF (--no-scheduler): nothing runs unless you press it")
        else:
            clock.start()
            print(f"  scheduler on, {clock.tick_s}s tick -- Collection shows what it did")

        # SERVE_FOREVER ON A THREAD, MAIN THREAD BLOCKED ON AN EVENT, and this
        # shape is forced rather than stylistic.
        #
        # The natural reading -- `serve_forever()` on the main thread plus a
        # `SIGTERM` handler calling `httpd.shutdown()` -- DEADLOCKS. Reproduced from
        # first principles with the bare stdlib:
        #
        #     serve_forever on the MAIN thread:  HANDLER ENTERED, then still alive
        #                                        3s later; shutdown() never returned
        #     serve_forever on a THREAD:         SHUTDOWN RETURNED, exited in 1.0s
        #
        # CPython delivers signals on the main thread, so the handler interrupts
        # `serve_forever` and then calls `shutdown()`, which waits on an event only
        # `serve_forever` can set. `socketserver`'s own docstring says it: "This must
        # be called while serve_forever() is running in another thread, or it will
        # deadlock."
        #
        # The consequence is worse than a slow exit BECAUSE this process now holds a
        # scheduler: `clock.stop()` below would never run, so the heartbeat would
        # keep advancing while the listener was dead -- inverting the one honesty
        # signal this whole plan was built to provide. The port would also stay
        # LISTEN-bound, so launchd's respawn fails with Errno 48 and its
        # `ExitTimeOut` eventually SIGKILLs, orphaning the in-flight run's `running`
        # row on every NORMAL stop.
        #
        # `serve_ephemeral` has always had this shape, which is why the suite never
        # saw the problem: it exercised the safe arrangement and shipped the unsafe
        # one.
        stop = threading.Event()

        def _bye(signum: int, _frame: Any) -> None:
            print(f"\nsignal {signum}, stopping")
            stop.set()

        # Installed only when this is the main thread. `signal.signal` raises
        # ValueError elsewhere, and `serve` is importable and callable from a test.
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(ValueError):
                signal.signal(sig, _bye)

        threading.Thread(target=httpd.serve_forever, name="optjournal-http",
                         daemon=True).start()
        try:
            stop.wait()
        except KeyboardInterrupt:
            print("\nstopped")
        finally:
            # ORDER MATTERS: the scheduler stops first, so no tick is mid-write when
            # the listener goes away, and joined rather than abandoned -- a tick
            # writing to a journal the caller is about to move is the kind of race
            # that shows up once.
            if clock is not None:
                clock.stop()
            httpd.shutdown()


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

    IT NEVER STARTS THE SCHEDULER, and that is a safety property rather than a
    convenience. `tests/conftest.py` points `RAW_DIR` at the LIVE `raw/` directory,
    and six call sites -- four in `tests/test_web.py`, one in `test_rendered.py`, one
    in `sweep.py` -- pass it here. A scheduler started by default would let the test
    suite fire real IBKR fetches against the real archive and the real
    `.fetch-state.json`, spending a rate-limited budget on a `pytest` run. There is
    deliberately no parameter to turn it on: a test that wants the loop constructs
    `jobs.Scheduler` directly against a scratch database, which is explicit at the
    call site and cannot be defaulted wrong.
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

#: What `/static/` will serve, by extension. An allowlist rather than a lookup
#: through `mimetypes`, and the reason is the 404 above: an extension absent
#: here is not served at all. The handler used to answer `text/javascript` for
#: every file it held, which was true while the directory held one .js module
#: and would have shipped the favicon as a script the moment a second file
#: arrived -- `nosniff` is set, so the browser would have refused it rather
#: than guessing. Adding a type is a deliberate act, which is the point.
STATIC_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    # The stylesheet, extracted from page.html's <style> block. `nosniff` is set,
    # so the type has to be right or the browser drops it and the page renders
    # unstyled -- which is the loud failure this allowlist is for.
    ".css": "text/css; charset=utf-8",
}


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
