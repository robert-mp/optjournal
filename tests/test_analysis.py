"""Tests for cost analysis, run against real archived statements."""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from conftest import RAW_DIR, STATEMENTS

from optjournal.analysis import (
    AUTOFX_MARKUP_BPS,
    WithholdingLine,
    _commission_to_base,
    analyse,
    categorise_fee,
    format_report,
)
from optjournal.flex import load
from optjournal.serialize import costs_data

ZERO = Decimal("0")


@pytest.fixture(params=STATEMENTS, ids=lambda p: p.name)
def statement(request):
    return load(request.param).FlexStatements[0]


@pytest.mark.parametrize(
    "description,expected",
    [
        ("R*******99:OPRA NP L1 FOR JUN 2026", "Market data"),
        ("KRW CUSTODY FEE ON STK FOR 2026-05-29 FOR JUN 2026", "Custody"),
        ("WITHHOLDING @ 20% ON CREDIT INT FOR MAY-2026", "Interest"),
        ("SOMETHING ENTIRELY NEW", "Other"),
        (None, "Other"),
        ("", "Other"),
    ],
)
def test_categorise_fee(description, expected):
    assert categorise_fee(description) == expected


def test_analyse_runs_and_totals_are_consistent(statement):
    r = analyse(statement)
    assert r.total_fx_notional_base == sum(p.notional_base for p in r.fx)
    assert r.total_fees_base == sum(c.total_base for c in r.fees)
    assert r.total_fx_notional_base >= ZERO
    assert r.total_fees_base >= ZERO


def test_fx_notional_is_unsigned(statement):
    """Proceeds are signed by direction; notional must be a magnitude."""
    for pair in analyse(statement).fx:
        assert pair.notional_base >= ZERO, pair.symbol
        assert pair.commission_base >= ZERO, pair.symbol


def test_fee_counts_match_statement(statement):
    expected = sum(
        1 for c in (statement.CashTransactions or ()) if "FEES" in str(c.type).upper()
    )
    assert sum(c.count for c in analyse(statement).fees) == expected


def test_format_report_does_not_raise(statement):
    assert "Cost report" in format_report(analyse(statement))


def _cash(kind, amount, currency="EUR", rate="1", symbol=None, description=""):
    """A CashTransaction, named by its enum MEMBER (FEES, WHTAX, BROKERINTRCVD).

    Member name rather than the human label, because `analyse` reads
    `str(c.type).upper()` and py_ibkr's str() yields 'CashAction.FEES' -- so the
    text the code branches on is the member name, not IBKR's 'Other Fees'. A
    fixture that used the label would test a string the code never sees.
    """
    return SimpleNamespace(
        # A plain string, not SimpleNamespace(__str__=...): dunder lookup goes
        # to the TYPE, so an instance attribute named __str__ is never called
        # and the fixture would silently stringify as "namespace(...)".
        type=f"CashAction.{kind}",
        amount=Decimal(amount),
        currency=currency,
        fxRateToBase=Decimal(rate),
        symbol=symbol,
        description=description,
    )


def _cash_stmt(cash):
    return SimpleNamespace(
        fromDate="20250801", toDate="20260731", Trades=[], CashTransactions=cash,
    )


# ------------------------------------------------------- cash classification
#
# Three separate gates read the same `kind` string, and each was unguarded.
# They share a failure mode: a cash row landing in the wrong bucket still
# produces a well-formed report, just with the wrong number in it.


def test_only_fee_rows_are_counted_as_fees():
    """The gate is a SUBSTRING test against an enum member name.

    That is the hazard worth pinning: `CashAction.BROKERINTRCVD` does not
    contain 'INTEREST' but does contain 'INT', so a well-meaning widening of
    this gate books interest you RECEIVED as a cost you paid -- and the archive
    holds three such rows. Deposits are the same shape of mistake with a much
    larger number attached.
    """
    r = analyse(_cash_stmt([
        _cash("FEES", "-1.30", description="OPRA NP L1"),
        _cash("BROKERINTRCVD", "4.20"),
        _cash("BROKERINTPAID", "-0.80"),
        _cash("DEPOSITWITHDRAW", "-5000.00"),
    ]))
    assert sum(c.count for c in r.fees) == 1, (
        f"{[(c.name, c.count) for c in r.fees]} -- only the FEES row is a fee"
    )
    assert sum(c.total_base for c in r.fees) == Decimal("1.30")


def test_a_fee_is_denominated_in_its_own_currency_not_the_base():
    """A CashTransaction carries its own currency, and 313 archived fees are KRW.

    Attributing them to the base would put a KRW figure under a EUR label --
    the as-charged half of `Money` exists precisely so a fee can be shown in
    what it was actually billed in. The base total stays EUR either way, which
    is why this is invisible without asserting on the native ledger.
    """
    r = analyse(_cash_stmt([
        _cash("FEES", "-1490.00", currency="KRW", rate="0.00057865",
              description="KRW CUSTODY FEE ON STK"),
    ]), base_currency="EUR")
    cat = r.fees[0]
    assert set(cat.native_by_ccy) == {"KRW"}, (
        f"billed in {set(cat.native_by_ccy)}, but the row says KRW"
    )
    assert cat.native_by_ccy["KRW"] == Decimal("1490.00")
    # The base translation is unchanged -- only the denomination was at stake.
    assert cat.total_base == Decimal("1490.00") * Decimal("0.00057865")


def test_withholding_is_a_positive_amount_however_ibkr_signs_it():
    """IBKR sends WHTAX as a NEGATIVE amount; the rate must not invert.

    All ten archived withholding rows are negative. Accumulated verbatim, the
    withheld total goes negative, and `effective_rate` -- withheld/gross -- returns
    a negative percentage on a real tax that was really paid. The dividend keeps
    its sign because a dividend is income.
    """
    r = analyse(_cash_stmt([
        _cash("DIVIDEND", "1.00", symbol="ACME"),
        _cash("WHTAX", "-0.15", symbol="ACME"),
    ]))
    line = next(w for w in r.withholding if w.symbol == "ACME")
    assert line.withheld_base == Decimal("0.15"), "the withheld amount stayed negative"
    assert line.gross_base == Decimal("1.00")
    assert line.effective_rate == Decimal("15")
    assert line.effective_rate > ZERO, "a tax paid cannot be a negative rate"


def test_a_fee_refund_nets_off_the_charge_it_cancels():
    """`activity-20260903`: 1.30 charged, the same 1.30 cancelled, 1.29 charged.

    The statement's EUR market data cost 1.29. Each row's magnitude made it 3.89,
    booking the cancellation as another charge.
    """
    r = analyse(_cash_stmt([
        _cash("FEES", "-1.30", description="OPRA NP L1 FOR AUG 2026"),
        _cash("FEES", "1.30", description="CANCEL[OPRA NP L1] FOR AUG 2026"),
        _cash("FEES", "-1.29", description="OPRA NP L1 FOR SEP 2026"),
    ]))
    (cat,) = r.fees
    assert cat.count == 3
    assert cat.total_base == Decimal("1.29")
    assert cat.native_by_ccy == {"EUR": Decimal("1.29")}
    assert r.total_fees_base == Decimal("1.29")


def test_a_withholding_refund_reduces_what_was_withheld():
    """A reclaimed tax arrives as a POSITIVE WHTAX row and must net off."""
    r = analyse(_cash_stmt([
        _cash("DIVIDEND", "1.00", symbol="ACME"),
        _cash("WHTAX", "-0.30", symbol="ACME"),
        _cash("WHTAX", "0.15", symbol="ACME"),
    ]))
    line = next(w for w in r.withholding if w.symbol == "ACME")
    assert line.withheld_base == Decimal("0.15")


def test_withholding_over_the_real_statement_is_never_negative(statement):
    """The same invariant over the archive, which is where the signs came from."""
    for w in analyse(statement).withholding:
        assert w.withheld_base >= ZERO, w.symbol
        if w.effective_rate is not None:
            assert w.effective_rate >= ZERO, w.symbol


def test_withholding_without_dividend_has_no_rate():
    """WHTAX on credit interest has no dividend counterpart, so no rate."""
    line = WithholdingLine(
        symbol="(non-dividend)",
        currency="SEK",
        gross_base=ZERO,
        withheld_base=Decimal("0.17"),
    )
    assert line.effective_rate is None


def test_withholding_rate_is_gross_relative():
    """IBKR's Dividends row is the GROSS amount, so the rate is withheld/gross.

    Dividing by gross plus withheld read a 30% rate as about 23%.
    """
    line = WithholdingLine(
        symbol="ACME",
        currency="USD",
        gross_base=Decimal("80"),
        withheld_base=Decimal("24"),
    )
    assert line.effective_rate == Decimal("30")


def test_zero_notional_pair_has_no_bps():
    from optjournal.analysis import FxPair

    assert FxPair(symbol="EUR.USD").commission_bps is None


# --- AutoFX rate markup ------------------------------------------------------
#
# IBKR charges auto-conversions via a rate markup, not commission, so the cost
# is invisible in the statement. These pin the constant and, more importantly,
# pin that it is applied ONLY to flagged conversions -- the failure mode worth
# guarding is silently charging a markup on a manual conversion that already
# paid a real commission.


class _Note:
    """Stands in for py_ibkr's Code enum, which exposes a wire `value`."""

    def __init__(self, value):
        self.value = value


def _conv(symbol, proceeds, notes=(), commission="0", rate="1"):
    # `notes` is passed through as given, NOT list()-ed: a note field legitimately
    # arrives as the `;`-joined string as well as as a sequence, and list("AFx;P")
    # would silently make a test of the string form a test of six characters.
    return SimpleNamespace(
        assetCategory=SimpleNamespace(value="CASH"),
        symbol=symbol,
        proceeds=Decimal(proceeds),
        ibCommission=Decimal(commission),
        taxes=Decimal("0"),
        fxRateToBase=Decimal(rate),
        quantity=None,
        notes=notes if isinstance(notes, str) else list(notes),
    )


def _stmt(trades):
    return SimpleNamespace(
        fromDate="20250801", toDate="20260731",
        Trades=trades, CashTransactions=[],
    )


def test_autofx_markup_is_three_bps():
    """Published rate for IBKR Ireland: 0.03% == 3 bps."""
    assert Decimal("3") == AUTOFX_MARKUP_BPS


def test_markup_applied_to_flagged_conversion():
    r = analyse(_stmt([_conv("EUR.USD", "-10000", notes=[_Note("AFx")])]))
    pair = r.fx[0]
    assert pair.autofx_conversions == 1
    assert pair.autofx_notional_base == Decimal("10000")
    # 10,000 * 3bps = 3.00
    assert pair.autofx_spread_base == Decimal("3")
    assert r.total_autofx_spread_base == Decimal("3")


def test_markup_not_applied_to_unflagged_conversion():
    """A manual IDEALPRO conversion pays commission, not a markup."""
    r = analyse(_stmt([_conv("EUR.USD", "-10000", commission="-2")]))
    pair = r.fx[0]
    assert pair.conversions == 1
    assert pair.autofx_conversions == 0
    assert pair.autofx_notional_base == Decimal("0")
    assert pair.autofx_spread_base == Decimal("0")
    assert r.total_autofx_spread_base == Decimal("0")
    # the real commission is still counted
    assert r.total_commission_base == Decimal("2")


def test_markup_scoped_per_conversion_not_per_pair():
    """Mixed pair: only the AFx leg is marked up, not the pair's total."""
    r = analyse(_stmt([
        _conv("EUR.USD", "-10000", notes=[_Note("AFx")]),
        _conv("EUR.USD", "-90000", commission="-18"),
    ]))
    pair = r.fx[0]
    assert pair.conversions == 2
    assert pair.notional_base == Decimal("100000")
    assert pair.autofx_conversions == 1
    # 3bps of 10,000 -- NOT of 100,000
    assert pair.autofx_spread_base == Decimal("3")


def test_autofx_flag_survives_extra_codes():
    """Real data carries `AFx;P` (auto-conversion + partial fill)."""
    r = analyse(_stmt([
        _conv("EUR.USD", "-10000", notes=[_Note("AFx"), _Note("P")]),
    ]))
    assert r.fx[0].autofx_conversions == 1


def test_autofx_flag_matches_plain_string_note():
    """compat may mint an ad-hoc member; wire-value comparison must still work."""
    r = analyse(_stmt([_conv("EUR.USD", "-10000", notes=["AFx"])]))
    assert r.fx[0].autofx_conversions == 1


@pytest.mark.parametrize("notes", ["AFx", "AFx;P", "P;AFx", " AFx ; P "])
def test_autofx_flag_read_from_the_joined_string_form(notes):
    """The shape `sources.py` STORES, not the list py_ibkr happens to hand over.

    Every other test here builds the pre-split list, which is why comparing the
    whole field to "AFx" passed for as long as it did: on the statement path
    py_ibkr always splits first. The database path stores `";".join(...)`, so a
    reader of it sees "AFx;P" -- unequal to "AFx", and its markup silently
    unestimated. Two real conversions in this archive carry exactly that.

    Parametrized over the orderings and the whitespace because the rule is
    token equality, and each of these breaks a different shortcut: a prefix
    comparison passes "AFx;P" and fails "P;AFx", and neither survives padding.
    """
    r = analyse(_stmt([_conv("EUR.USD", "-10000", notes=notes)]))
    assert r.fx[0].autofx_conversions == 1
    assert r.fx[0].autofx_spread_base == Decimal("3")


def test_a_note_code_is_not_matched_as_a_substring():
    """`A` is assignment, and it is a substring of `AFx`.

    The inverse of the defect above, and the reason the shared rule splits
    rather than searches: a conversion flagged only `A` must not be read as an
    auto-conversion and charged a markup it never incurred.
    """
    r = analyse(_stmt([_conv("EUR.USD", "-10000", notes="A", commission="-2")]))
    assert r.fx[0].autofx_conversions == 0
    assert r.total_autofx_spread_base == ZERO
    assert r.total_commission_base == Decimal("2")


def test_friction_splits_stated_from_estimated():
    r = analyse(_stmt([
        _conv("EUR.USD", "-10000", notes=[_Note("AFx")]),
        _conv("EUR.SEK", "-10000", commission="-2"),
    ]))
    assert r.total_stated_friction_base == Decimal("2")
    assert r.total_autofx_spread_base == Decimal("3")
    assert r.total_friction_base == Decimal("5")


def test_caveat_reports_autofx_share():
    r = analyse(_stmt([
        _conv("EUR.USD", "-10000", notes=[_Note("AFx")]),
        _conv("EUR.USD", "-10000"),
    ]))
    assert "1 of 2 conversions are AutoFX" in r.fx_caveat
    assert "estimated" in r.fx_caveat


def test_caveat_when_no_autofx():
    r = analyse(_stmt([_conv("EUR.USD", "-10000", commission="-2")]))
    assert "No conversion carries the AutoFX flag" in r.fx_caveat


# --- JSON serialisation ------------------------------------------------------
#
# `dataclasses.asdict()` serialises fields only, so every computed total was
# silently missing from `costs --json`: consumers got the raw components and
# none of the answers. These pin the contract so the trap cannot return.


def _real_report():
    paths = sorted(RAW_DIR.glob("activity-*.xml"))
    if not paths:
        pytest.skip("needs an archived statement")
    return analyse(load(paths[-1]).FlexStatements[0])


def test_costs_data_includes_every_computed_total():
    data = costs_data(analyse(_stmt([_conv("EUR.USD", "-10000")])))
    for key in (
        "commission", "fees", "taxes", "autofx_notional_base",
        "autofx_spread_base", "stated_friction", "friction_base",
        "fx_notional_base", "fx_commission",
    ):
        assert key in data["totals"], f"totals.{key} missing"


def test_costs_data_totals_are_self_consistent():
    report = _real_report()
    t = costs_data(report)["totals"]
    assert t["friction_base"] == pytest.approx(
        t["stated_friction"]["base"] + t["autofx_spread_base"]
    )
    assert t["stated_friction"]["base"] == pytest.approx(
        t["commission"]["base"] + t["fees"]["base"] + t["taxes"]["base"]
    )
    assert t["friction_base"] == pytest.approx(float(report.total_friction_base))


def test_costs_data_money_is_numeric_not_string():
    """Decimal + json.dumps(default=str) silently emits money as strings."""
    import json
    data = costs_data(analyse(_stmt([_conv("EUR.USD", "-10000")])))
    assert isinstance(data["totals"]["friction_base"], float)
    assert isinstance(data["fx"][0]["notional_base"], float)
    round_tripped = json.loads(json.dumps(data))
    assert isinstance(round_tripped["totals"]["friction_base"], (int, float))


def test_costs_data_includes_per_pair_properties():
    """autofx_spread_base and commission_bps are properties, so asdict drops them."""
    data = costs_data(analyse(_stmt([_conv("EUR.USD", "-10000")])))
    assert data["fx"], "expected at least one FX pair"
    for pair in data["fx"]:
        assert "autofx_spread_base" in pair
        assert "commission_bps" in pair


def test_costs_data_autofx_spread_sums_to_total():
    data = costs_data(analyse(_stmt([_conv("EUR.USD", "-10000")])))
    assert sum(p["autofx_spread_base"] for p in data["fx"]) == pytest.approx(
        data["totals"]["autofx_spread_base"]
    )


def test_costs_data_is_json_serialisable():
    import json
    json.dumps(costs_data(_real_report()), default=str)


# ---------------------------------------------------------------- sign handling
#
# IBKR states commission as negative-is-a-charge, and on a split order it
# charges the per-order minimum against one fill then credits part of it back
# on another. Taking abs() per fill inverted those credits, so a credit of c
# was booked as +c instead of -c and the total came out 2c too high.


def _fill(asset, commission, qty="1", rate="1", taxes="0"):
    return SimpleNamespace(
        assetCategory=SimpleNamespace(value=asset),
        symbol="TSLA",
        proceeds=Decimal("-1000"),
        ibCommission=Decimal(commission),
        taxes=Decimal(taxes),
        fxRateToBase=Decimal(rate),
        quantity=Decimal(qty),
        notes=[],
    )


def test_commission_credit_is_netted_not_added():
    """The real defect: order 1096738670's two fills must net, not accumulate.

    Charged -0.3481 on the 2-share fill and credited +0.0088 on the 8-share
    fill; IBKR shows the order as having paid 0.3393. The old per-fill abs()
    reported 0.3569 -- 0.0176 too high, exactly twice the credit.
    """
    r = analyse(_stmt([
        _fill("STK", "-0.3481", qty="2"),
        _fill("STK", "0.0088", qty="8"),
    ]))
    stk = r.commissions[0]
    assert stk.commission_base == Decimal("0.3393")
    assert stk.credit_fills == 1
    # The figure the bug produced, asserted so a regression is unambiguous.
    assert stk.commission_base != Decimal("0.3569")


def test_a_charged_fill_is_not_counted_as_a_credit():
    """`credit_fills` counts credits, so a charge must not increment it.

    The count is the fingerprint the report prints a note about ("N fill(s)
    carried a commission credit"), so a gate of `!= ZERO` rather than `> ZERO`
    turns that note into a claim that every fill was credited. The two credit
    tests above happen to fail on that widening, but incidentally -- they assert
    a count of 1 for other reasons. This one is about the gate itself.
    """
    r = analyse(_stmt([_fill("STK", "-0.35"), _fill("STK", "-0.35")]))
    assert r.commissions[0].credit_fills == 0, "a charge was booked as a credit"
    assert "commission credit" not in format_report(r)
    # A zero commission is not a credit either: nothing was given back.
    assert analyse(_stmt([_fill("STK", "0")])).commissions[0].credit_fills == 0


def test_a_net_credit_category_reports_negative_cost():
    """A category that was net credited is a negative cost, not a positive one."""
    r = analyse(_stmt([_fill("STK", "0.50"), _fill("STK", "-0.20")]))
    assert r.commissions[0].commission_base == Decimal("-0.30")
    assert r.commissions[0].credit_fills == 1


def test_taxes_use_the_same_sign_convention():
    r = analyse(_stmt([_fill("OPT", "-1.00", taxes="-0.25")]))
    assert r.commissions[0].taxes_base == Decimal("0.25")


# ---------------------------------------------------------- fractional lots
#
# `quantity` is the denominator of `per_unit_base`, and it used to accumulate
# `int(abs(t.quantity))` -- truncating EVERY FILL toward zero before adding.
# Truncating per fill rather than once at the end is what made it severe: any
# fill under one whole unit contributed nothing at all. The archive holds
# 0.0007-share IBKR fills and 1.79-share dividend-reinvestment buys, so this is
# not a hypothetical shape. db.py's schema note and `ingest._quantity` both
# already treat a fractional lot as lossless; this is the third place that has
# to agree, and it was the one that did not.


def test_a_sub_unit_fill_is_not_truncated_to_nothing():
    """One 0.5-share fill must contribute 0.5, not 0.

    The narrowest statement of the bug: with `int()`, quantity stays 0, so
    `per_unit_base` returns None and the per-unit column shows a dash. A dash
    reads as "not applicable" rather than "we discarded your denominator",
    which is why this was invisible.
    """
    r = analyse(_stmt([_fill("STK", "-0.35", qty="0.5")]))
    g = r.commissions[0]
    assert g.quantity == Decimal("0.5"), "a sub-unit fill was truncated away"
    assert g.per_unit_base == Decimal("0.7"), "0.35 over 0.5 shares is 0.70"


def test_truncation_does_not_compound_across_fills():
    """The severity: per-fill truncation loses more than the final fraction.

    A thousand half-share buys are 500 shares. Truncating each fill first gives
    0 -- so the figure does not merely round, it disappears, and the more
    fractional the account the worse it gets. Asserted against the arithmetic
    the old code produced so a regression names itself.
    """
    r = analyse(_stmt([_fill("STK", "-0.35", qty="0.5") for _ in range(1000)]))
    g = r.commissions[0]
    assert g.quantity == Decimal("500.0")
    assert g.per_unit_base == Decimal("0.7")
    assert g.fills == 1000, "the fill count was never in doubt; the units were"


def test_a_mixed_lot_keeps_the_fraction_the_statement_stated(statement):
    """Over the real archive: the group total equals the sum of the fills.

    Recomputed from the statement rather than hard-coded, so this holds as the
    corpus grows. Two archived statements carry a 5089.0013-share stock total
    whose .0013 comes from two dividend-reinvestment fills.
    """
    r = analyse(statement)
    expected: dict[str, Decimal] = {}
    for t in statement.Trades or ():
        cat = str(getattr(t.assetCategory, "value", t.assetCategory) or "?").upper()
        if cat == "CASH" or t.quantity is None:
            continue
        expected[cat] = expected.get(cat, ZERO) + abs(Decimal(str(t.quantity)))
    for g in r.commissions:
        if g.asset_category == "CASH":
            continue
        assert g.quantity == expected.get(g.asset_category, ZERO), g.asset_category


def test_a_fractional_quantity_reaches_the_payload_as_a_number():
    """`quantity` is a Decimal now, and Decimal is not JSON-serialisable.

    The gate that keeps the internal exactness from leaking a string into the
    payload -- `json.dumps(default=str)` would turn 500.0013 into "500.0013"
    and the page's `num()` would render it as text. Same trap `_num` exists for.
    """
    data = costs_data(analyse(_stmt([_fill("STK", "-0.35", qty="1.79")])))
    qty = data["commissions"][0]["quantity"]
    assert isinstance(qty, float), f"{type(qty).__name__} reached the payload"
    assert qty == pytest.approx(1.79)
    json.dumps(data)  # raises on a stray Decimal


def test_the_report_shows_a_whole_quantity_without_false_precision():
    """A round lot reads "750", a fractional one keeps its digits.

    Formatting, not arithmetic -- but the reason the field was an int in the
    first place was to get this for free, so the replacement has to earn it
    back or the fix trades a wrong number for an unreadable one.
    """
    whole = format_report(analyse(_stmt([_fill("STK", "-1.00", qty="750")])))
    assert "750" in whole and "750.0000" not in whole
    frac = format_report(analyse(_stmt([_fill("STK", "-1.00", qty="1.79")])))
    assert "1.79" in frac


def test_credit_netting_holds_on_the_real_statement(statement):
    """Signed accumulation must equal the magnitude of the signed sum.

    The oracle applies the commission-currency rule rather than fxRateToBase
    alone. It is not what this test is about -- credit netting is -- but the two
    were entangled: the oracle recomputed the conversion, so it silently pinned
    `commission x fxRateToBase` as correct for every row. On the real statement
    that is wrong for exactly one, the EUR.SEK conversion IBKR bills in EUR,
    which made this test fail on a genuine fix by 1.576966 EUR.
    """
    signed = sum(
        _commission_to_base(
            t.ibCommission, t.fxRateToBase,
            str(getattr(t, "ibCommissionCurrency", None) or "") or None,
            str(getattr(t, "currency", None) or "") or None,
            "EUR",
        )
        for t in statement.Trades or ()
    )
    assert analyse(statement).total_commission_base == -signed


# --------------------------------------------------------------- journal scope
#
# `analyse` reads the raw statement, which covers the whole IBKR account, while
# ingest filters the database to options. Without an explicit scope the report
# presented account-wide commission under a journal scoped to options: 93% of
# the "commission" figure was stock the journal deliberately excludes.


def test_journal_scope_separates_attributable_from_account_level():
    r = analyse(_stmt([
        _fill("OPT", "-2.00", qty="3"),
        _fill("STK", "-25.00", qty="100"),
    ]))
    assert r.journal_asset == "OPT"
    assert r.journal_commission_base == Decimal("2.00")
    assert r.journal_friction_base == Decimal("2.00")
    assert r.other_commission_base == Decimal("25.00")
    # The account total is unchanged -- the split reapportions, never drops.
    assert r.total_commission_base == Decimal("27.00")


def test_the_two_blocks_sum_to_the_account_total(statement):
    """No cost may fall between the journal block and the account block."""
    r = analyse(statement)
    assert r.journal_friction_base + r.account_friction_base == r.total_friction_base


def test_journal_scope_is_configurable():
    """Rescoping moves costs between blocks without changing the total."""
    trades = [_fill("OPT", "-2.00", qty="3"), _fill("STK", "-25.00", qty="100")]
    stk = analyse(_stmt(trades), journal_asset="STK")
    assert stk.journal_commission_base == Decimal("25.00")
    assert stk.other_commission_base == Decimal("2.00")
    assert stk.total_friction_base == analyse(_stmt(trades)).total_friction_base


def test_journal_block_is_reported_even_with_no_matching_trades():
    """A statement with no option fills must say so, not print an empty block."""
    r = analyse(_stmt([_fill("STK", "-25.00", qty="100")]))
    assert r.journal_commissions == []
    assert r.journal_friction_base == ZERO
    assert f"no {r.journal_asset} trades" in format_report(r)


def test_json_exposes_both_scopes(statement):
    """A consumer must be able to read the journal figure, not just the account."""
    t = costs_data(analyse(statement))["totals"]
    for key in (
        "journal_commission", "journal_friction",
        "other_commission", "account_friction_base", "credit_fills",
    ):
        assert key in t, key
    assert t["journal_friction"]["base"] + t["account_friction_base"] == pytest.approx(
        t["friction_base"]
    )


def test_native_commission_is_offered_only_for_a_single_currency_scope():
    """Exact when one currency accounts for the whole figure, withheld the
    moment a second appears.

    A native amount cannot be summed across currencies -- USD, SEK and KRW
    commission share no number -- so offering one for a mixed scope would mean
    either a wrong total or a silently partial one. None is the display's
    signal to fall back to the base restatement, which is approximate but
    complete.
    """
    from optjournal.money import one_currency

    assert one_currency({"USD": -6.97}) == (-6.97, "USD")
    assert one_currency({"USD": -4.46, "SEK": -208.41}) == (None, None)
    assert one_currency({}) == (None, None)
    # A zero-commission currency is not a second currency: a scope of USD
    # option trades plus a free EUR conversion row is still a USD figure.
    assert one_currency({"USD": -6.97, "EUR": 0.0}) == (-6.97, "USD")
    # ...and a scope with no commission at all names no currency.
    assert one_currency({"EUR": 0.0}) == (None, None)


def test_native_commission_is_exact_where_the_restatement_was_not(tmp_path):
    """The regression this closes, end to end on demo data: the native figure
    must equal the sum IBKR billed, and must NOT equal the base sum restated at
    a snapshot rate -- the two differ precisely because the base figure was
    converted per trade at each trade's own date.
    """
    import sqlite3

    from optjournal import web
    from optjournal.db import connect, migrate
    from optjournal.demo import write_demo_statement
    from optjournal.ingest import ingest_file

    statement = write_demo_statement(tmp_path / "demo", tmp_path / "demo.db")
    conn = connect(tmp_path / "demo.db")
    migrate(conn)
    ingest_file(conn, statement)
    conn.close()

    st = web.build_state(db_path=tmp_path / "demo.db", archive_dir=statement.parent,
                         query_id=None)["stats"]
    if st["commissions"]["native"] is None:
        return  # demo scope is multi-currency; the gate is pinned above
    billed = sqlite3.connect(tmp_path / "demo.db").execute(
        "SELECT SUM(ib_commission) FROM trades WHERE asset_category='OPT'"
        " AND currency = ?", (st["commissions"]["ccy"],)
    ).fetchone()[0]
    # Native is a subset of billed (closed round trips only), never larger.
    assert abs(st["commissions"]["native"]) <= abs(billed) + 1e-9
    # And it is a genuinely different number from the base figure, which is
    # what makes the display distinction worth drawing.
    assert st["commissions"]["native"] != st["commissions"]["base"]


def test_cost_analysis_converts_commission_at_a_rate_that_applies_to_it():
    """The cost report recomputes from the statement rather than reading the
    stored column, so repairing the database could not reach it -- the same
    defect existed in two places and only one had been fixed.

    `fxRateToBase` is the INSTRUMENT's rate. IBKR bills the commission on an FX
    conversion in the BASE currency while the row's currency is the pair's quote,
    so converting it understated that cost by a factor of the rate.
    """
    from decimal import Decimal

    # The real shape: an EUR.SEK conversion, commission billed in EUR.
    assert _commission_to_base(
        Decimal("-1.73464"), Decimal("0.090897"), "EUR", "SEK", "EUR"
    ) == Decimal("-1.73464"), "a base-currency commission must not be converted"
    # Agreeing currencies keep the instrument's rate -- every other row.
    assert _commission_to_base(
        Decimal("-2"), Decimal("0.86892"), "USD", "USD", "EUR"
    ) == Decimal("-2") * Decimal("0.86892")
    # An absent commission currency is old data: fall through, do not raise.
    assert _commission_to_base(
        Decimal("-2"), Decimal("0.86892"), None, "USD", "EUR"
    ) == Decimal("-2") * Decimal("0.86892")
    # A third currency has no rate in the statement. Left unconverted rather
    # than dropped: omitting a charge from a cost report is the worse failure.
    assert _commission_to_base(
        Decimal("-1.5"), Decimal("0.090897"), "GBP", "SEK", "EUR"
    ) == Decimal("-1.5")
    assert _commission_to_base(None, Decimal("1"), "EUR", "EUR", "EUR") == ZERO


def test_the_cost_report_carries_native_commission_per_billing_currency():
    """analysis.py stays a leaf: it accumulates the breakdown and imports nothing
    to interpret it. The single-currency judgement lives once, in stats, and is
    applied by serialize -- the layer that already holds both.

    Magnitudes, matching commission_base's convention: the report presents cost
    as positive, and a breakdown that disagreed in sign with the total it
    decomposes would be worse than no breakdown at all.
    """
    r = analyse(_stmt([
        _fill("OPT", "-2.00", qty="3"),
        _fill("STK", "-25.00", qty="100"),
    ]))
    assert r.journal_asset == "OPT"
    nat = r.journal_native_by_ccy
    assert nat, "no per-currency breakdown produced"
    assert sum(nat.values()) == r.journal_commission_base, \
        "the breakdown does not reconcile with the total it decomposes"
    assert all(v > 0 for v in nat.values()), "cost must be presented positive"


def test_a_mixed_currency_journal_scope_serves_no_native_cost_figure():
    """The gate is what keeps an exact-looking figure from silently covering
    only part of a total. A journal scope spanning currencies has no single
    number that is both exact and complete, so it serves None and the display
    falls back to the base restatement.
    """
    from optjournal.serialize import _journal_commission

    one = analyse(_stmt([_fill("OPT", "-2.00", qty="3")]))
    charged = _journal_commission(one)
    assert charged.is_exact and charged.native == float(one.journal_commission_base)

    # Two billing currencies in the journal scope -> withheld. Built explicitly
    # rather than by extending _fill: that helper is shared by a dozen tests
    # that have nothing to say about currency, and its silence is what proves
    # the fallback path works in the case above.
    usd = _fill("OPT", "-2.00", qty="3")
    usd.ibCommissionCurrency = "USD"
    sek = _fill("OPT", "-3.00", qty="1")
    sek.ibCommissionCurrency = "SEK"
    mixed = analyse(_stmt([usd, sek]))
    assert len(mixed.journal_native_by_ccy) == 2, "the fake did not span currencies"
    withheld = _journal_commission(mixed)
    assert not withheld.is_exact and (withheld.native, withheld.currency) == (None, None)


def test_friction_has_no_as_charged_figure_because_part_of_it_is_estimated():
    """`total_friction` and `account_friction` deliberately have NO native
    counterpart, and that is a statement about the data rather than a gap.

    Both include the AutoFX markup, which is basis points applied to a converted
    notional -- IBKR never billed it as a line item in any currency. There is no
    figure "as charged" for a cost that was never charged explicitly, so offering
    one would invent precision instead of recovering it. Stated friction, which is
    commission plus taxes plus fees, is all real charges and does get one.
    """
    r = analyse(_stmt([_fill("OPT", "-2.00", qty="3")]))
    assert hasattr(r, "total_stated_friction_native_by_ccy")
    for absent in ("total_friction_native_by_ccy", "account_friction_native_by_ccy"):
        assert not hasattr(r, absent), (
            f"{absent} exists; friction mixes a real charge with an estimate and"
            " must not claim a billing currency"
        )
    # Stated friction reconciles with the total it decomposes.
    assert sum(r.total_stated_friction_native_by_ccy.values()) \
        == r.total_stated_friction_base


def test_the_account_level_gate_withholds_on_a_mixed_scope_but_is_applied():
    """"Withheld because the scope is mixed" and "never considered" look
    identical in a payload until a currency becomes uniform -- only one of them
    then starts producing a figure. The account-level ledgers deliberately span
    asset categories, so on a multi-currency account the gate almost always
    withholds; the point is that it is asked.
    """
    from optjournal.serialize import _money

    usd = _fill("STK", "-25.00", qty="100")
    usd.ibCommissionCurrency = "USD"
    mixed = analyse(_stmt([_fill("OPT", "-2.00", qty="3"), usd]))
    # Journal scope is single-currency, so it answers.
    assert _money(mixed.journal_commission_base, mixed.journal_native_by_ccy).is_exact
    # One non-journal currency: the account-level ledger answers too.
    other = _money(mixed.other_commission_base, mixed.other_native_by_ccy)
    assert (other.native, other.currency) == (25.0, "USD")

    sek = _fill("STK", "-9.00", qty="5")
    sek.ibCommissionCurrency = "SEK"
    spanning = analyse(_stmt([_fill("OPT", "-2.00", qty="3"), usd, sek]))
    withheld = _money(spanning.other_commission_base, spanning.other_native_by_ccy)
    assert (withheld.native, withheld.currency) == (None, None)


def _taxed(asset, commission="-1.00", taxes="-0.25", ccy="USD", billed=None,
           rate="1", qty="1"):
    """A fill that carries currencies, which `_fill` deliberately does not.

    Two currency fields, because IBKR sends two and they are independent:
    `ibCommissionCurrency` labels the commission ONLY, while a tax has no
    currency field of its own and so takes the instrument's. A row can be
    billed commission in EUR and tax in SEK -- that is the case the separate
    ledgers exist to represent, and it cannot be built without both fields.
    """
    return SimpleNamespace(
        assetCategory=SimpleNamespace(value=asset),
        symbol="TSLA",
        currency=ccy,
        ibCommissionCurrency=billed or ccy,
        proceeds=Decimal("-1000"),
        ibCommission=Decimal(commission),
        taxes=Decimal(taxes),
        fxRateToBase=Decimal(rate),
        quantity=Decimal(qty),
        notes=[],
    )


def test_journal_taxes_ledger_is_scoped_to_the_journal_asset():
    """Taxes on other asset categories must not reach the journal figure.

    The point of the ledger: `journal_taxes_base` was already scoped, but the
    as-charged breakdown existed only inside `journal_friction_native_by_ccy`,
    so journal taxes had a base figure and no way to reach the exact one.
    """
    r = analyse(_stmt([
        _taxed("OPT", taxes="-0.25", ccy="USD"),
        _taxed("STK", taxes="-9.99", ccy="SEK"),
    ]))
    assert r.journal_taxes_native_by_ccy == {"USD": Decimal("0.25")}
    assert r.other_taxes_native_by_ccy == {"SEK": Decimal("9.99")}


def test_journal_taxes_ledger_uses_the_report_sign_convention():
    """Charges arrive negative; the report presents cost as positive.

    A breakdown that did not flip would disagree in sign with the total it
    decomposes, which is worse than no breakdown.
    """
    r = analyse(_stmt([_taxed("OPT", taxes="-0.25", ccy="USD")]))
    assert r.journal_taxes_native_by_ccy == {"USD": Decimal("0.25")}
    assert r.journal_taxes_base > 0


def test_journal_taxes_across_currencies_produces_two_entries():
    """Two currencies must survive as two entries for the gate to withhold.

    Collapsing them here -- summing 0.25 USD and 3.00 SEK into 3.25 of nothing
    -- would hand the gate a single-currency-looking ledger and produce an
    exact figure covering part of a total.
    """
    r = analyse(_stmt([
        _taxed("OPT", taxes="-0.25", ccy="USD"),
        _taxed("OPT", taxes="-3.00", ccy="SEK", rate="0.09"),
    ]))
    assert r.journal_taxes_native_by_ccy == {
        "USD": Decimal("0.25"), "SEK": Decimal("3.00")}


def test_journal_friction_merges_commission_and_taxes_ledgers():
    """Friction composes the two named ledgers, and says so in one place.

    It used to re-walk the groups with an inlined tax loop, which meant
    "friction is commission plus taxes" was stated twice -- once in the base
    property and once, differently, in the ledger.
    """
    r = analyse(_stmt([_taxed("OPT", commission="-1.00", taxes="-0.25", ccy="USD")]))
    assert r.journal_native_by_ccy == {"USD": Decimal("1.00")}
    assert r.journal_taxes_native_by_ccy == {"USD": Decimal("0.25")}
    assert r.journal_friction_native_by_ccy == {"USD": Decimal("1.25")}
    assert r.journal_friction_base == r.journal_commission_base + r.journal_taxes_base


def test_friction_keeps_commission_and_taxes_apart_when_billed_differently():
    """Commission in EUR and tax in SEK on ONE fill must stay two entries.

    Not hypothetical: this account holds an EUR.SEK conversion billed
    commission in EUR while the instrument is SEK. `ibCommissionCurrency`
    labels the commission only, so a merged single-ledger implementation would
    have attributed the tax to the commission's currency and produced an exact
    figure for a scope that has none.
    """
    r = analyse(_stmt([
        _taxed("OPT", commission="-1.00", taxes="-3.00", ccy="SEK", billed="EUR",
               rate="0.09"),
    ]))
    assert r.journal_native_by_ccy == {"EUR": Decimal("1.00")}
    assert r.journal_taxes_native_by_ccy == {"SEK": Decimal("3.00")}
    assert r.journal_friction_native_by_ccy == {
        "EUR": Decimal("1.00"), "SEK": Decimal("3.00")}


#: analysis.py's import rule is asserted in `test_layering.py`, which walks the
#: real graph: `IMPORTS_LEAVES_ONLY` there says it may hold value types and
#: nothing that reads a database. A hand-rolled copy of that check lived here and
#: said something slightly stronger -- import NOTHING internal -- which stopped
#: being true when the note-code rule became the `notes` leaf. Two statements of
#: one rule, disagreeing, is how a rule gets weakened at the wrong site: the
#: honest edit is to the rule, in the one place it is written.
