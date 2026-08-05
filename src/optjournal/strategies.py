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

from optjournal.money import FILL_MONEY_FIELDS, Money

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
    """The order's single underlying, or None when it has none or several.

    A stock's underlying IS the stock, so `symbol` is the fallback rather than
    a guess: reading only `underlying_symbol` left every equities lifecycle
    nameless, and the Trades tab rendered its card heading as a literal "?"
    for a position whose ticker sat one column away. Real IBKR data happens to
    populate `underlyingSymbol` on stock rows, which is why this surfaced only
    on the synthetic journal -- exactly the kind of gap demo data should be
    exposing rather than hiding.
    """
    names = {
        str(leg.get("underlying_symbol") or leg.get("symbol") or "")
        for leg in order.get("legs", ())
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
        # Named from the leg itself, like every other shape here -- IBKR
        # sends no strategy field to read (verified against the raw
        # statements: origOrderID/origTradeID/relatedTradeID/
        # volatilityOrderLink are all empty on option trades, and no
        # combo/strategy vocabulary exists anywhere in a statement).
        # Direction is the POSITION'S, not the fill's: a BUY that closes
        # closes a short, so the buyback of a short put reads "Short put
        # close", never "Long put". A leg without a right (a stock leg in
        # the equities view) or without an open/close marker keeps the old
        # generic label rather than guessing a direction.
        leg = legs[0]
        right = {"P": "put", "C": "call"}.get(str(leg.get("put_call") or "").upper())
        side = str(leg.get("buy_sell") or "").upper()
        oc_leg = str(leg.get("open_close") or "").upper()
        if right is None or side not in ("BUY", "SELL") or oc_leg not in ("O", "C"):
            # The suffix still applies. Dropping it here was a real defect
            # rather than a cosmetic one: a stock leg never has a right, so
            # every equities lifecycle gave its opening AND its closing event
            # the identical label "Single leg", and the Trades tab decides an
            # event's caption by comparing the event label to the lifecycle's
            # ("X" -> Opened, "X close" -> Closed). Both matched the first
            # case, so a share SALE that closed the position was captioned
            # "Opened" while its own action chip beside it read STC. The
            # direction is what cannot be guessed without a right; whether
            # the order closed is known from open_close alone.
            return f"Single leg{suffix}"
        opened_long = (side == "BUY") == (oc_leg == "O")
        return f"{'Long' if opened_long else 'Short'} {right}{suffix}"

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
            # Aggregated from `legs` -- the leaf fill rows already in hand --
            # rather than by summing the orders' own figures. The base is the
            # same either way, but the gate must be asked against the union of
            # THESE legs' currencies: a group whose legs span currencies has no
            # exact figure, and re-gating an already-gated order total cannot
            # tell a withheld native from an absent one.
            **{f: Money.from_rows(legs, f).payload() for f in FILL_MONEY_FIELDS},
            "orders": members,
        })
    out.sort(key=lambda g: g["first_fill_at"], reverse=True)
    return out


def position_groups(
    orders: list[Row],
    *,
    episodes: list[Any],
    trade_to_order: dict[str, str],
) -> list[Row]:
    """Strategy events linked into position lifecycles, newest first.

    An opened-then-closed single leg is one position, not two trades -- and
    the accounting layer already knows it: `history.py` FIFO-matches fills
    into per-conid episodes, so the open and the close share an episode. The
    linkage here is exact, not heuristic: episode.trade_ids -> the fills'
    ib_order_id (via `trade_to_order`) -> the strategy event that order
    belongs to. Events sharing any episode are one lifecycle.

    Two deliberate consequences:

    * A re-opened contract later is a NEW episode, so it starts a new
      lifecycle rather than reviving the old card.
    * A roll event (mixed open/close legs) shares an episode with the old
      lifecycle AND opens a new episode -- the union links the whole chain
      into one campaign card. That is the intended reading of a roll: one
      continuing decision, with each episode's P&L still landing in its own
      close month underneath.

    Events whose orders map to no episode (nothing but snapshots, or an
    unmatched category) stay as singleton lifecycles.
    """
    events = strategy_groups(orders)

    order_to_eps: dict[str, set[int]] = {}
    for idx, ep in enumerate(episodes):
        for tid in getattr(ep, "trade_ids", ()) or ():
            oid = trade_to_order.get(str(tid))
            if oid is not None:
                order_to_eps.setdefault(oid, set()).add(idx)

    def event_eps(event: Row) -> set[int]:
        eps: set[int] = set()
        for oid in event.get("order_ids", ()):
            eps |= order_to_eps.get(str(oid), set())
        return eps

    # Union events sharing episodes. Tiny n, so the quadratic sweep is
    # clearer than a union-find and costs nothing.
    clusters: list[tuple[set[int], list[Row]]] = []
    for event in events:
        eps = event_eps(event)
        merged: tuple[set[int], list[Row]] | None = None
        for cluster in clusters:
            if eps and cluster[0] & eps:
                cluster[0].update(eps)
                cluster[1].append(event)
                merged = cluster
                break
        if merged is None:
            clusters.append((set(eps), [event]))

    out: list[Row] = []
    for ep_idxs, members in clusters:
        members.sort(key=lambda e: str(e.get("first_fill_at") or ""))
        opening = members[0]
        eps = [episodes[i] for i in sorted(ep_idxs)]
        closed = bool(eps) and all(e.is_closed for e in eps)
        out.append({
            "underlying": opening.get("underlying"),
            # The shape it was OPENED as names the position; later events
            # (closes, rolls) are its history, not its identity.
            "label": opening.get("label"),
            "status": "closed" if closed else "open",
            "opened_at": opening.get("first_fill_at"),
            "closed_at": max((str(e.closed_at) for e in eps if e.closed_at),
                             default=None) if closed else None,
            "conids": sorted({str(e.conid) for e in eps}),
            "episodes": len(eps),
            "fills": sum(e.get("fills") or 0 for e in members),
            # Down to the same leaf rows again, through every event's orders.
            "proceeds": Money.from_rows(
                (lg for ev in members for o in ev.get("orders", ())
                 for lg in o.get("legs", ())),
                "proceeds",
            ).payload(),
            # Episode-sourced, so these equal the Dashboard's accounting
            # exactly -- populated only when the lifecycle is closed, same
            # rule. Episodes carry the native and the currency, so the figure
            # is exact wherever one currency closed the whole position.
            "realized_pnl": (
                Money.charged(
                    (e.realized_pnl_base, e.realized_pnl, e.currency) for e in eps
                ).payload() if closed else None
            ),
            "commission": (
                Money.charged(
                    (e.commission_base, e.commission, e.currency) for e in eps
                ).payload() if closed else None
            ),
            "events": members,
        })
    out.sort(key=lambda p: str(p["opened_at"] or ""), reverse=True)
    return out
