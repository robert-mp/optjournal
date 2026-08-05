"""The Money value type: what it guarantees, and what it makes unrepresentable.

Each test here pins a property that used to be an inspection obligation --
something a reader had to verify by eye across two or three fields, and which
therefore drifted. See money.py's docstring for the defects that motivated it.
"""

import pytest

from optjournal.money import Money, one_currency


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
