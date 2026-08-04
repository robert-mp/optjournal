"""Grouping orders into the strategies they were placed as.

IBKR reports what happened at order granularity, but a strategy is often
several orders: this account's first strangle was sold as two orders (a call
and a put) filled within the same second, each with its own ib_order_id --
so no amount of order-level grouping can ever show it as one position. This
module groups *orders* into *strategy groups* and names the shape.

Grouping rule, stated so its limits are visible: consecutive orders on the
same single underlying whose first fills are within `WINDOW_S` seconds are
one strategy. Legs of a multi-leg position are placed together -- that is
what makes them a strategy rather than two opinions -- while distinct
strategies on the same underlying minutes apart stay separate. An order whose
legs span several underlyings (rare, but IBKR can report combo exercises that
way) is never merged with anything: the heuristic only trusts itself on
single-underlying orders.

Classification is derived from the combined legs, never asserted from order
count: a strangle is put+call, same expiry, same side, different strikes --
whether it arrived as one combo order or two singles. Mixed open/close legs
are a roll (one leg closes an expiry, another opens the next), which is a
statement about the *fills*, not about position lineage: the episode model in
`history.py` still ends one episode and starts another, unlinked.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

Row = dict[str, Any]

#: Orders on the same underlying with first fills inside this window are one
#: strategy. Same-second for the real strangle; 90s tolerates a combo split
#: into legs that fill as the market moves, without swallowing a deliberate
#: second trade placed minutes later.
WINDOW_S = 90


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _underlying(order: Row) -> str | None:
    """The order's single underlying, or None when it has none or several."""
    names = {
        str(leg.get("underlying_symbol") or "") for leg in order.get("legs", ())
    } - {""}
    return names.pop() if len(names) == 1 else None


def classify(legs: list[Row]) -> str:
    """Name the shape the combined legs form.

    Derived, not assumed: the same rules apply whether the legs arrived as
    one combo order or as several single-leg orders grouped by time.
    """
    if not legs:
        return "Empty"
    oc = {str(leg.get("open_close") or "").upper() for leg in legs}
    if len(oc) > 1:
        return "Roll"
    closing = oc == {"C"}
    suffix = " close" if closing else ""
    if len(legs) == 1:
        return "Single leg"

    rights = {str(leg.get("put_call") or "").upper() for leg in legs}
    strikes = {leg.get("strike") for leg in legs}
    expiries = {leg.get("expiry") for leg in legs}
    sides = {str(leg.get("buy_sell") or "").upper() for leg in legs}

    if len(legs) == 2:
        if rights == {"P", "C"} and len(sides) == 1 and len(expiries) == 1:
            return ("Straddle" if len(strikes) == 1 else "Strangle") + suffix
        if len(rights) == 1 and len(expiries) == 1 and len(strikes) == 2:
            name = "Put" if rights == {"P"} else "Call"
            return f"{name} vertical{suffix}"
        if len(rights) == 1 and len(expiries) == 2:
            kind = "calendar" if len(strikes) == 1 else "diagonal"
            name = "Put" if rights == {"P"} else "Call"
            return f"{name} {kind}{suffix}"
    if len(legs) == 4 and rights == {"P", "C"}:
        return ("Iron butterfly" if len(strikes) == 3 else "Iron condor") + suffix
    return f"{len(legs)}-leg combo{suffix}"


def strategy_groups(orders: list[Row]) -> list[Row]:
    """Orders folded into the strategies they were placed as, newest first.

    Input is `orders_data` output (each order carrying its legs); output rows
    keep every order intact under `orders` so the view can show the strategy
    as one position with its constituent legs beneath. Totals are sums of the
    member orders' totals, so a group of one is exactly its order.
    """
    def sort_key(o: Row):
        return (_underlying(o) or f"\uffff{o.get('ib_order_id')}",
                str(o.get("first_fill_at") or ""))

    groups: list[list[Row]] = []
    for order in sorted(orders, key=sort_key):
        last = groups[-1][-1] if groups else None
        same = False
        if last is not None:
            u_now, u_prev = _underlying(order), _underlying(last)
            t_now, t_prev = _dt(order.get("first_fill_at")), _dt(last.get("first_fill_at"))
            same = (
                u_now is not None
                and u_now == u_prev
                and t_now is not None
                and t_prev is not None
                and abs((t_now - t_prev).total_seconds()) <= WINDOW_S
            )
        if same:
            groups[-1].append(order)
        else:
            groups.append([order])

    out: list[Row] = []
    for members in groups:
        legs = [leg for o in members for leg in o.get("legs", ())]
        out.append({
            "underlying": _underlying(members[0]) or members[0].get("underlyings"),
            "label": classify(legs),
            "order_ids": [str(o.get("ib_order_id")) for o in members],
            "first_fill_at": min(str(o.get("first_fill_at") or "") for o in members),
            "fills": sum(o.get("fills") or 0 for o in members),
            "proceeds_base": sum(o.get("proceeds_base") or 0.0 for o in members),
            "commission_base": sum(o.get("commission_base") or 0.0 for o in members),
            "realized_pnl_base": sum(o.get("realized_pnl_base") or 0.0 for o in members),
            "orders": members,
        })
    out.sort(key=lambda g: g["first_fill_at"], reverse=True)
    return out
