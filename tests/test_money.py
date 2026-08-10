"""The Money value type: what it guarantees, and what it makes unrepresentable.

Each test here pins a property that used to be an inspection obligation --
something a reader had to verify by eye across two or three fields, and which
therefore drifted. See money.py's docstring for the defects that motivated it.
"""

import pytest

from optjournal.money import Charge, Money, one_currency


def test_one_currency_answers_only_for_a_single_currency_scope():
    """The judgement, in isolation. A native figure is exact but unaddable, so
    it can only speak for a scope that one currency accounts for."""
    assert one_currency({"USD": -6.97}) == (-6.97, "USD")
    assert one_currency({"USD": -4.46, "SEK": -208.41}) == (None, None)
    assert one_currency({}) == (None, None)
    # A zero-amount currency is not a second currency: USD option trades plus a
    # free EUR conversion row is still honestly a USD figure.
    assert one_currency({"USD": -6.97, "EUR": 0.0}) == (-6.97, "USD")
    assert one_currency({"EUR": 0.0}) == (None, None)


def test_an_amount_without_its_currency_cannot_be_constructed():
    """The invariant the three-field spelling could not express.

    `x_native` and `x_native_ccy` were separate attributes on a mutable
    dataclass, so a figure could be left half-assigned -- an amount with no
    currency to label it, or a currency naming no amount -- and nothing
    complained until a card rendered `$None` or a bare number.
    """
    with pytest.raises(ValueError, match="both be set or both absent"):
        Money(base=1.0, native=2.0)
    with pytest.raises(ValueError, match="both be set or both absent"):
        Money(base=1.0, currency="USD")


def test_a_figure_cannot_be_half_updated():
    """Frozen, so advancing an amount without its currency is not expressible.

    This is why the per-fill loop accumulates into a local and builds the
    figure once, rather than `+=`-ing a base field and hoping the ledger beside
    it was updated in the same branch.
    """
    charge = Money.gated(-6.07, {"USD": -6.97})
    with pytest.raises(AttributeError):
        charge.native = -99.0


def test_gated_withholds_rather_than_reporting_part_of_a_total():
    """A mixed scope gets no native figure at all.

    An exact-looking number covering part of a total is worse than an honest
    approximation of all of it, because only the second is labelled.
    """
    exact = Money.gated(-6.07, {"USD": -6.97})
    assert (exact.native, exact.currency, exact.is_exact) == (-6.97, "USD", True)

    mixed = Money.gated(-6.07, {"USD": -4.46, "SEK": -208.41})
    assert (mixed.native, mixed.currency, mixed.is_exact) == (None, None, False)
    # The base figure survives: it is the complete one.
    assert mixed.base == -6.07


def test_restated_is_a_statement_about_the_data_not_a_gap():
    """`friction` includes the estimated AutoFX markup, which IBKR never billed
    as a line item in any currency. `restated` says so; it is not a figure
    whose native half someone forgot to fill in."""
    friction = Money.restated(9.16)
    assert (friction.base, friction.native, friction.is_exact) == (9.16, None, False)


def test_charged_accumulates_and_gates_in_one_pass():
    """Replaces the five-line ledger block that was written out four times.

    Rows with no native amount contribute to base only -- a zero-commission
    conversion row must not make the scope look multi-currency.
    """
    single = Money.charged([(-3.00, -3.50, "USD"), (-3.07, -3.47, "USD")])
    assert single.base == pytest.approx(-6.07)
    assert single.native == pytest.approx(-6.97)
    assert single.currency == "USD"

    spanning = Money.charged([(-3.00, -3.50, "USD"), (-3.07, -20.0, "SEK")])
    assert spanning.base == pytest.approx(-6.07)
    assert not spanning.is_exact

    # A row carrying base but no native (or no currency) is still counted in
    # base and ignored by the gate.
    partial = Money.charged([(-3.00, -3.50, "USD"), (-1.00, None, None)])
    assert partial.base == pytest.approx(-4.00)
    assert (partial.native, partial.currency) == (-3.50, "USD")

    assert Money.charged([]) == Money(base=0.0)


def test_abs_carries_the_currency_with_the_amount():
    """The drift this type exists to prevent, in one line.

    `options_friction` used to be two properties: `abs(commissions_native)` for
    the amount, and `commissions_native_ccy` -- a field on a DIFFERENT figure --
    for the label. Correct only while friction happened to be commission alone.
    Taking a magnitude while leaving the currency behind is now impossible.
    """
    charge = Money.gated(-6.07, {"USD": -6.97})
    assert abs(charge) == Money(base=6.07, native=6.97, currency="USD")
    # A withheld figure stays withheld under abs -- no currency appears from
    # nowhere.
    assert abs(Money.restated(-6.07)) == Money(base=6.07)


def test_per_divides_both_halves_by_the_same_quantity():
    """A per-unit figure cannot be an as-charged numerator over a restated
    denominator, which two separate divisions had to agree on by inspection."""
    per = Money.gated(-6.07, {"USD": -6.97}).per(10)
    assert per is not None
    assert per.base == pytest.approx(-0.607)
    assert per.native == pytest.approx(-0.697)
    assert per.currency == "USD"
    # No quantity, no rate -- and None rather than a division by zero.
    assert Money.gated(-6.07, {"USD": -6.97}).per(0) is None


def test_at_rate_derives_base_from_the_row_that_owns_the_rate():
    """For figures IBKR reports natively with no base column -- a position's
    cost basis and unrealised P&L -- the base is derived from that row's own
    `fxRateToBase`. A single row is single-currency by construction, so the gate
    has nothing to decide and the native is always offered.
    """
    # The real LEAP row: 818.30 USD at 0.86714 is the 709.580662 IBKR stored.
    value = Money.at_rate(818.30, 0.86714, "USD")
    assert value.native == 818.30
    assert value.base == pytest.approx(709.580662)
    assert value.currency == "USD"

    # No rate to convert with: the native stands as its own base rather than
    # the figure vanishing.
    assert Money.at_rate(818.30, None, "USD") == Money(
        base=818.30, native=818.30, currency="USD")
    # Nothing to interpret -- a zero base, not a half-set figure.
    assert Money.at_rate(None, 0.86714, "USD") == Money(base=0.0)
    assert Money.at_rate(818.30, 0.86714, None) == Money(base=818.30)


def test_the_payload_shape_is_stable_so_the_page_tests_for_null():
    """All three keys are always present. The page checks for a null value and
    never for a missing property, so an absent key can never be mistaken for a
    withheld figure."""
    assert Money.gated(-6.07, {"USD": -6.97}).payload() == {
        "base": -6.07, "native": -6.97, "ccy": "USD",
    }
    assert Money.restated(9.16).payload() == {
        "base": 9.16, "native": None, "ccy": None,
    }
    assert set(Money.restated(0.0).payload()) == {"base", "native", "ccy"}


# --- Charge: a cost that keeps every currency it was billed in ---------------
#
# `Money` withholds the native figure the moment a scope spans currencies, which
# is right for any figure and wrong for a cost report the reader scopes: the
# charges are all still known, so widening from options to the whole account
# should add columns, not delete exactness. These pin that difference.


def test_a_charge_keeps_every_billing_currency():
    """The property `Money.gated` cannot express: four charges, none discarded."""
    c = Charge.of([
        (15.16, 17.46, "USD"),
        (1.66, 1.66, "EUR"),
        (18.90, 208.41, "SEK"),
    ])
    assert c.by_ccy == {"USD": 17.46, "EUR": 1.66, "SEK": 208.41}
    assert c.base == pytest.approx(35.72)


def test_a_single_currency_charge_still_answers_as_money():
    """Where the two types overlap they must agree, or a reader checking one
    surface against another finds two different figures for one cost."""
    c = Charge.of([(15.16, 17.46, "USD")])
    assert c.money == Money(base=15.16, native=17.46, currency="USD")
    assert c.money.is_exact


def test_a_mixed_charge_withholds_the_single_figure_but_not_the_ledger():
    """The whole point: the headline falls back to base, the detail survives.

    `Money.gated`'s rule is delegated to rather than reimplemented, so the
    fallback can never disagree with the rest of the payload.
    """
    c = Charge.of([(15.16, 17.46, "USD"), (18.90, 208.41, "SEK")])
    assert not c.money.is_exact
    assert c.money.base == pytest.approx(34.06)
    assert c.by_ccy == {"USD": 17.46, "SEK": 208.41}


def test_charges_add_by_merging_their_ledgers():
    """Addition is what makes the tab scopeable: the page sums the categories
    the reader ticked, and each currency stays its own column through the sum."""
    opt = Charge.of([(15.16, 17.46, "USD")])
    stk = Charge.of([(1.66, 1.66, "EUR"), (4.46, 5.16, "USD")])
    total = opt + stk
    assert total.by_ccy == {"USD": pytest.approx(22.62), "EUR": 1.66}
    assert total.base == pytest.approx(21.28)
    # Addition does not mutate either operand -- both are frozen.
    assert opt.by_ccy == {"USD": 17.46}


def test_a_zero_amount_currency_is_not_a_currency():
    """Same rule as `one_currency`, applied at construction.

    A USD option scope plus a zero-commission EUR conversion row is a USD
    charge; carrying `EUR: 0.0` would make it mixed and cost it its exactness.
    """
    c = Charge.of([(15.16, 17.46, "USD"), (0.0, 0.0, "EUR")])
    assert c.by_ccy == {"USD": 17.46}
    assert c.money.is_exact


def test_nothing_charged_is_distinct_from_an_estimated_cost():
    """Two different zeros, and the AutoFX markup is the reason it matters.

    An empty ledger with a zero base is "no cost". An empty ledger with a
    non-zero base is a cost IBKR never itemised in any currency -- the rate
    markup -- and it must never render as an as-charged figure.
    """
    assert Charge.of([]).is_free
    estimated = Charge(base=12.83)
    assert not estimated.is_free
    assert not estimated.money.is_exact
    assert estimated.by_ccy == {}


def test_charge_payload_carries_moneys_keys_plus_the_ledger():
    """A consumer already reading a money-shaped payload needs no new branch."""
    p = Charge.of([(15.16, 17.46, "USD")]).payload()
    assert p == {"base": 15.16, "native": 17.46, "ccy": "USD",
                 "charged": {"USD": 17.46}}
    assert set(Charge.of([]).payload()) == {"base", "native", "ccy", "charged"}


def test_charge_magnitude_carries_every_currency():
    """Cost is presented positive, and taking the magnitude of the total while
    leaving the ledger signed would make the breakdown disagree with it."""
    c = abs(Charge.of([(-15.16, -17.46, "USD"), (-18.90, -208.41, "SEK")]))
    assert c.base == pytest.approx(34.06)
    assert c.by_ccy == {"USD": 17.46, "SEK": 208.41}


def test_money_stays_a_leaf_module():
    """Money is a value type, so every layer may hold one without acquiring a
    dependency direction. The moment it imports another journal module, that
    stops being true and `analysis.py` -- which imports nothing internal -- can
    no longer be handed one.
    """
    import ast
    import pathlib

    src = pathlib.Path("src/optjournal/money.py").read_text()
    internal = [
        node
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("optjournal")
    ]
    assert not internal, (
        "money.py imported "
        f"{[n.module for n in internal]} -- it must stay a leaf so any layer can hold one"
    )
