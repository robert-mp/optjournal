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

The grouping RULE itself lives in `campaigns.py`, not here. It has a second
consumer -- the Dashboard's scoreboard, which must count a roll as one decision
rather than two wins -- and `stats.py` cannot import this module. What stays
here is naming the shape and aggregating the legs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from optjournal import campaigns
from optjournal.money import FILL_MONEY_FIELDS, Money

Row = dict[str, Any]


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

    The window rule is `campaigns.cluster_orders`, so the Trades tab and the
    scoreboard group the same fills the same way by construction.
    """
    # Clustered under the order id with the broker after it, so two brokers'
    # order 5000 are two orders and ties still break on the id.
    by_key = {"\x1f".join(_order_key(o)[::-1]): o for o in orders}
    out = [
        _event([by_key[key] for key in keys])
        for keys in campaigns.cluster_orders(
            (key, o.get("first_fill_at"), _underlying(o)) for key, o in by_key.items()
        )
    ]
    out.sort(key=lambda g: g["first_fill_at"], reverse=True)
    return out


def _event(members: list[Row]) -> Row:
    """One strategy event: the orders placed together, named and totalled."""
    legs = [leg for o in members for leg in o.get("legs", ())]
    return {
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
    }


def _order_key(order: Row) -> tuple[str, str]:
    """An order's identity: `(broker, order id)`, since an order id is the
    issuing broker's own and two brokers can both number one 5000."""
    return str(order.get("broker") or ""), str(order.get("ib_order_id"))


def _campaign_of_order(
    campaign_list: list[campaigns.Campaign],
) -> dict[tuple[str, str], list[int]]:
    """Which campaigns each order filled, so an event is placed by its own orders.

    The campaign carries them because the leg views aggregate per contract and so
    carry no fill id for an event to join on.

    A LIST, because one order can fill two campaigns: its fills can end one
    position and begin the next, a fill through zero (IBKR's `C;O`) most plainly,
    so its order belongs to the decision it ended and the one it began. Keeping a
    single index let the last campaign win, and the order was then drawn in one
    card only, and the other read its opening date and its proceeds from whatever
    event was left to it.
    """
    out: dict[tuple[str, str], list[int]] = {}
    for index, camp in enumerate(campaign_list):
        for key in camp.orders:
            out.setdefault(key, []).append(index)
    return out


def _leg_part(leg: Row, part: Mapping[str, Any] | None) -> Row:
    """One campaign's share of a leg two campaigns divided, or the leg itself.

    `part` is `campaigns.Campaign.leg_parts`: the leg's own summed columns as this
    campaign took them, so it overlays the leg and the rest (the contract, the
    side, the currency) stays. Its marker is what makes a closing share read STC
    and an opening one STO.
    """
    if part is None:
        return leg
    out = {**leg, **part}
    out["money"] = {f: Money.from_rows([out], f).payload()
                    for f in FILL_MONEY_FIELDS}
    return out


def _order_part(order: Row, camp: campaigns.Campaign) -> Row:
    """The order as one campaign filled it, when another filled the rest.

    Its own totals are re-read from the legs it now carries, so the part's fill
    count and first fill are its own and not the whole order's.
    """
    oid = str(order.get("ib_order_id"))
    legs = [_leg_part(leg, camp.leg_parts.get((oid, str(leg.get("conid")))))
            for leg in order.get("legs", ())]
    times = [str(leg["first_fill_at"]) for leg in legs if leg.get("first_fill_at")]
    ends = [str(leg["last_fill_at"]) for leg in legs if leg.get("last_fill_at")]
    return {
        **order,
        "legs": legs,
        "fills": sum(leg.get("fills") or 0 for leg in legs),
        "first_fill_at": min(times, default=order.get("first_fill_at")),
        "last_fill_at": max(ends, default=order.get("last_fill_at")),
        **{f: Money.from_rows(legs, f).payload() for f in FILL_MONEY_FIELDS},
    }


def _first_taker(
    order: Row, indices: list[int], campaign_list: list[campaigns.Campaign],
) -> int:
    """Of the campaigns dividing an order, the one that took its first execution.

    A reversal's one execution is taken by both, so the tie goes to the position
    it closed (`campaigns.first_taken`).
    """
    oid = str(order.get("ib_order_id"))

    def first(index: int) -> tuple[str, bool]:
        return min(
            (campaigns.first_taken(part)
             for (order_id, _conid), part in campaign_list[index].leg_parts.items()
             if order_id == oid),
            default=("\uffff", True),
        )

    return min(indices, key=lambda index: (first(index), index))


def _campaign_events(
    orders: list[Row], campaign_list: list[campaigns.Campaign], *, divide: bool,
) -> list[tuple[int | None, Row]]:
    """Every strategy event with the campaign that filled it, newest first.

    The event grouping reads orders, which carry no note codes, so it puts two
    positions' expirations in one event (IBKR stamps both 16:20:00) after the
    campaigns have kept the positions apart. Split along campaign lines, each
    position keeps its own expiry, while a spread's legs expiring together (one
    campaign) stay one event.

    An order two campaigns filled joins both when `divide`, each part carrying
    its own share of the leg (`_order_part`), which is how the cards draw it. The
    Calendar lists a day's executions instead, so there the order stays whole, in
    the campaign that took its first execution (`_first_taker`). `None` for an
    event no campaign claims.
    """
    campaigns_of_order = _campaign_of_order(campaign_list)
    out: list[tuple[int | None, Row]] = []
    for event in strategy_groups(orders):
        parts: dict[int | None, list[Row]] = {}
        for order in event["orders"]:
            found = campaigns_of_order.get(_order_key(order), [])
            if len(found) > 1 and not divide:
                found = [_first_taker(order, found, campaign_list)]
            if len(found) <= 1:
                # An order drawn in one place is drawn whole.
                parts.setdefault(found[0] if found else None, []).append(order)
            else:
                for index in found:
                    parts.setdefault(index, []).append(
                        _order_part(order, campaign_list[index]))
        if len(parts) == 1:
            out.append((next(iter(parts)), event))
        else:
            out.extend((index, _event(members))
                       for index, members in parts.items())
    return out


def campaign_events(
    orders: list[Row], campaign_list: list[campaigns.Campaign],
) -> list[Row]:
    """`strategy_groups`, with no event straddling two campaigns, newest first.

    What the Calendar's day detail reads. Its events are the Trades cards' events,
    with one difference: an order two positions divided is listed once and whole,
    because the day detail lists the day's executions, and a reversal is one. See
    `_campaign_events`.
    """
    return [event for _index, event in
            _campaign_events(orders, campaign_list, divide=False)]


def position_groups(
    orders: list[Row],
    *,
    episodes: list[Any],
    campaign_list: list[campaigns.Campaign],
) -> list[Row]:
    """Strategy events linked into position lifecycles, newest first.

    An opened-then-closed single leg is one position, not two trades -- and
    the accounting layer already knows it: `history.py` FIFO-matches fills
    into per-conid episodes, so the open and the close share an episode.

    The union itself is `campaigns.link`'s, handed in rather than recomputed,
    which is what makes this card and the Dashboard's win rate the same reading
    of the same fills. Before, the rule was written here and the scoreboard
    counted episodes, so a roll drew ONE card while the headline said two wins.

    Two deliberate consequences, both `campaigns.py`'s:

    * A re-opened contract later is a NEW episode, so it starts a new
      lifecycle rather than reviving the old card.
    * A roll shares an episode with the old lifecycle AND opens a new one --
      the union links the whole chain into one campaign card. That is the
      intended reading of a roll: one continuing decision, with each episode's
      P&L still landing in its own close month underneath.

    Events whose orders map to no episode (nothing but snapshots, or an
    unmatched category) stay as singleton lifecycles.

    An order whose fills ended one position and began the next is drawn in both
    cards, each with its own share (`_campaign_events`), so every fill's
    quantity and money is drawn once across the cards. A card's `fills` counts
    every execution it draws, so a split `C;O` one counts in both of its cards:
    no total adds cards' fills up, and the ones that count executions across
    cards (the Dashboard's, the Calendar's) count each once.
    """
    # Keyed by campaign index, or by the event's own position when no campaign
    # claims it -- a unique key, so an unlinked event stays a card of its own
    # rather than pooling every orphan into one. The index comes from the split
    # itself: a reversal order's two parts differ only in which campaign claimed
    # them, so reading it back off the event's order ids could not tell them
    # apart.
    grouped: dict[tuple[bool, int], list[Row]] = {}
    for position, (index, event) in enumerate(
        _campaign_events(orders, campaign_list, divide=True)
    ):
        key = (True, index) if index is not None else (False, position)
        grouped.setdefault(key, []).append(event)

    out: list[Row] = []
    for (linked, index), members in grouped.items():
        members.sort(key=lambda e: str(e.get("first_fill_at") or ""))
        opening = members[0]
        camp = campaign_list[index] if linked else None
        eps = [episodes[i] for i in camp.episode_indices] if camp else []
        out.append({
            "underlying": opening.get("underlying"),
            # The shape it was OPENED as names the position; later events
            # (closes, rolls) are its history, not its identity.
            "label": opening.get("label"),
            "status": "closed" if camp and camp.is_decided else "open",
            "opened_at": opening.get("first_fill_at"),
            "closed_at": camp.closed_at if camp else None,
            "conids": list(camp.conids) if camp else [],
            # The handle anything keyed on this DECISION uses -- today the
            # journal entry. `campaigns.Campaign.anchor`: the lowest order id the
            # decision filled under, which survives the rebuild this whole
            # structure goes through on every ingest. None for a card no campaign
            # claims (snapshots only), and the page renders that as a decision it
            # cannot yet attach writing to rather than hiding the button.
            "anchor": camp.anchor if camp else None,
            # Hand-made links that built this card, so the page can offer to
            # undo exactly those and nothing the window decided.
            "links": [list(pair) for pair in camp.links] if camp else [],
            "episodes": len(eps),
            "fills": sum(e.get("fills") or 0 for e in members),
            # Down to the same leaf rows again, through every event's orders.
            "proceeds": Money.from_rows(
                (lg for ev in members for o in ev.get("orders", ())
                 for lg in o.get("legs", ())),
                "proceeds",
            ).payload(),
            # Campaign-sourced, so these equal the Dashboard's accounting
            # exactly -- populated only when the position is decided, same
            # rule. Episodes carry the native and the currency, so the figure
            # is exact wherever one currency closed the whole position.
            "realized_pnl": (
                camp.realized.payload() if camp and camp.realized else None
            ),
            "commission": (
                camp.commission.payload() if camp and camp.commission else None
            ),
            "events": members,
        })
    out.sort(key=lambda p: str(p["opened_at"] or ""), reverse=True)
    return out
