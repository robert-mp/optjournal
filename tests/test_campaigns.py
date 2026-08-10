"""Tests for the campaign unit: which episodes were one decision.

Every case here runs against literals, with a nine-line episode double and no
SQLite, no statement and no fixture. That is what `campaigns.py` being a leaf
holder buys, and it is the point: before, the roll and re-entry cases could only
be reached through `position_groups` (which needs a twenty-key leg dict) and the
scoreboard half could not be reached at all except through `build_state` against
a populated database.

The numbers in the docstrings are from `demo/journal.db` and the real journal,
so a reader can check them against the running page.
"""

from __future__ import annotations

import pytest

from optjournal.campaigns import (
    WINDOW_S,
    cluster_orders,
    link,
    position_count,
)


class _Ep:
    """Just enough Episode surface for the union rule.

    Duck-typed deliberately: `campaigns.py` reads exactly these attributes, and
    keeping it to them is what stops the win rate's own rule from depending on
    `history.py` and a database.
    """

    def __init__(self, conid, trade_ids, *, closed=True, closed_at="2025-12-19",
                 pnl=0.0, comm=0.0, currency="USD"):
        self.conid = conid
        self.trade_ids = trade_ids
        self.is_closed = closed
        self.closed_at = closed_at if closed else None
        self.realized_pnl_base = pnl
        self.realized_pnl = pnl
        self.commission_base = comm
        self.commission = comm
        self.currency = currency


def _one_group(*order_ids):
    """Every named order as ONE decision, the shape a combo or a roll has."""
    return [tuple(order_ids)]


# ------------------------------------------------------------------ the window


def test_same_second_orders_on_one_underlying_are_one_decision():
    """The real journal's shape, and the reason order-id union is not enough.

    Verified against `journal.db`: every multi-leg event there arrives as
    SEPARATE order ids filled in the same second (META 1241544513/1241544750,
    GOOG 1243007507/1243007533, and the GOOG roll 1247248833/1247248883). Union
    by order id alone links none of them, so it reproduces the exact defect the
    campaign unit exists to remove.
    """
    groups = cluster_orders([
        ("1241544513", "2026-08-03 11:11:19", "META"),
        ("1241544750", "2026-08-03 11:11:19", "META"),
    ])
    assert groups == [("1241544513", "1241544750")]


def test_orders_outside_the_window_stay_separate():
    groups = cluster_orders([
        ("1", "2026-08-03 11:11:19", "META"),
        ("2", "2026-08-03 11:14:00", "META"),  # 161s later
    ])
    assert len(groups) == 2


def test_same_second_different_underlyings_stay_separate():
    groups = cluster_orders([
        ("1", "2026-08-03 11:11:19", "META"),
        ("2", "2026-08-03 11:11:19", "TSLA"),
    ])
    assert len(groups) == 2


def test_an_order_without_an_underlying_is_never_merged():
    """IBKR can report a combo exercise spanning several underlyings. The
    heuristic only trusts itself where it can see one, so such an order stays
    alone rather than being folded into whatever filled beside it."""
    groups = cluster_orders([
        ("1", "2026-08-03 11:11:19", "META"),
        ("2", "2026-08-03 11:11:19", None),
    ])
    assert len(groups) == 2


def test_an_order_without_a_time_is_never_merged():
    groups = cluster_orders([
        ("1", "2026-08-03 11:11:19", "META"),
        ("2", None, "META"),
    ])
    assert len(groups) == 2


def test_window_constant_is_seconds_and_modest():
    """The window is the heuristic's whole risk surface, and a win rate now
    depends on it where before only a card layout did. Pin its scale so a
    casual edit to minutes cannot silently merge unrelated trades into one
    decision and improve the win rate by doing so."""
    assert 1 <= WINDOW_S <= 300


# ------------------------------------------------------------------- the union


def test_a_roll_is_one_decision_and_is_undecided_until_the_last_leg_closes():
    """The whole behavioural change. A roll's order closes one contract and
    opens the next, so the two episodes are one campaign -- and it is not
    decided while the rolled-into leg is open, even though the near leg booked
    real P&L. That P&L still belongs to its own close month; only the OUTCOME
    waits."""
    eps = [_Ep("A", ["t1", "t2"], pnl=585.82),
           _Ep("B", ["t3"], closed=False)]
    (camp,) = link(eps, order_groups=_one_group("open", "roll"),
                   order_of_trade={"t1": "open", "t2": "roll", "t3": "roll"})
    assert camp.episode_indices == (0, 1)
    assert not camp.is_decided, "the rolled-into leg is still open"
    assert camp.realized is None and camp.closed_at is None
    assert not camp.is_win and not camp.is_loss


def test_a_rolled_loser_scratched_at_the_end_is_a_loss_not_a_win():
    """The trap a smaller fix falls into, and the reason a campaign's outcome
    is the SUM of its episodes rather than its final leg.

    Sell a put, go down 1200, roll out, scratch the last leg at +50. Counting
    episodes reports one win and one loss (50%). Merely skipping roll-closes
    reports ONE WIN at 100%, which would let any loser be rolled into a
    winning record. The sum is -1150, which is a loss.
    """
    eps = [_Ep("A", ["t1", "t2"], pnl=-1200.0),
           _Ep("B", ["t3", "t4"], pnl=50.0)]
    (camp,) = link(eps, order_groups=_one_group("open", "roll", "close"),
                   order_of_trade={"t1": "open", "t2": "roll",
                                   "t3": "roll", "t4": "close"})
    assert camp.is_decided
    assert camp.realized.base == pytest.approx(-1150.0)
    assert camp.is_loss and not camp.is_win


def test_a_spread_is_one_decision_though_it_is_two_contracts():
    """No roll involved, and still wrong on the episode unit. `demo/journal.db`
    holds an NVDA put vertical whose legs closed at +1150.86 and -588.54: one
    win PLUS one loss on a single spread that netted +562.33."""
    eps = [_Ep("A", ["t1", "t2"], pnl=1150.86),
           _Ep("B", ["t3", "t4"], pnl=-588.54)]
    (camp,) = link(eps, order_groups=_one_group("open", "close"),
                   order_of_trade={"t1": "open", "t3": "open",
                                   "t2": "close", "t4": "close"})
    assert camp.is_decided
    assert camp.realized.base == pytest.approx(562.32)
    assert camp.is_win, "one win, where the episode unit scored 1W and 1L"


def test_a_spread_closed_by_two_orders_on_two_days_is_still_one_decision():
    """The latent defect the greedy first-match sweep this replaces carried.

    Its clustering was not transitive: a vertical opened as one order and closed
    by two separate orders on separate days produced TWO cards sharing a
    contract, double-counting the position's P&L. Not reachable on either
    database today, so it never showed -- it goes live the first time a spread
    is closed in two goes. Union-find has no such case.
    """
    eps = [_Ep("A", ["t1", "t3"], pnl=500.0),
           _Ep("B", ["t2", "t4"], pnl=-200.0)]
    camps = link(
        eps,
        # Three separate decisions by the window: the open, then each close on
        # its own day. Only the shared OPEN ties the two legs together, which is
        # exactly the transitivity the sweep got wrong.
        order_groups=[("open",), ("close1",), ("close2",)],
        order_of_trade={"t1": "open", "t2": "open",
                        "t3": "close1", "t4": "close2"},
    )
    assert len(camps) == 1, "one position, not two cards sharing a contract"
    assert camps[0].realized.base == pytest.approx(300.0)


def test_re_entering_the_same_contract_is_a_new_decision():
    """Closing a position and opening the same contract again later are two
    decisions. This is why the union is by ORDER rather than by contract: a
    conid-keyed rule would fuse the completed round trip with the re-entry,
    leaving it undecided and dropping a real outcome from the scoreboard."""
    eps = [_Ep("A", ["t1", "t2"], pnl=300.0),
           _Ep("A", ["t3"], closed=False)]
    camps = link(eps, order_groups=[("first",), ("again",)],
                 order_of_trade={"t1": "first", "t2": "first", "t3": "again"})
    assert len(camps) == 2
    assert [c.is_decided for c in camps] == [True, False]
    assert camps[0].realized.base == pytest.approx(300.0)


def test_an_episode_no_order_reached_is_its_own_decision():
    """The LEAP: held from before the archive begins, so it has no fills at all
    and no order to group by. Still a position, and dropping it would
    undercount the book by exactly the position with the most history in it."""
    eps = [_Ep("LEAP", [], closed=False)]
    (camp,) = link(eps, order_groups=[], order_of_trade={})
    assert camp.episode_indices == (0,)
    assert camp.order_ids == frozenset()
    assert not camp.is_decided


def test_every_episode_lands_in_exactly_one_campaign():
    """The partition invariant. A campaign holds INDICES into the episode list,
    so an episode counted twice or dropped would silently move the scoreboard
    without moving the money."""
    eps = [_Ep(str(i), [f"t{i}"], closed=bool(i % 2)) for i in range(6)]
    camps = link(eps, order_groups=[("a",), ("b",)],
                 order_of_trade={"t0": "a", "t1": "a", "t2": "b"})
    seen = [i for c in camps for i in c.episode_indices]
    assert sorted(seen) == list(range(6))
    assert len(seen) == len(set(seen)), "an episode landed in two campaigns"


def test_a_campaign_spanning_currencies_withholds_its_native_figure():
    """USD and SEK cannot share a number, so the as-charged amount is withheld
    and the base translation stands -- `Money`'s rule, applied to the union of
    the contributing episodes' currencies rather than by re-gating a sum."""
    eps = [_Ep("A", ["t1"], pnl=100.0, currency="USD"),
           _Ep("B", ["t2"], pnl=50.0, currency="SEK")]
    (camp,) = link(eps, order_groups=_one_group("o"),
                   order_of_trade={"t1": "o", "t2": "o"})
    assert camp.realized.base == pytest.approx(150.0)
    assert camp.realized.native is None and camp.realized.currency is None


def test_the_decided_month_is_the_last_contract_to_close():
    """A campaign is credited where the DECISION finished, so the scoreboard
    does not claim an outcome in a month the position was still open. The money
    stays split by episode across the months it landed in, which is why the two
    counts can differ and why the page shows both."""
    eps = [_Ep("A", ["t1", "t2"], pnl=585.82, closed_at="2025-11-17 14:30:05"),
           _Ep("B", ["t3", "t4"], pnl=1044.76, closed_at="2025-12-19 16:00:00")]
    (camp,) = link(eps, order_groups=_one_group("open", "roll", "close"),
                   order_of_trade={"t1": "open", "t2": "roll",
                                   "t3": "roll", "t4": "close"})
    assert camp.closed_at == "2025-12-19 16:00:00"
    assert camp.realized.base == pytest.approx(1630.58)


# ---------------------------------------------------------- counting positions


def test_a_strangle_is_one_open_position_not_two():
    """The real book's headline read "open 5" for two strangles and a LEAP,
    because an episode is per CONTRACT. Five open contracts is true; five open
    positions is not, and the Positions tab grouped the same book into three."""
    eps = [_Ep("P1", ["t1"], closed=False), _Ep("C1", ["t2"], closed=False)]
    camps = link(eps, order_groups=_one_group("o"),
                 order_of_trade={"t1": "o", "t2": "o"})
    assert position_count(camps, eps) == 1
    assert len(camps[0].conids) == 2, (
        "the control: the position really holds two contracts, so 1 is a "
        "grouping and not a dropped leg"
    )


def test_a_decided_position_is_not_an_open_one():
    eps = [_Ep("A", ["t1"], pnl=10.0)]
    camps = link(eps, order_groups=_one_group("o"), order_of_trade={"t1": "o"})
    assert position_count(camps, eps) == 0


def test_the_count_applies_a_scope_without_disturbing_the_indices():
    """A trade-type filter must survive, and it arrives as a PREDICATE: a
    campaign holds indices into the episode list, so pre-filtering the list
    would shift what they point at and count the wrong positions."""
    eps = [_Ep("A", ["t1"], closed=False), _Ep("B", ["t2"], closed=False)]
    camps = link(eps, order_groups=[("a",), ("b",)],
                 order_of_trade={"t1": "a", "t2": "b"})
    assert position_count(camps, eps) == 2, "the control"
    assert position_count(camps, eps, in_scope=lambda e: e.conid == "A") == 1
    assert position_count(camps, eps, in_scope=lambda e: False) == 0
