"""Which episodes were one decision, and what that decision earned.

An episode (`history.py`) is one round trip in ONE contract. That is the right
unit for MONEY: it ties to the broker statement, and monthly rows sum to annual
ones because each episode lands in exactly one close month. It is the wrong unit
for a SCOREBOARD, and two verified cases show why:

* A roll closes one expiry and opens the next. It ends episode A and starts
  episode B, so one continuing decision scored as two closed trades and two
  wins. Worse in the losing direction: roll a short put down 1200, scratch the
  final leg at +50, and the episode unit reports one win and one loss (50%)
  for a decision that lost 1150.
* A put vertical is two conids with no roll involved. On `demo/journal.db` it
  scored one win PLUS one loss on a single spread that netted +562.33.

Measured on `demo/journal.db`: the episode unit gives 9 closed, 7 wins, 2
losses, 77.8%; the campaign unit gives 7, 6, 1, 85.7%. Net P&L is 3695.08
either way, which is the whole point. The money does not move, only the
counting. `stats.py`'s module docstring states the money rule; this one states
the outcome rule, and the two are deliberately different.

A campaign is DECIDED only when every episode in it is closed, because a roll
is a continuation. Its outcome is the SUM of its episodes' realised P&L, which
is what makes the losing-roll case above come out at -1150 rather than +50:
counting only the final leg would let any loser be rolled into a win.

Two levels of linkage, and both are needed. Verified against the real journal,
where every multi-leg event arrives as SEPARATE order ids filled in the same
second:

    2026-08-03 11:11:19  META strangle  orders 1241544513, 1241544750
    2026-08-04 11:24:00  GOOG strangle  orders 1243007507, 1243007533
    2026-08-07 11:06:03  GOOG roll      orders 1247248833, 1247248883  (O, C)

So order-id union alone finds nothing on real data: it reproduces the exact
defect it was meant to remove. `cluster_orders` is what bridges that, and
`strategy_groups`'s docstring has recorded the same fact since the first
strangle. The consequence is honest and worth stating plainly: a win rate now
depends on a 90-second heuristic, where before only a card layout did.

Why a module of its own, rather than a helper inside `strategies.py`: the union
rule has two consumers on opposite sides of the import graph. `strategies.py`
draws the Trades tab's lifecycle cards, `stats.py` counts the scoreboard, and
neither may import the other. The rule lived in `strategies.py` mixed in with
order grouping, event labelling and Money aggregation, where the stats layer
could not reach it, so the Dashboard counted a roll as two wins while the
Trades tab drew it as one card. That is `notes.py`'s situation exactly: one
rule, two readers that cannot see each other, the previous state being the rule
written twice. This holds `money.py` and `notes.py`, both leaves, so any layer may hold it
and every case below is testable against literals.

Episodes are duck-typed rather than imported. Everything here reads is
`conid`, `trade_ids`, `is_closed`, `closed_at`, `realized_pnl{,_base}`,
`commission{,_base}`, `currency` and `fill_parts`, which is why this stays a
leaf holder instead of acquiring `history.py`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from optjournal.money import Money
from optjournal.notes import split_notes

__all__ = [
    "BROKER_CODES",
    "Campaign",
    "WINDOW_S",
    "cluster_orders",
    "first_taken",
    "link",
    "placed_by_broker",
    "position_count",
]

#: Orders on the same underlying whose first fills land inside this window are
#: one decision. Same-second for the real strangles and the real roll above;
#: 90s tolerates a combo split into legs that fill as the market moves, without
#: swallowing a deliberate second trade placed minutes later. Moved here from
#: `strategies.py` because this is now the module that owns the union rule, and
#: the constant is the whole risk surface of it.
WINDOW_S = 90

#: IBKR note codes on fills the BROKER generated rather than the trader placed:
#: expiry (`Ep`), assignment (`A`), exercise (`Ex`, `AEx`, `MEx`, `GEA`), a
#: margin liquidation (`L`) and a dividend reinvestment (`R`). IBKR stamps them
#: with its own processing time, every expiration at 16:20:00, so the window
#: would read unrelated positions expiring together as one placement.
BROKER_CODES = frozenset({"Ep", "A", "Ex", "AEx", "MEx", "GEA", "L", "R"})


def placed_by_broker(notes: Any) -> bool:
    """Whether a fill's note codes say IBKR generated it. Whole codes only, so
    `AFx` (an auto-conversion) is not read as `A` (an assignment)."""
    return not BROKER_CODES.isdisjoint(split_notes(notes))


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _order_sort_key(order_id: str) -> tuple[int, float]:
    """Sortable numerically when an order id is a number, else last.

    `(0, value)` for the numeric ids IBKR issues and `(1, inf)` for anything
    else, so a broker that ever labels an order 'A17' sorts after every number
    rather than raising in the middle of a page render.
    """
    try:
        return (0, float(order_id))
    except (TypeError, ValueError):
        return (1, float("inf"))


@dataclass(frozen=True, slots=True)
class Campaign:
    """One continuing decision, and the episodes that carried it out.

    Holds indices into the episode sequence it was built from rather than the
    episodes themselves, so a caller gets back to its OWN objects without this
    module deciding what an episode is. The indices are only meaningful for
    that exact sequence: `link` returns campaigns for the list it was handed,
    and reordering that list afterwards silently invalidates them.
    """

    #: Positions in the sequence handed to `link`, ascending.
    episode_indices: tuple[int, ...]
    #: Contracts the campaign touched, for reconciling against the open book.
    conids: tuple[str, ...]
    #: The orders that filled it: those of its episodes' own fills. Carried
    #: because it is how a caller holding ORDERS rather than episodes reaches the
    #: campaign: the leg views aggregate per contract and carry no fill id, so an
    #: order is the only handle the Trades tab has. One order is in two campaigns
    #: when its fills ended one position and began the next; `leg_parts` says
    #: what each took. `orders` says which broker issued each.
    order_ids: frozenset[str]
    #: Every episode closed. A roll into a still-open position leaves this
    #: False, which is the entire behavioural change: the near leg's realised
    #: P&L stays in its own close month while the decision stays undecided.
    is_decided: bool
    #: When the LAST episode closed, so the scoreboard credits the month the
    #: decision finished. None while undecided.
    closed_at: str | None
    #: Summed realised P&L, gated across currencies. None while undecided,
    #: matching the lifecycle card and the Dashboard's own rule.
    realized: Money | None
    commission: Money | None
    #: The hand-made links (`link`'s `links`) that joined episodes into this
    #: campaign, as stored. Empty for a campaign the window alone built, which is
    #: how the Trades tab knows which cards it may offer to unlink.
    links: tuple[tuple[str, str], ...] = ()
    #: `order_ids` with the broker that issued each, `(broker, order id)`, which
    #: is how the Trades tab finds an order's campaigns: an order id is the
    #: issuing broker's own, and two brokers can both number an order 5000.
    orders: frozenset[tuple[str, str]] = frozenset()
    #: What this campaign took of an order LEG another campaign also took, keyed
    #: by `(order id, conid)`, in the leg's own columns (`_leg_share`): quantity,
    #: fills, prices, times, money, and the open/close marker of what it took.
    #: A leg is divided when one order's fills end one position and begin the
    #: next: a reversal (IBKR's `C;O`) split at zero, or an order whose closing
    #: fills and opening fills arrive separately. Summed from the episodes' own
    #: `fill_parts`, so a reversal divides as `history._through_zero` divided it
    #: and every other fill goes whole to the position that took it. A leg only
    #: this campaign took is not here, since it is drawn as it is. Empty for
    #: every campaign that divides no leg, which is all of them on either
    #: journal today.
    #:
    #: `hash=False` because a mapping is not hashable and this dataclass is
    #: frozen, so including it would turn `hash(campaign)` from working into a
    #: TypeError. Equality still reads it.
    leg_parts: Mapping[tuple[str, str], Mapping[str, Any]] = field(
        default_factory=dict, hash=False)

    @property
    def anchor(self) -> str | None:
        """The campaign's stable handle: its lowest order id, or None if it has
        no fills.

        `episode_indices` cannot be a handle: they are positions in the list
        `link` was handed, and every ingest rebuilds that list. Nor can the
        campaign's identity be its membership, which a 90-second heuristic
        decides and a later fill can change. An ORDER ID is neither: IBKR issued
        it, it names one placement forever, and it is already carried here
        because the Trades tab reaches campaigns through orders.

        The LOWEST of its OWN orders, so the handle is the decision's earliest
        placement, a roll added tomorrow does not move it, and nothing but the
        card's own fills decides it: not the episode list's order, and not which
        other cards exist. Compared numerically, because IBKR order ids are
        numbers in text and `min` on strings would rank '999' above '1000' (true
        today only because the real ids are all ten digits, which is the kind of
        accident that holds until it does not). Ties fall back to the string so
        the answer is total either way.

        Two cards share one only where one order is the lowest of each: a
        holding from before the archive closed by the order that opened the next
        position, or one order allocated to two accounts. Both show what is
        filed under it, as the released code showed it.

        None for a campaign built only from position snapshots: the archive holds
        no fills for it, so there is no order to name. Callers that key anything
        on this have to say what they do about that; see `journal.py`.
        """
        return _lowest(self.order_ids)

    @property
    def brokers(self) -> frozenset[str]:
        """The brokers whose fills it holds; one, in all but a hand-made case."""
        return frozenset(broker for broker, _oid in self.orders)

    @property
    def is_win(self) -> bool:
        """Decided and up. Judged on `base`, the only figure always present."""
        return self.realized is not None and self.realized.base > 0

    @property
    def is_loss(self) -> bool:
        return self.realized is not None and self.realized.base < 0


def cluster_orders(
    items: Iterable[tuple[str, Any, Any]],
    *,
    standalone: Iterable[str] = (),
) -> list[tuple[str, ...]]:
    """Order ids grouped into the decisions they were placed as.

    Each item is `(order_id, first_fill_at, underlying)`. Orders on one
    underlying whose first fills fall within `WINDOW_S` of each other are one
    decision; an order with no underlying (IBKR can report a combo exercise
    spanning several) or no parseable time is never merged, because the
    heuristic only trusts itself where it can see both.

    `standalone` names orders that are never merged either: the ones IBKR
    generated (`placed_by_broker`). The window infers a shared placement from a
    shared time, and nobody placed an expiration, so two positions expiring on
    the same afternoon share IBKR's timestamp and nothing else.

    Returned as tuples of ids in fill order, so a caller can map back to
    whatever it holds those ids against.
    """
    alone = {str(oid) for oid in standalone}
    rows = [
        (str(oid), _dt(at), str(under) if under and str(oid) not in alone else None)
        for oid, at, under in items
    ]
    rows.sort(key=lambda r: (r[2] or f"￿{r[0]}", str(r[1] or ""), r[0]))

    groups: list[list[tuple[str, datetime | None, str | None]]] = []
    for row in rows:
        last = groups[-1][-1] if groups else None
        same = (
            last is not None
            and row[2] is not None
            and row[2] == last[2]
            and row[1] is not None
            and last[1] is not None
            and abs((row[1] - last[1]).total_seconds()) <= WINDOW_S
        )
        if same:
            groups[-1].append(row)
        else:
            groups.append([row])
    return [tuple(r[0] for r in g) for g in groups]


def link(
    episodes: Sequence[Any],
    *,
    order_groups: Iterable[Iterable[str]],
    order_of_trade: Mapping[str, str],
    links: Iterable[tuple[str, str]] = (),
) -> list[Campaign]:
    """Episodes unioned into campaigns by the orders that filled them.

    Two episodes are one campaign when one order group touched both: that is a
    roll (the group's order closed one expiry and opened the next) or a spread
    (its legs are separate contracts filled together). A group that touched only
    ONE contract joins nothing: that is a fill through zero, a reversal rather
    than a continuation, and its two sides are two decisions. `order_of_trade` maps a
    fill id to its order, which is how an episode -- which knows only its trade
    ids -- reaches the group.

    Union-find rather than the greedy first-match sweep this replaces, because
    the relation is transitive and the sweep was not. A vertical opened as one
    order and closed by two separate orders on separate days produced TWO
    clusters sharing a contract, double-counting the position's P&L across two
    cards. Not reachable on either database today, which is why it survived: it
    is latent, and it goes live the first time a spread is closed in two goes.

    An episode no order group claims is its own campaign. Not an edge case: a
    contract held from before the archive has no fills at all (this journal's
    LEAP), and it is still a position.

    `links` are pairs of order ids the reader joined by hand: a roll whose two
    halves were placed further apart than `WINDOW_S`. The page posts two cards'
    anchors, so each end is the card the window built whose anchor it is, and an
    id that is no card's anchor (one stored when anchors were read another way)
    is the card that filled that order. A pair naming an order no episode
    reached (another category, or a fill since re-keyed) is skipped rather than
    raised, because the row is the reader's and the page still has to render.
    """
    parent = list(range(len(episodes)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # Which campaign-group each fill belongs to, then which episodes each group
    # touches. Built from the episodes' own trade ids, so an episode with no
    # fills simply appears in no group.
    group_of_order: dict[str, int] = {}
    for index, ids in enumerate(order_groups):
        for oid in ids:
            group_of_order[str(oid)] = index

    #: Per broker as well: a decision is placed at one broker, and an order id is
    #: the issuing broker's own, so two brokers' order 5000 are not one order
    #: touching two contracts, which is what a spread looks like.
    members_of_group: dict[tuple[int, str], list[int]] = {}
    #: The orders of each episode's OWN fills. Every order of a group that joins
    #: episodes (below) is some member's own anyway; a group that joins nothing,
    #: a contract closed and re-opened or flipped through two orders seconds
    #: apart, would otherwise list each side's order under the other.
    orders_of_episode: dict[int, set[str]] = {}
    broker_of = [str(getattr(episode, "broker", "") or "") for episode in episodes]
    for i, episode in enumerate(episodes):
        for tid in getattr(episode, "trade_ids", ()) or ():
            order_id = order_of_trade.get(str(tid))
            if order_id is None:
                continue
            orders_of_episode.setdefault(i, set()).add(order_id)
            group = group_of_order.get(order_id)
            if group is not None:
                members_of_group.setdefault((group, broker_of[i]), []).append(i)
    # A group joins DIFFERENT contracts: a roll's two expiries, a spread's legs.
    # A group that touched episodes of only ONE contract has reversed it -- a fill
    # through zero (IBKR's `C;O`) belongs to the long it closed and the short it
    # opened -- and the finished side is an outcome of its own, not cash in
    # flight inside the other. Not reachable on either journal today: no
    # campaign there holds two episodes of one contract.
    for touched in members_of_group.values():
        if len({str(getattr(episodes[i], "conid", "") or "") for i in touched}) > 1:
            for i in touched:
                union(touched[0], i)

    def orders_of(idxs: Iterable[int]) -> set[str]:
        return {oid for i in idxs for oid in orders_of_episode.get(i, ())}

    # The card each end of a hand link names, among the cards the window built
    # before any link is applied: the one card that filled it, nearly always.
    # Where several did, the card whose anchor it is, then the one that took the
    # order's first execution, then the one whose own orders, and then fills,
    # sort first. Nothing here reads the order of the episode list.
    window = _members(find, len(episodes))
    owners: dict[str, list[int]] = {}
    for root, idxs in window.items():
        for order_id in orders_of(idxs):
            owners.setdefault(order_id, []).append(root)
    # What `nearness` reads of a card, built once per card and only for the
    # cards that share an order, so the common case costs one pass.
    seen: dict[int, tuple[dict[str, tuple[bool, str, bool]], tuple[Any, ...]]] = {}

    def nearness(root: int, order_id: str) -> tuple[Any, ...]:
        if root not in seen:
            idxs = window[root]
            own_ids = sorted(orders_of(idxs), key=lambda oid: (_order_sort_key(oid), oid))
            first: dict[str, tuple[bool, str, bool]] = {}
            for i in idxs:
                for tid, part in (getattr(episodes[i], "fill_parts", None) or {}).items():
                    oid = order_of_trade.get(str(tid))
                    if oid is not None and (oid not in first or _taken_first(part) < first[oid]):
                        first[oid] = _taken_first(part)
            seen[root] = (
                first,
                (own_ids, sorted(str(t) for i in idxs
                                 for t in getattr(episodes[i], "trade_ids", ()) or ())))
        first, rest = seen[root]
        return (rest[0][0] != order_id, first.get(order_id, (True, "", True)), rest)

    def nearest(roots: list[int], order_id: str) -> int:
        if len(roots) == 1:
            return roots[0]
        return min(roots, key=lambda root: nearness(root, order_id))

    def card_of(order_id: str) -> int | None:
        roots = owners.get(order_id)
        return window[nearest(roots, order_id)][0] if roots else None

    applied: list[tuple[int, tuple[str, str]]] = []
    for a, b in links:
        ia, ib = card_of(str(a)), card_of(str(b))
        if ia is not None and ib is not None:
            union(ia, ib)
            applied.append((ia, (str(a), str(b))))

    members = _members(find, len(episodes)) if applied else window
    links_of_root: dict[int, list[tuple[str, str]]] = {}
    for i, pair in applied:
        links_of_root.setdefault(find(i), []).append(pair)

    # What each campaign took of each order leg, per (order, contract), which is
    # the shape a leg has: its episodes' own fill parts. A leg more than one
    # campaign took is divided between them; see `Campaign.leg_parts`.
    took: dict[int, dict[tuple[str, str, str], list[tuple[str, Any]]]] = {}
    for root, idxs in members.items():
        for i in idxs:
            conid = str(getattr(episodes[i], "conid", "") or "")
            for tid, part in (getattr(episodes[i], "fill_parts", None) or {}).items():
                order_id = order_of_trade.get(str(tid))
                if order_id is not None:
                    took.setdefault(root, {}).setdefault(
                        (broker_of[i], order_id, conid), []).append((str(tid), part))
    takers: dict[tuple[str, str, str], int] = {}
    for legs in took.values():
        for key in legs:
            takers[key] = takers.get(key, 0) + 1

    out: list[Campaign] = []
    for root in sorted(members):
        idxs = members[root]
        eps = [episodes[i] for i in idxs]
        decided = bool(eps) and all(e.is_closed for e in eps)
        own = frozenset((broker_of[i], oid) for i in idxs
                        for oid in orders_of_episode.get(i, ()))
        out.append(Campaign(
            episode_indices=tuple(idxs),
            conids=tuple(sorted({str(getattr(e, "conid", "") or "") for e in eps})),
            order_ids=frozenset(oid for _broker, oid in own),
            orders=own,
            is_decided=decided,
            closed_at=max(
                (str(e.closed_at) for e in eps if e.closed_at), default=None
            ) if decided else None,
            # Fed the episodes' own (base, native, currency) triples rather than
            # a sum, so the gate is asked against the union of THEIR currencies:
            # a campaign that closed in two currencies withholds its native
            # instead of labelling one figure with the other's currency.
            realized=Money.charged(
                (e.realized_pnl_base, e.realized_pnl, e.currency) for e in eps
            ) if decided else None,
            commission=Money.charged(
                (e.commission_base, e.commission, e.currency) for e in eps
            ) if decided else None,
            links=tuple(sorted(links_of_root.get(root, ()))),
            leg_parts={(order_id, conid): _leg_share(taken)
                       for (broker, order_id, conid), taken in took.get(root, {}).items()
                       if takers[(broker, order_id, conid)] > 1},
        ))
    return out


def _members(find: Callable[[int], int], count: int) -> dict[int, list[int]]:
    """Episode indices by the root `find` puts them under, each list ascending."""
    members: dict[int, list[int]] = {}
    for i in range(count):
        members.setdefault(find(i), []).append(i)
    return members


def _lowest(order_ids: Iterable[str]) -> str | None:
    """The lowest order id, numerically, or None for none (`Campaign.anchor`)."""
    return min(order_ids, key=lambda oid: (_order_sort_key(oid), oid), default=None)


def _taken_first(part: Any) -> tuple[bool, str, bool]:
    """Orders the fill parts of one order by when they were taken: by time (an
    undated part last), and on one instant the closing half of a split execution
    before its opening half, since it closed one position before it opened the
    next."""
    return (not part.date_time, str(part.date_time or ""),
            str(part.open_close).upper() != "C")


#: The columns of a leg that are sums over its fills, named as `db.trade_legs`
#: and `history.FillPart` both name them.
_LEG_TOTALS = ("quantity", "proceeds", "proceeds_base", "commission",
               "commission_base", "realized_pnl", "realized_pnl_base")


def _leg_share(taken: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    """The share of one order leg a campaign took, in the leg's own columns.

    Summed from the fill parts its episodes took, `(trade id, part)`, so every
    figure is those fills' own, a reversal's half included, and the shares of the
    campaigns dividing a leg add back up to it. `fills` counts the executions it
    drew from, a split one included, so each card counts every execution it
    draws and a split one counts in both of its cards (once in one card holding
    both of its halves); a total across cards counts executions from the fills
    themselves instead (the Dashboard's, and the Calendar's, which lists an
    order whole). The price is `trade_legs`' average, over this share's fills.
    The open/close marker is that of what the share took first (`first_taken`),
    which is every part's when they agree, and which says, of two shares
    starting on one split execution, which took its closing half.
    """
    parts = [part for _tid, part in taken]
    share: dict[str, Any] = {
        name: sum(getattr(part, name) for part in parts) for name in _LEG_TOTALS}
    share["fills"] = len({tid for tid, _part in taken})
    size = sum(abs(part.quantity) for part in parts)
    share["avg_price"] = sum(
        abs(part.quantity) * part.trade_price
        for part in parts if part.trade_price is not None
    ) / size if size else None
    times = [str(part.date_time) for part in parts if part.date_time]
    share["first_fill_at"] = min(times, default=None)
    share["last_fill_at"] = max(times, default=None)
    share["open_close"] = min(parts, key=_taken_first).open_close
    return share


def first_taken(share: Mapping[str, Any]) -> tuple[str, bool]:
    """When a share of an order leg (`_leg_share`) took its first execution, for
    ordering the shares of one order: by time, and on a tie the share that took
    it closing first. A split `C;O` execution is taken by both of its positions at
    one instant, and it closed the one before it opened the next."""
    return (str(share.get("first_fill_at") or ""),
            str(share.get("open_close") or "").upper() != "C")


def position_count(
    campaigns: Sequence[Campaign],
    episodes: Sequence[Any],
    *,
    in_scope: Callable[[Any], bool] | None = None,
) -> int:
    """How many POSITIONS the open episodes among `episodes` form.

    An episode is per CONTRACT, so a strangle is two of them and a headline
    "open 5" counted a two-leg strangle twice, reading as five separate bets.
    Five is a true number, it is the count of open contracts, but it is not the
    number of positions, and the Positions tab groups the same book into three
    cards -- so the two disagreed on screen.

    A campaign is that unit already, which is the whole reason this is a few
    lines here and was thirty in `strategies.py`: "which contracts are one
    position" is the question this module answers.

    `in_scope` filters the episodes a trade-type scope excludes. A predicate
    rather than a pre-filtered list because a campaign holds INDICES into
    `episodes`, so dropping elements would silently shift what they point at.
    """
    counted = 0
    for campaign in campaigns:
        for index in campaign.episode_indices:
            episode = episodes[index]
            if episode.is_closed:
                continue
            if in_scope is not None and not in_scope(episode):
                continue
            counted += 1
            break
    return counted
