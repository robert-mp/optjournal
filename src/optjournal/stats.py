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
from datetime import date
from typing import Any

from optjournal.history import build_history

__all__ = [
    "Cohort",
    "DayPnl",
    "MonthStats",
    "annual_stats",
    "available_months",
    "available_years",
    "cohort_data",
    "daily_series",
    "month_stats",
    "odte_cohorts",
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
    autofx_base: float = 0.0

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
        return abs(self.fees_base) + abs(self.autofx_base)

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


def available_months(conn: sqlite3.Connection, asset_category: str | None = "OPT") -> list[str]:
    """Months with at least one trade, newest first."""
    where, params = ("WHERE asset_category = ?", (asset_category,)) if asset_category else ("", ())
    rows = conn.execute(f"SELECT DISTINCT trade_date FROM trades {where}", params).fetchall()
    months = {m for m in (_month_of(r["trade_date"]) for r in rows) if m}
    return sorted(months, reverse=True)


def available_years(conn: sqlite3.Connection, asset_category: str | None = "OPT") -> list[str]:
    """Years with at least one trade, newest first."""
    where, params = ("WHERE asset_category = ?", (asset_category,)) if asset_category else ("", ())
    rows = conn.execute(f"SELECT DISTINCT trade_date FROM trades {where}", params).fetchall()
    years = {d[:4] for d in (_day_of(r["trade_date"]) for r in rows) if d}
    return sorted(years, reverse=True)


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
    """
    return [
        month_stats(
            conn, year, asset_category=asset_category, base_currency=base_currency
        )
        for year in available_years(conn, asset_category)
    ]


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
    report = build_history(conn, asset_category=asset_category, base_currency=base_currency)
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
    conn: sqlite3.Connection, period: str | None = None, asset_category: str | None = "OPT"
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
        f"SELECT trade_date, fifo_pnl_realized_base FROM trades {where}", params
    ):
        day = _day_of(row["trade_date"])
        if day is None or not _in_period(row["trade_date"], period):
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
) -> MonthStats:
    """Statistics for one period, or for everything when `period` is None.

    `period` is a month ("2025-03") or a whole year ("2025"). Both widths run
    through the identical summations, which is what makes the Annual tab's
    figures reconcile with the monthly ones instead of being a second opinion.
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
        f"SELECT trade_date, ib_order_id, fifo_pnl_realized_base, ib_commission_base"
        f" FROM trades {where}", params
    ):
        if not _in_period(row["trade_date"], period):
            continue
        stats.total_trades += 1
        if row["ib_order_id"]:
            orders.add(str(row["ib_order_id"]))
        stats.net_pnl_base += row["fifo_pnl_realized_base"] or 0.0
        stats.commissions_base += row["ib_commission_base"] or 0.0
    stats.orders = len(orders)

    # Fees are account-level CashTransaction rows, never trade-linked -- verified
    # against real data, where none of the 65 fee rows carries a conid or tradeID.
    for row in conn.execute(
        "SELECT date_time, amount_base, type FROM cash_transactions"
        " WHERE UPPER(type) LIKE '%FEES%'"
    ):
        if not _in_period(row["date_time"], period):
            continue
        stats.fees_base += row["amount_base"] or 0.0

    report = build_history(conn, asset_category=asset_category, base_currency=base_currency)
    # Attributed by close date, matching the monthly convention: an episode
    # opened in December and closed in January is a January outcome, and so a
    # 2026 one. Attributing by entry instead would make the annual rows stop
    # summing to the monthly ones.
    closed = [e for e in report.closed if _in_period(e.closed_at, period)]
    stats.closed_episodes = len(closed)
    stats.open_episodes = len(report.open)
    wins = [e.realized_pnl_base for e in closed if e.realized_pnl_base > 0]
    losses = [e.realized_pnl_base for e in closed if e.realized_pnl_base < 0]
    stats.wins, stats.losses = len(wins), len(losses)
    stats.avg_win_base = sum(wins) / len(wins) if wins else None
    stats.avg_loss_base = sum(losses) / len(losses) if losses else None

    stats.days = daily_series(conn, period, asset_category)
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
        "autofx_base": stats.autofx_base,
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
