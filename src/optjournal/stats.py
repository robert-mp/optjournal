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

__all__ = ["DayPnl", "MonthStats", "available_months", "daily_series", "month_stats"]


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
    month: str            #: "YYYY-MM", or "ALL"
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
    def total_friction_base(self) -> float:
        return abs(self.commissions_base) + abs(self.fees_base) + abs(self.autofx_base)


def available_months(conn: sqlite3.Connection, asset_category: str | None = "OPT") -> list[str]:
    """Months with at least one trade, newest first."""
    where, params = ("WHERE asset_category = ?", (asset_category,)) if asset_category else ("", ())
    rows = conn.execute(f"SELECT DISTINCT trade_date FROM trades {where}", params).fetchall()
    months = {m for m in (_month_of(r["trade_date"]) for r in rows) if m}
    return sorted(months, reverse=True)


def daily_series(
    conn: sqlite3.Connection, month: str | None = None, asset_category: str | None = "OPT"
) -> list[DayPnl]:
    """Realised P&L and fill count per calendar day, ascending."""
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
        if day is None or (month and not day.startswith(month)):
            continue
        bucket = buckets.setdefault(day, DayPnl(day=day))
        bucket.trades += 1
        bucket.realized_base += row["fifo_pnl_realized_base"] or 0.0
    return [buckets[k] for k in sorted(buckets)]


def month_stats(
    conn: sqlite3.Connection,
    month: str | None = None,
    *,
    asset_category: str | None = "OPT",
    base_currency: str = "EUR",
) -> MonthStats:
    """Statistics for one month, or for everything when `month` is None."""
    stats = MonthStats(
        month=month or "ALL",
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
        if month and _month_of(row["trade_date"]) != month:
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
        if month and _month_of(row["date_time"]) != month:
            continue
        stats.fees_base += row["amount_base"] or 0.0

    report = build_history(conn, asset_category=asset_category, base_currency=base_currency)
    closed = [
        e for e in report.closed
        if not month or (_month_of(e.closed_at) == month)
    ]
    stats.closed_episodes = len(closed)
    stats.open_episodes = len(report.open)
    wins = [e.realized_pnl_base for e in closed if e.realized_pnl_base > 0]
    losses = [e.realized_pnl_base for e in closed if e.realized_pnl_base < 0]
    stats.wins, stats.losses = len(wins), len(losses)
    stats.avg_win_base = sum(wins) / len(wins) if wins else None
    stats.avg_loss_base = sum(losses) / len(losses) if losses else None

    stats.days = daily_series(conn, month, asset_category)
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
        "total_friction_base": stats.total_friction_base,
        "green_days": stats.green_days,
        "red_days": stats.red_days,
        "days": [
            {"day": d.day, "trades": d.trades, "realized_base": d.realized_base}
            for d in stats.days
        ],
    }
