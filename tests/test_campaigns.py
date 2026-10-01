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


def test_orders_the_broker_generated_are_never_merged():
    """IBKR stamps every expiration at 16:20:00, each under its own order id.

    Two positions on one underlying that expire the same day then land inside
    the window, so a short put opened in September and a long call opened three
    weeks later scored as ONE decision (`pnl/s_expiry_merge.py`: 1W/1L became
    0W/1L). The trader placed neither expiration, so there is no placement to
    share. The caller names those orders; they stand alone.
    """
    items = [("9001", "2026-10-16 16:20:00", "SPY"),
             ("9002", "2026-10-16 16:20:00", "SPY")]
    assert len(cluster_orders(items)) == 1, "the control: the window alone merges"
    assert len(cluster_orders(items, standalone={"9001", "9002"})) == 2
    # A placed order in the same second still clusters with other placed ones.
    placed = [*items, ("5", "2026-10-16 16:20:00", "SPY"),
              ("6", "2026-10-16 16:20:30", "SPY")]
    assert sorted(cluster_orders(placed, standalone={"9001", "9002"})) == [
        ("5", "6"), ("9001",), ("9002",)]


@pytest.mark.parametrize("notes, expected", [
    ("Ep", True), ("A", True), ("A;P", True), ("Ex", True), ("AEx", True),
    ("MEx", True), ("GEA", True), ("L", True), ("R", True),
    ("AFx", False), ("P", False), ("SL", False), ("", False), (None, False),
])
def test_which_fills_the_broker_generated(notes, expected):
    """Expiry, assignment, exercise, a margin liquidation and a dividend
    reinvestment are stamped by IBKR. Matched as whole codes: `AFx` (an
    auto-conversion) contains `A` (assignment) and is not one."""
    from optjournal.campaigns import placed_by_broker

    assert placed_by_broker(notes) is expected


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


def test_a_fill_through_zero_is_two_decisions_not_a_roll():
    """Long 2 calls, then one SELL 3 (IBKR `C;O`): the long closed and a short
    opened, in the same contract, by the same fill.

    `history.py` splits that fill across the two episodes, so both hold its
    trade id and one order touches both. A roll or a spread joins DIFFERENT
    contracts; an order touching two episodes of the SAME contract has reversed
    the position, and the finished long is an outcome of its own rather than
    cash in flight inside the short.
    """
    eps = [_Ep("C1", ["t1", "t2"], pnl=198.0),
           _Ep("C1", ["t2"], closed=False)]
    camps = link(eps, order_groups=[("open",), ("flip",)],
                 order_of_trade={"t1": "open", "t2": "flip"})
    assert len(camps) == 2
    assert [c.is_decided for c in camps] == [True, False]
    assert camps[0].realized.base == pytest.approx(198.0)


def test_a_group_that_joins_nothing_lends_neither_side_its_other_order():
    """Sell a long, buy the same contract back 30 seconds later under a second
    order: one window group on one contract, so it joins nothing. Each campaign
    still listed every order of the group, and the Trades tab, which reaches a
    campaign through its orders, drew both orders whole in both cards. Each now
    lists the orders of its own fills, and answers to the lowest of them."""
    eps = [_Ep("C1", ["t1", "t2"], pnl=48.0), _Ep("C1", ["t3", "t4"], pnl=43.0)]
    camps = link(eps, order_groups=[("1001",), ("1002", "1003"), ("1004",)],
                 order_of_trade={"t1": "1001", "t2": "1002", "t3": "1003",
                                 "t4": "1004"})
    assert [c.order_ids for c in camps] == [
        frozenset({"1001", "1002"}), frozenset({"1003", "1004"})]
    assert [c.anchor for c in camps] == ["1001", "1003"]


def _part(quantity, open_close, at, price, *, proceeds=0.0, pnl=0.0):
    from optjournal.history import FillPart

    return FillPart(quantity=quantity, open_close=open_close,
                    date_time=at, trade_price=price, proceeds=proceeds,
                    proceeds_base=proceeds, commission=-1.0, commission_base=-1.0,
                    realized_pnl=pnl, realized_pnl_base=pnl)


def test_a_leg_two_campaigns_took_is_divided_by_the_fills_each_took():
    """One order filled C at 1.50, then C;O at 1.60 through zero: the long took
    the first fill and the closing half of the second, the short the opening
    half. Each campaign carries its share of that leg in the leg's own columns,
    summed from its own fill parts; a leg only one campaign took carries none."""
    eps = [_Ep("C1", ["t1", "t2", "t3"], pnl=95.0), _Ep("C1", ["t3", "t4"], pnl=40.0)]
    eps[0].fill_parts = {
        "t1": _part(2, "O", "2026-09-10 10:00:00", 1.0, proceeds=-200.0),
        "t2": _part(-1, "C", "2026-09-15 10:00:00", 1.5, proceeds=150.0, pnl=48.0),
        "t3": _part(-1, "C", "2026-09-15 10:00:01", 1.6, proceeds=160.0, pnl=47.0),
    }
    eps[1].fill_parts = {
        "t3": _part(-1, "O", "2026-09-15 10:00:01", 1.6, proceeds=160.0),
        "t4": _part(1, "C", "2026-09-20 10:00:00", 1.0, proceeds=-100.0, pnl=40.0),
    }
    camps = link(eps, order_groups=[("open",), ("flip",), ("out",)],
                 order_of_trade={"t1": "open", "t2": "flip", "t3": "flip",
                                 "t4": "out"})
    assert [dict(c.leg_parts) for c in camps] == [
        {("flip", "C1"): {
            "quantity": -2, "fills": 2, "proceeds": 310.0, "proceeds_base": 310.0,
            "commission": -2.0, "commission_base": -2.0, "realized_pnl": 95.0,
            "realized_pnl_base": 95.0, "avg_price": pytest.approx(1.55),
            "first_fill_at": "2026-09-15 10:00:00",
            "last_fill_at": "2026-09-15 10:00:01", "open_close": "C"}},
        {("flip", "C1"): {
            "quantity": -1, "fills": 1, "proceeds": 160.0, "proceeds_base": 160.0,
            "commission": -1.0, "commission_base": -1.0, "realized_pnl": 0.0,
            "realized_pnl_base": 0.0, "avg_price": 1.6,
            "first_fill_at": "2026-09-15 10:00:01",
            "last_fill_at": "2026-09-15 10:00:01", "open_close": "O"}},
    ]


def test_a_share_holding_both_halves_of_a_split_counts_that_execution_once():
    """A card counts every execution it draws. When a hand link puts both sides
    of a reversal in one campaign while another campaign took an earlier fill of
    the same order, the leg is still divided, and the one execution whose two
    halves it holds is one fill, not two."""
    eps = [_Ep("C1", ["t1"]), _Ep("C1", ["t2", "t5"]), _Ep("C1", ["t2", "t3", "t6"])]
    eps[0].fill_parts = {"t1": _part(-1, "C", "2026-09-15 10:00:00", 1.0)}
    eps[1].fill_parts = {"t2": _part(-1, "C", "2026-09-15 10:00:01", 1.0),
                         "t5": _part(1, "C", "2026-09-16 10:00:00", 1.0)}
    eps[2].fill_parts = {"t2": _part(-1, "O", "2026-09-15 10:00:01", 1.0),
                         "t3": _part(-1, "O", "2026-09-15 10:00:02", 1.0),
                         "t6": _part(1, "O", "2026-09-17 10:00:00", 1.0)}
    camps = link(eps, order_groups=[("x",), ("y",), ("z",)],
                 order_of_trade={"t1": "x", "t2": "x", "t3": "x", "t5": "y",
                                 "t6": "z"},
                 links=[("y", "z")])
    fills = sorted(c.leg_parts[("x", "C1")]["fills"] for c in camps)
    assert fills == [1, 2], "t1 in one card; t2 (both halves) and t3 in the other"


def test_a_share_reads_as_what_it_took_first():
    """A share holding a closing AND an opening fill reads as the first, which is
    also what orders the shares of one order: by time, and on one split execution
    the share that took its closing half first."""
    from optjournal.campaigns import first_taken

    eps = [_Ep("C1", ["t1", "t3"]), _Ep("C1", ["t2"]), _Ep("C1", ["t4"]),
           _Ep("C1", ["t4", "t5"])]
    eps[0].fill_parts = {"t1": _part(-1, "C", "2026-09-15 10:00:02", 1.0),
                         "t3": _part(-1, "O", "2026-09-15 10:00:00", 1.0)}
    eps[1].fill_parts = {"t2": _part(-1, "O", "2026-09-15 10:00:01", 1.0)}
    # One split execution: the opening half is listed first, and still sorts last.
    eps[2].fill_parts = {"t4": _part(1, "O", "2026-09-20 10:00:00", 1.0)}
    eps[3].fill_parts = {"t4": _part(-2, "C", "2026-09-20 10:00:00", 1.0),
                         "t5": _part(-1, "C", "2026-09-18 10:00:00", 1.0)}
    x = dict.fromkeys(("t1", "t2", "t3"), "x") | {"t4": "y", "t5": "z"}
    shares = [dict(c.leg_parts) for c in link(
        eps, order_groups=[("x",), ("y",), ("z",)], order_of_trade=x)]
    assert shares[0][("x", "C1")]["open_close"] == "O", "its 10:00:00 fill opened"
    assert shares[1][("x", "C1")]["open_close"] == "O"
    assert first_taken(shares[2][("y", "C1")]) == ("2026-09-20 10:00:00", True)
    assert first_taken(shares[3][("y", "C1")]) == ("2026-09-20 10:00:00", False)
    assert first_taken(shares[3][("y", "C1")]) < first_taken(shares[2][("y", "C1")])


def test_a_flip_placed_with_another_contract_is_still_one_decision():
    """The same-contract exception only covers a group of ONE contract. A
    reversal filled in the same second as a leg on another contract is a
    multi-leg placement, and the group joins all of it as before."""
    eps = [_Ep("C1", ["t1", "t2"], pnl=198.0),
           _Ep("C1", ["t2"], closed=False),
           _Ep("P1", ["t3"], closed=False)]
    (camp,) = link(eps, order_groups=[("open",), ("flip", "hedge")],
                   order_of_trade={"t1": "open", "t2": "flip", "t3": "hedge"})
    assert camp.episode_indices == (0, 1, 2)


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


# ------------------------------------------------------------------- the anchor


def test_the_anchor_is_the_decisions_lowest_order_id():
    """A campaign's stable handle, for anything keyed on a decision.

    `episode_indices` cannot be one: they index the list `link` was handed, and
    every ingest rebuilds that list. An order id is IBKR's own and names one
    placement forever, so `journal.py` keys a reader's notes on it.
    """
    campaigns = link(
        [_Ep("C1", ["T1"]), _Ep("C2", ["T2"])],
        order_groups=_one_group("1241544750", "1241544513"),
        order_of_trade={"T1": "1241544750", "T2": "1241544513"},
    )
    assert len(campaigns) == 1
    assert campaigns[0].anchor == "1241544513"


def test_a_roll_added_later_does_not_move_the_anchor():
    """The property that makes it a key: a decision that GROWS keeps its handle.

    A roll opens a new contract under a new, higher order id. If the anchor were
    the newest order, or anything derived from membership, every roll would
    re-point a reader's notes at a fresh key and orphan what they wrote.
    """
    before = link(
        [_Ep("C1", ["T1"])],
        order_groups=_one_group("1247248833"),
        order_of_trade={"T1": "1247248833"},
    )
    after = link(
        [_Ep("C1", ["T1"]), _Ep("C2", ["T2"])],
        order_groups=_one_group("1247248833", "1299999999"),
        order_of_trade={"T1": "1247248833", "T2": "1299999999"},
    )
    assert before[0].anchor == after[0].anchor == "1247248833"


def test_the_anchor_compares_numerically_not_as_text():
    """`min` on strings ranks '999' above '1000'.

    True of the real ids only because they are all ten digits, which is the kind
    of accident that holds until IBKR issues a shorter one. Asserted on ids of
    different lengths, since equal lengths cannot tell the two orderings apart.
    """
    campaigns = link(
        [_Ep("C1", ["T1"]), _Ep("C2", ["T2"])],
        order_groups=_one_group("999", "1000"),
        order_of_trade={"T1": "999", "T2": "1000"},
    )
    assert campaigns[0].anchor == "999", "text ordering would have chosen 1000"


def test_a_non_numeric_order_id_sorts_last_rather_than_raising():
    """A broker that labels an order 'A17' must not break a page render.

    Numbers first, so the anchor stays IBKR's earliest placement whenever one is
    present, and the odd label is merely last instead of an exception thrown
    halfway through building the payload.
    """
    campaigns = link(
        [_Ep("C1", ["T1"]), _Ep("C2", ["T2"])],
        order_groups=_one_group("A17", "1000"),
        order_of_trade={"T1": "A17", "T2": "1000"},
    )
    assert campaigns[0].anchor == "1000"


def test_a_campaign_with_no_fills_has_no_anchor():
    """The LEAP held from before the archive: a position with no orders.

    None rather than a placeholder, because two such campaigns would collide on
    any placeholder chosen -- so `journal.save` refuses them explicitly instead
    of quietly filing both under one key.
    """
    campaigns = link([_Ep("C1", [])], order_groups=[], order_of_trade={})
    assert campaigns[0].anchor is None


# ------------------------------------------------------------ links by hand


def _two_separate_decisions():
    """A put closed at a loss on Monday and a new one opened on Tuesday: a roll
    the window cannot see, because the two orders are a day apart."""
    eps = [_Ep("A", ["t1", "t2"], pnl=-1200.0), _Ep("B", ["t3", "t4"], pnl=50.0)]
    groups = [("a1",), ("a2",), ("b1",), ("b2",)]
    orders = {"t1": "a1", "t2": "a2", "t3": "b1", "t4": "b2"}
    return eps, groups, orders


def test_a_roll_the_window_missed_is_one_decision_once_linked_by_hand():
    """Without the link it scores a win and a loss; with it, one loss of 1150,
    which is the losing-roll case the module docstring opens with."""
    eps, groups, orders = _two_separate_decisions()
    assert len(link(eps, order_groups=groups, order_of_trade=orders)) == 2

    (camp,) = link(eps, order_groups=groups, order_of_trade=orders,
                   links=[("a1", "b1")])
    assert camp.episode_indices == (0, 1)
    assert camp.realized is not None and camp.realized.base == -1150.0
    assert camp.links == (("a1", "b1"),)


def test_a_link_naming_an_order_no_episode_filled_is_skipped():
    """Another category's order, say. The row is the reader's, so it is left
    alone rather than raised, and the cards stay as the window built them."""
    eps, groups, orders = _two_separate_decisions()
    camps = link(eps, order_groups=groups, order_of_trade=orders,
                 links=[("a1", "zz")])
    assert len(camps) == 2
    assert all(c.links == () for c in camps)


def _link_lines(n: int) -> int:
    """Lines `link` runs, helpers included, for `n` round trips on one contract
    whose orders are one 90-second chain, scalping-style."""
    import sys

    from optjournal import campaigns

    eps = [_Ep("C1", [f"o{k}", f"c{k}"]) for k in range(n)]
    orders = {f"{side}{k}": f"{side}{k}" for k in range(n) for side in "oc"}
    chain = [tuple(orders.values())]
    count = 0

    def local(frame, event, arg):
        nonlocal count
        count += event == "line"
        return local

    sys.settrace(lambda frame, event, arg: (
        local if frame.f_code.co_filename == campaigns.__file__ else None))
    try:
        assert len(link(eps, order_groups=chain, order_of_trade=orders)) == n
    finally:
        sys.settrace(None)
    return count


def test_a_long_window_chain_costs_its_length_not_its_square():
    """A scalper's day is one window chain of hundreds of orders, and reading an
    anchor across every order of a card's groups made the linkage grow with the
    square of its length (600 round trips a day: 32 to 622 ms). Counted in lines
    run rather than timed: three times the chain is about three times the work."""
    small, large = _link_lines(50), _link_lines(150)
    assert large < 4 * small, (small, large)
