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
written twice. This holds `money.py` and nothing else, so any layer may hold it
and every case below is testable against literals.

Episodes are duck-typed rather than imported. Everything here reads is
`conid`, `trade_ids`, `is_closed`, `closed_at`, `realized_pnl{,_base}`,
`commission{,_base}` and `currency`, which is why this stays a leaf holder
instead of acquiring `history.py`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from optjournal.money import Money

__all__ = [
    "Campaign",
    "WINDOW_S",
    "cluster_orders",
    "link",
    "position_count",
]

#: Orders on the same underlying whose first fills land inside this window are
#: one decision. Same-second for the real strangles and the real roll above;
#: 90s tolerates a combo split into legs that fill as the market moves, without
#: swallowing a deliberate second trade placed minutes later. Moved here from
#: `strategies.py` because this is now the module that owns the union rule, and
#: the constant is the whole risk surface of it.
WINDOW_S = 90


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


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
    #: The orders that filled it. Carried because it is how a caller holding
    #: ORDERS rather than episodes reaches the campaign: the leg views aggregate
    #: per contract and carry no fill id, so an order is the only handle the
    #: Trades tab has.
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

    @property
    def is_win(self) -> bool:
        """Decided and up. Judged on `base`, the only figure always present."""
        return self.realized is not None and self.realized.base > 0

    @property
    def is_loss(self) -> bool:
        return self.realized is not None and self.realized.base < 0


def cluster_orders(
    items: Iterable[tuple[str, Any, Any]],
) -> list[tuple[str, ...]]:
    """Order ids grouped into the decisions they were placed as.

    Each item is `(order_id, first_fill_at, underlying)`. Orders on one
    underlying whose first fills fall within `WINDOW_S` of each other are one
    decision; an order with no underlying (IBKR can report a combo exercise
    spanning several) or no parseable time is never merged, because the
    heuristic only trusts itself where it can see both.

    Returned as tuples of ids in fill order, so a caller can map back to
    whatever it holds those ids against.
    """
    rows = [
        (str(oid), _dt(at), str(under) if under else None)
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
) -> list[Campaign]:
    """Episodes unioned into campaigns by the orders that filled them.

    Two episodes are one campaign when one order group touched both: that is a
    roll (the group's order closed one expiry and opened the next) or a spread
    (its legs are separate contracts filled together). `order_of_trade` maps a
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

    first_in_group: dict[int, int] = {}
    #: Orders reached per episode, so the campaign can carry the union of them.
    orders_of_episode: dict[int, set[str]] = {}
    for i, episode in enumerate(episodes):
        for tid in getattr(episode, "trade_ids", ()) or ():
            oid = order_of_trade.get(str(tid))
            if oid is None:
                continue
            orders_of_episode.setdefault(i, set()).add(str(oid))
            group = group_of_order.get(str(oid))
            if group is None:
                continue
            # Every order in the group, not just this fill's: a spread leg that
            # never shared an episode with its sibling is still the same
            # decision, and the group is what says so.
            orders_of_episode[i].update(
                oid2 for oid2, g in group_of_order.items() if g == group
            )
            union(first_in_group.setdefault(group, i), i)

    members: dict[int, list[int]] = {}
    for i in range(len(episodes)):
        members.setdefault(find(i), []).append(i)

    out: list[Campaign] = []
    for root in sorted(members):
        idxs = members[root]
        eps = [episodes[i] for i in idxs]
        decided = bool(eps) and all(e.is_closed for e in eps)
        out.append(Campaign(
            episode_indices=tuple(idxs),
            conids=tuple(sorted({str(getattr(e, "conid", "") or "") for e in eps})),
            order_ids=frozenset(
                oid for i in idxs for oid in orders_of_episode.get(i, ())
            ),
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
        ))
    return out


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
