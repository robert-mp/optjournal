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
