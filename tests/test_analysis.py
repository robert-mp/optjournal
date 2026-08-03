"""Tests for cost analysis, run against real archived statements."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from types import SimpleNamespace

from optjournal.analysis import (
    AUTOFX_MARKUP_BPS,
    WithholdingLine,
    analyse,
    categorise_fee,
    format_report,
)
from optjournal.flex import load
from optjournal.render import costs_data

RAW_DIR = Path(__file__).resolve().parent.parent / "raw"
ZERO = Decimal("0")


@pytest.fixture(params=sorted(RAW_DIR.glob("activity-*.xml")), ids=lambda p: p.name)
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
    """IBKR reports dividends net, so the rate is withheld/(net+withheld)."""
    line = WithholdingLine(
        symbol="ACME",
        currency="USD",
        gross_base=Decimal("85"),
        withheld_base=Decimal("15"),
    )
    assert line.effective_rate == Decimal("15")


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
    return SimpleNamespace(
        assetCategory=SimpleNamespace(value="CASH"),
        symbol=symbol,
        proceeds=Decimal(proceeds),
        ibCommission=Decimal(commission),
        taxes=Decimal("0"),
        fxRateToBase=Decimal(rate),
        quantity=None,
        notes=list(notes),
    )


def _stmt(trades):
    return SimpleNamespace(
        fromDate="20250801", toDate="20260731",
        Trades=trades, CashTransactions=[],
    )


def test_autofx_markup_is_three_bps():
    """Published rate for IBKR Ireland: 0.03% == 3 bps."""
    assert AUTOFX_MARKUP_BPS == Decimal("3")


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
    data = costs_data(_real_report())
    for key in (
        "commission_base", "fees_base", "taxes_base", "autofx_notional_base",
        "autofx_spread_base", "stated_friction_base", "friction_base",
        "fx_notional_base", "fx_commission_base",
    ):
        assert key in data["totals"], f"totals.{key} missing"


def test_costs_data_totals_are_self_consistent():
    report = _real_report()
    t = costs_data(report)["totals"]
    assert t["friction_base"] == pytest.approx(
        t["stated_friction_base"] + t["autofx_spread_base"]
    )
    assert t["stated_friction_base"] == pytest.approx(
        t["commission_base"] + t["fees_base"] + t["taxes_base"]
    )
    assert t["friction_base"] == pytest.approx(float(report.total_friction_base))


def test_costs_data_money_is_numeric_not_string():
    """Decimal + json.dumps(default=str) silently emits money as strings."""
    import json
    data = costs_data(_real_report())
    assert isinstance(data["totals"]["friction_base"], float)
    assert isinstance(data["fx"][0]["notional_base"], float)
    round_tripped = json.loads(json.dumps(data))
    assert isinstance(round_tripped["totals"]["friction_base"], (int, float))


def test_costs_data_includes_per_pair_properties():
    """autofx_spread_base and commission_bps are properties, so asdict drops them."""
    data = costs_data(_real_report())
    assert data["fx"], "expected at least one FX pair"
    for pair in data["fx"]:
        assert "autofx_spread_base" in pair
        assert "commission_bps" in pair


def test_costs_data_autofx_spread_sums_to_total():
    data = costs_data(_real_report())
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


def test_a_net_credit_category_reports_negative_cost():
    """A category that was net credited is a negative cost, not a positive one."""
    r = analyse(_stmt([_fill("STK", "0.50"), _fill("STK", "-0.20")]))
    assert r.commissions[0].commission_base == Decimal("-0.30")
    assert r.commissions[0].credit_fills == 1


def test_taxes_use_the_same_sign_convention():
    r = analyse(_stmt([_fill("OPT", "-1.00", taxes="-0.25")]))
    assert r.commissions[0].taxes_base == Decimal("0.25")


def test_credit_netting_holds_on_the_real_statement(statement):
    """Signed accumulation must equal the magnitude of the signed sum."""
    signed = sum(
        (t.ibCommission or ZERO) * (t.fxRateToBase or Decimal("1"))
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
        "journal_commission_base", "journal_friction_base",
        "other_commission_base", "account_friction_base", "credit_fills",
    ):
        assert key in t, key
    assert t["journal_friction_base"] + t["account_friction_base"] == pytest.approx(
        t["friction_base"]
    )
