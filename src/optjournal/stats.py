"""Monthly statistics and daily P&L series for the journal dashboard.

Kept separate from `analysis` (which measures account *costs* from a single
statement) and from `history` (which reconstructs position episodes). This
module answers the calendar-shaped questions a journal dashboard asks: what
happened in March, and what happened on the 14th.

Two deliberate choices about what gets counted, because the obvious approach
is wrong in both cases:

* **Realised P&L comes from `fifo_pnl_realized_base`, summed per day.** IBKR
  computes it per fill and it is already net of both opening and closing
  commission -- verified arithmetically in `history`. Deriving daily P&L by
  reconstructing episodes and then apportioning them across days would be
  more machinery for a worse answer, because an episode spanning three days
  has no principled daily split.

* **Win/loss counts come from episodes, not fills.** A round trip closed by
  two partial fills is one outcome, not two, so counting fills would inflate
  both the trade count and the win rate. `total_trades` counts fills because
  that is what "how many executions" means; the win/loss block counts
  episodes because that is what "did it work" means. The dashboard labels
  which is which rather than blurring them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from optjournal.history import build_history

__all__ = [
    "ALL_TRADES",
    "Cohort",
    "DayPnl",
    "MonthStats",
    "TradeScope",
    "annual_stats",
    "available_months",
    "available_years",
    "cohort_data",
    "daily_series",
    "month_stats",
    "monthly_stats",
    "odte_cohorts",
    "odte_scope",
    "scope_for",
]


def _in_period(value: str | None, period: str | None) -> bool:
    """Whether a stored date falls inside `period`, which may be a year.

    `period` is matched as a prefix of the normalised ISO day, so "2025-03"
    selects a month and "2025" a year with one predicate. That is what lets
    `month_stats` serve the Annual tab unchanged: the year figures come from
    the same summations, over the same columns, with the same episode-versus-
    fill distinctions as the monthly ones, so the two reconcile by
    construction rather than by two implementations agreeing.

    Prefix-matching the *normalised* day matters -- the raw column mixes ISO
    `2025-01-14 14:30:05` with IBKR's compact `20250114`, and the compact form
    would not match an ISO prefix.

    An empty period means "everything", so this returns True.
    """
    if not period:
        return True
    day = _day_of(value)
    return day is not None and day.startswith(period)


def _month_of(value: str | None) -> str | None:
    """YYYY-MM from a stored date, tolerating both ISO and IBKR compact forms."""
    if not value:
        return None
    text = str(value).strip().replace("/", "-")
    if len(text) >= 7 and text[4] == "-":
        return text[:7]
    if len(text) >= 6 and text[:6].isdigit():
        return f"{text[:4]}-{text[4:6]}"
    return None


def _day_of(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip().replace("/", "-").split(" ")[0].split("T")[0]
    if len(text) >= 10 and text[4] == "-":
        return text[:10]
    if len(text) >= 8 and text[:8].isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return None


@dataclass(slots=True)
class DayPnl:
    day: str
    trades: int = 0
    realized_base: float = 0.0

    @property
    def is_green(self) -> bool:
        return self.realized_base > 0

    @property
    def is_red(self) -> bool:
        return self.realized_base < 0


@dataclass(slots=True)
class MonthStats:
    month: str            #: "YYYY-MM", "YYYY" for an annual row, or "ALL"
    base_currency: str = "EUR"
    asset_category: str = "OPT"

    total_trades: int = 0          #: fills
    orders: int = 0
    net_pnl_base: float = 0.0      #: realised, already net of commission
    commissions_base: float = 0.0
    fees_base: float = 0.0

    #: Episode-derived, so a two-fill close counts once.
    closed_episodes: int = 0
    open_episodes: int = 0
    wins: int = 0
    losses: int = 0
    avg_win_base: float | None = None
    avg_loss_base: float | None = None

    #: Not derivable from an Activity statement: it carries no Net Asset Value.
    #: Enabling the "Equity Summary in Base" section on the Flex query template
    #: would supply it. None means "unavailable", not "zero".
    net_liq_base: float | None = None

    days: list[DayPnl] = field(default_factory=list)

    @property
    def win_rate(self) -> float | None:
        decided = self.wins + self.losses
        return None if not decided else self.wins / decided * 100.0

    @property
    def green_days(self) -> int:
        return sum(1 for d in self.days if d.is_green)

    @property
    def red_days(self) -> int:
        return sum(1 for d in self.days if d.is_red)

    @property
    def gain_pct_of_net_liq(self) -> float | None:
        if not self.net_liq_base:
            return None
        return self.net_pnl_base / self.net_liq_base * 100.0

    @property
    def options_friction_base(self) -> float:
        """Friction attributable to `asset_category`.

        Commission is charged per trade, so it carries an assetCategory and
        the ingest filter genuinely applies to it. This is the only friction
        figure on this panel that is scoped to the journalled instruments.
        """
        return abs(self.commissions_base)

    @property
    def account_friction_base(self) -> float:
        """Friction that cannot be attributed to `asset_category`.

        Fee rows carry no assetCategory at all -- market-data subscriptions
        and custody charges are levied on the account, not on a trade -- so
        ingest deliberately does not filter them and they cannot be
        apportioned to options without inventing the split. The AutoFX markup
        has the same problem: IBKR never ties a conversion back to the trade
        that caused it.

        Narrower than `CostReport.account_friction_base` despite the matching
        name: this panel reads the `asset_category`-filtered database, so it
        cannot see commission on other instruments at all, while the cost
        report reads the raw statement and includes it. Do not present the two
        under the same label -- they differ by the whole of stock commission.
        """
        return abs(self.fees_base)

    @property
    def total_friction_base(self) -> float:
        """Both scopes together.

        Presenting this single figure under a panel headed "options" was
        wrong: it labelled account-level fees as this journal's cost, which
        is the same defect the cost report carried. Read
        `options_friction_base` and `account_friction_base` instead wherever
        the scope is being claimed.
        """
        return self.options_friction_base + self.account_friction_base


@dataclass(frozen=True)
class TradeScope:
    """A subset of the journal, identified by the fills that belong to it.

    The Trade Types control is a scope selector, and a scope has to be defined
    at fill level because that is what every summation here iterates over. Both
    id sets `None` means "everything", which is the default and costs nothing.

    Membership is by fill, not by predicate re-evaluation, so the scope agrees
    with the cohort it came from by construction. In particular the 0DTE scope
    is *not* "every fill whose trade date equals its expiry": that would also
    catch the expiry-day close of a position held for a month, which is not
    0DTE trading. It is the fills of the round trips classified as 0DTE.
    """

    key: str
    label: str
    trade_ids: frozenset[str] | None = None
    order_ids: frozenset[str] | None = None

    @property
    def is_everything(self) -> bool:
        return self.trade_ids is None

    def has_trade(self, trade_id: Any) -> bool:
        return self.trade_ids is None or str(trade_id or "") in self.trade_ids

    def has_order(self, order_id: Any) -> bool:
        return self.order_ids is None or str(order_id or "") in self.order_ids

    def has_episode(self, episode: Any) -> bool:
        """In scope when any of the episode's own fills is.

        Any rather than all: a scope built from whole episodes contains all of
        their fills, so the two agree -- and a partial overlap should surface
        the episode rather than silently drop it.
        """
        if self.trade_ids is None:
            return True
        return any(str(t) in self.trade_ids for t in episode.trade_ids)


#: The default: no filtering at all, and no id sets to build or carry.
ALL_TRADES = TradeScope(key="all", label="Options")


def odte_scope(
    conn: sqlite3.Connection,
    *,
    asset_category: str | None = "OPT",
    base_currency: str = "EUR",
    report: Any = None,
) -> TradeScope:
    """The 0DTE round trips, as a fill-level scope.

    Includes open episodes as well as closed ones. An open 0DTE position is odd
    but reachable -- a statement cut on expiry day, before the contract died --
    and a scope that filters the journal should not hide a position the
    Positions tab would still show. `odte_cohorts` stays closed-only because
    comparing outcomes needs outcomes.
    """
    if report is None:
        report = build_history(
            conn, asset_category=asset_category, base_currency=base_currency
        )
    trade_ids = {str(t) for e in report.episodes if e.is_odte is True for t in e.trade_ids}
    # Orders are aggregated in a view keyed by ib_order_id, so filtering them
    # needs the order ids those fills belong to rather than the fills.
    order_ids = {
        str(row["ib_order_id"])
        for row in conn.execute(
            "SELECT trade_id, ib_order_id FROM trades WHERE ib_order_id IS NOT NULL"
        )
        if str(row["trade_id"]) in trade_ids
    }
    return TradeScope(
        key="odte",
        label="0DTE",
        trade_ids=frozenset(trade_ids),
        order_ids=frozenset(order_ids),
    )


#: Selectable scopes, by the key the UI and the `?type=` parameter use.
SCOPE_BUILDERS = {"all": None, "odte": odte_scope}


def scope_for(
    conn: sqlite3.Connection,
    key: str | None,
    *,
    asset_category: str | None = "OPT",
    report: Any = None,
) -> TradeScope:
    """Resolve a scope key, falling back to everything for anything unknown.

    Tolerant on purpose: the key arrives from a query parameter, and an
    unrecognised one should show the whole journal rather than fail.
    """
    builder = SCOPE_BUILDERS.get((key or "all").lower())
    if builder is None:
        return ALL_TRADES
    return builder(conn, asset_category=asset_category, report=report)


def available_months(
    conn: sqlite3.Connection,
    asset_category: str | None = "OPT",
    scope: TradeScope = ALL_TRADES,
) -> list[str]:
    """Months with at least one trade in scope, newest first.

    Scoped, so the month dropdown cannot offer a month that the active filter
    has emptied -- picking one would show a blank dashboard and look broken.
    """
    where, params = ("WHERE asset_category = ?", (asset_category,)) if asset_category else ("", ())
    rows = conn.execute(
        f"SELECT DISTINCT trade_date, trade_id FROM trades {where}", params
    ).fetchall()
    months = {
        m
        for m in (
            _month_of(r["trade_date"]) for r in rows if scope.has_trade(r["trade_id"])
        )
        if m
    }
    return sorted(months, reverse=True)


def available_years(
    conn: sqlite3.Connection, asset_category: str | None = "OPT"
) -> list[str]:
    """Years with at least one trade, newest first.

    Unscoped, unlike `available_months`: the only caller is the Annual tab,
    which shows no filter bar and so must not narrow. See `build_state`.
    """
    where, params = ("WHERE asset_category = ?", (asset_category,)) if asset_category else ("", ())
    rows = conn.execute(f"SELECT DISTINCT trade_date FROM trades {where}", params).fetchall()
    years = {d[:4] for d in (_day_of(r["trade_date"]) for r in rows) if d}
    return sorted(years, reverse=True)


def _period_stats(
    conn: sqlite3.Connection,
    periods: list[str],
    *,
    asset_category: str | None,
    base_currency: str,
) -> list[MonthStats]:
    """`month_stats` over several periods, sharing one episode history pass.

    The Annual tab asks for every month, every year and an all-time row at
    once. Each `month_stats` call otherwise rebuilds the whole episode history,
    so a thirteen-month archive did that fifteen times per page load for
    identical results.
    """
    report = build_history(
        conn, asset_category=asset_category, base_currency=base_currency
    )
    return [
        month_stats(
            conn, period, asset_category=asset_category,
            base_currency=base_currency, report=report,
        )
        for period in periods
    ]


def annual_stats(
    conn: sqlite3.Connection,
    *,
    asset_category: str | None = "OPT",
    base_currency: str = "EUR",
) -> list[MonthStats]:
    """One `MonthStats` per calendar year, newest first.

    Each year is `month_stats` over a year-wide period rather than a separate
    aggregation, so a change to how P&L or win rate is counted lands on the
    monthly and annual views together. A year with fills but nothing closed
    still gets a row: "traded, decided nothing" is a real outcome and hiding
    it would make the years stop accounting for all the activity.

    Takes no `TradeScope`, unlike `month_stats`. The Annual tab renders no
    filter bar, and a tab whose numbers move with a control it does not display
    leaves the reader nothing to explain the change with. A scope parameter here
    would be an unused hook inviting exactly that.
    """
    return _period_stats(
        conn, available_years(conn, asset_category),
        asset_category=asset_category, base_currency=base_currency,
    )


def monthly_stats(
    conn: sqlite3.Connection,
    *,
    asset_category: str | None = "OPT",
    base_currency: str = "EUR",
) -> list[MonthStats]:
    """One `MonthStats` per calendar month, newest first.

    The same rows the month selector produces one at a time, so the Annual
    tab's breakdown and the Dashboard agree for any month the reader checks --
    they are the same call with the same period string.

    Unscoped for the same reason as `annual_stats`: it feeds the Annual tab,
    which carries no filter.
    """
    return _period_stats(
        conn, available_months(conn, asset_category),
        asset_category=asset_category, base_currency=base_currency,
    )


@dataclass(slots=True)
class Cohort:
    """Outcome summary for a subset of closed episodes."""

    label: str
    episodes: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl_base: float = 0.0
    commission_base: float = 0.0
    contracts: int = 0

    @property
    def win_rate(self) -> float | None:
        decided = self.wins + self.losses
        return None if not decided else self.wins / decided * 100.0

    @property
    def avg_pnl_base(self) -> float | None:
        """Mean outcome per round trip, wins and losses together.

        The figure that answers "is this cohort worth trading": a high win
        rate with a worse average is a losing strategy, and the two numbers
        only mean something side by side.
        """
        return None if not self.episodes else self.net_pnl_base / self.episodes


def _cohort(label: str, episodes: list[Any]) -> Cohort:
    c = Cohort(label=label)
    for e in episodes:
        c.episodes += 1
        c.net_pnl_base += e.realized_pnl_base
        c.commission_base += abs(e.commission_base)
        c.contracts += e.contracts
        if e.realized_pnl_base > 0:
            c.wins += 1
        elif e.realized_pnl_base < 0:
            c.losses += 1
    return c


def odte_cohorts(
    conn: sqlite3.Connection,
    *,
    asset_category: str | None = "OPT",
    base_currency: str = "EUR",
    report: Any = None,
) -> tuple[Cohort, Cohort, int]:
    """0DTE round trips against everything else, plus the unclassifiable count.

    Returned as a pair because a 0DTE win rate in isolation says nothing --
    the question is always whether same-day expiries did better or worse than
    the rest of the book, and that needs both sides on screen.

    The third value counts closed episodes whose DTE cannot be determined,
    which is anything without an expiry. Reported rather than silently folded
    into "not 0DTE": a stock has no DTE, and claiming it was not a 0DTE trade
    is a different statement from admitting the question does not apply.
    """
    if report is None:
        report = build_history(
            conn, asset_category=asset_category, base_currency=base_currency
        )
    odte = [e for e in report.closed if e.is_odte is True]
    rest = [e for e in report.closed if e.is_odte is False]
    unknown = sum(1 for e in report.closed if e.is_odte is None)
    return _cohort("0DTE", odte), _cohort("Everything else", rest), unknown


def cohort_data(c: Cohort) -> dict[str, Any]:
    """JSON-safe view, including the computed properties `asdict` would drop."""
    return {
        "label": c.label,
        "episodes": c.episodes,
        "wins": c.wins,
        "losses": c.losses,
        "win_rate": c.win_rate,
        "net_pnl_base": c.net_pnl_base,
        "avg_pnl_base": c.avg_pnl_base,
        "commission_base": c.commission_base,
        "contracts": c.contracts,
    }


def daily_series(
    conn: sqlite3.Connection,
    period: str | None = None,
    asset_category: str | None = "OPT",
    scope: TradeScope = ALL_TRADES,
) -> list[DayPnl]:
    """Realised P&L and fill count per calendar day, ascending.

    `period` is a year or a month; see `_in_period`.
    """
    clauses, params = [], []
    if asset_category:
        clauses.append("asset_category = ?")
        params.append(asset_category)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    buckets: dict[str, DayPnl] = {}
    for row in conn.execute(
        f"SELECT trade_date, trade_id, fifo_pnl_realized_base FROM trades {where}", params
    ):
        day = _day_of(row["trade_date"])
        if day is None or not _in_period(row["trade_date"], period):
            continue
        if not scope.has_trade(row["trade_id"]):
            continue
        bucket = buckets.setdefault(day, DayPnl(day=day))
        bucket.trades += 1
        bucket.realized_base += row["fifo_pnl_realized_base"] or 0.0
    return [buckets[k] for k in sorted(buckets)]


def month_stats(
    conn: sqlite3.Connection,
    period: str | None = None,
    *,
    asset_category: str | None = "OPT",
    base_currency: str = "EUR",
    scope: TradeScope = ALL_TRADES,
    report: Any = None,
) -> MonthStats:
    """Statistics for one period, or for everything when `period` is None.

    `period` is a month ("2025-03") or a whole year ("2025"). Both widths run
    through the identical summations, which is what makes the Annual tab's
    figures reconcile with the monthly ones instead of being a second opinion.

    `scope` restricts every trade-derived figure to a subset of fills. `report`
    lets a caller building many periods reuse one `build_history` pass -- the
    Annual tab asks for a dozen months, two years and an all-time row on every
    page load, and rebuilding the episode history for each was the whole cost.
    """
    stats = MonthStats(
        month=period or "ALL",
        base_currency=base_currency,
        asset_category=asset_category or "ALL",
    )

    clauses, params = [], []
    if asset_category:
        clauses.append("asset_category = ?")
        params.append(asset_category)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    orders: set[str] = set()
    for row in conn.execute(
        f"SELECT trade_date, trade_id, ib_order_id, fifo_pnl_realized_base,"
        f" ib_commission_base FROM trades {where}", params
    ):
        if not _in_period(row["trade_date"], period):
            continue
        if not scope.has_trade(row["trade_id"]):
            continue
        stats.total_trades += 1
        if row["ib_order_id"]:
            orders.add(str(row["ib_order_id"]))
        stats.net_pnl_base += row["fifo_pnl_realized_base"] or 0.0
        stats.commissions_base += row["ib_commission_base"] or 0.0
    stats.orders = len(orders)

    # Fees are account-level CashTransaction rows, never trade-linked -- verified
    # against real data, where none of the 65 fee rows carries a conid or tradeID.
    # Deliberately NOT scoped: there is nothing to filter them on, and pro-rating
    # them into a fill subset would be inventing an attribution. Under an active
    # scope they stay the account's figure, which is what the pill already says.
    for row in conn.execute(
        "SELECT date_time, amount_base, type FROM cash_transactions"
        " WHERE UPPER(type) LIKE '%FEES%'"
    ):
        if not _in_period(row["date_time"], period):
            continue
        stats.fees_base += row["amount_base"] or 0.0

    if report is None:
        report = build_history(
            conn, asset_category=asset_category, base_currency=base_currency
        )
    # Attributed by close date, matching the monthly convention: an episode
    # opened in December and closed in January is a January outcome, and so a
    # 2026 one. Attributing by entry instead would make the annual rows stop
    # summing to the monthly ones.
    closed = [
        e for e in report.closed
        if _in_period(e.closed_at, period) and scope.has_episode(e)
    ]
    stats.closed_episodes = len(closed)
    stats.open_episodes = sum(1 for e in report.open if scope.has_episode(e))
    wins = [e.realized_pnl_base for e in closed if e.realized_pnl_base > 0]
    losses = [e.realized_pnl_base for e in closed if e.realized_pnl_base < 0]
    stats.wins, stats.losses = len(wins), len(losses)
    stats.avg_win_base = sum(wins) / len(wins) if wins else None
    stats.avg_loss_base = sum(losses) / len(losses) if losses else None

    stats.days = daily_series(conn, period, asset_category, scope)
    return stats


def stats_data(stats: MonthStats) -> dict[str, Any]:
    """JSON-safe view, including the computed properties `asdict` would drop."""
    return {
        "month": stats.month,
        "base_currency": stats.base_currency,
        "asset_category": stats.asset_category,
        "total_trades": stats.total_trades,
        "orders": stats.orders,
        "net_pnl_base": stats.net_pnl_base,
        "commissions_base": stats.commissions_base,
        "fees_base": stats.fees_base,
        "closed_episodes": stats.closed_episodes,
        "open_episodes": stats.open_episodes,
        "wins": stats.wins,
        "losses": stats.losses,
        "win_rate": stats.win_rate,
        "avg_win_base": stats.avg_win_base,
        "avg_loss_base": stats.avg_loss_base,
        "net_liq_base": stats.net_liq_base,
        "gain_pct_of_net_liq": stats.gain_pct_of_net_liq,
        "options_friction_base": stats.options_friction_base,
        "account_friction_base": stats.account_friction_base,
        "total_friction_base": stats.total_friction_base,
        "green_days": stats.green_days,
        "red_days": stats.red_days,
        "days": [
            {"day": d.day, "trades": d.trades, "realized_base": d.realized_base}
            for d in stats.days
        ],
    }
