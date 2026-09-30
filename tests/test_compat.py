"""Tests for the py_ibkr Code enum fallback."""

from __future__ import annotations

import pytest
from conftest import STATEMENTS, connect_migrated
from py_ibkr.flex.enums import Code

from optjournal.compat import install_code_fallback, unknown_codes, unknown_values
from optjournal.ingest import ingest_file


@pytest.fixture(autouse=True)
def _shim():
    install_code_fallback()  # idempotent; optjournal/__init__ already ran it


def test_declared_codes_still_resolve_normally():
    assert Code("P") is Code.PARTIAL
    assert Code("AFx") is Code.AUTOFX


def test_unknown_code_does_not_raise():
    member = Code("ZZQ-not-a-real-code")
    assert member.value == "ZZQ-not-a-real-code"
    assert "ZZQ-not-a-real-code" in unknown_codes


def test_unknown_code_is_stable_across_lookups():
    """Repeat lookups must return the same object, not a fresh member."""
    assert Code("ZZQ-stable") is Code("ZZQ-stable")


def test_unknown_code_keeps_str_behaviour():
    """Code mixes in str, so ad-hoc members must remain usable as strings."""
    member = Code("ZZQ-strlike")
    assert isinstance(member, str)
    assert member == "ZZQ-strlike"


def test_empty_value_still_rejected():
    """Empty and non-string values are real errors, not unknown codes."""
    with pytest.raises(ValueError):
        Code("")
    with pytest.raises(ValueError):
        Code(None)


# --- every closed enum the parse touches, not only Code (M5) ------------------


@pytest.mark.parametrize(("enum_name", "value"), [
    ("OrderType", "LIT"),
    ("OrderType", "MOO"),
    ("TradeType", "ZZQTrade"),
    ("CashAction", "Broker Fees"),
    ("AssetClass", "ZZQ"),
    ("BuySell", "ZZQ"),
    ("PutCall", "ZZQ"),
])
def test_an_undeclared_value_of_any_py_ibkr_enum_is_accepted(enum_name, value):
    """One `orderType="LIT"` used to make the whole statement unreadable."""
    from py_ibkr.flex import enums

    cls = getattr(enums, enum_name)
    member = cls(value)
    assert member.value == value
    assert cls(value) is member, "a repeat lookup minted a second member"
    assert f"{enum_name}={value}" in unknown_values


def test_declared_values_of_the_other_enums_still_resolve_normally():
    from py_ibkr.flex.enums import CashAction, OrderType

    assert OrderType("LMT") is OrderType.LIMIT
    assert CashAction("Other Fees") is CashAction.FEES


@pytest.mark.skipif(not STATEMENTS, reason="needs an archived statement")
def test_a_statement_with_unfamiliar_enum_values_still_ingests(tmp_path):
    """End to end: the values IBKR sent are stored, not guessed at or dropped."""
    text = STATEMENTS[0].read_text(encoding="utf-8")
    for old, new in (('orderType="LMT"', 'orderType="LIT"'),
                     ('transactionType="ExchTrade"', 'transactionType="ZZQTrade"'),
                     ('type="Other Fees"', 'type="Broker Fees"')):
        assert old in text, f"fixture changed: {old}"
        text = text.replace(old, new, 1)
    path = tmp_path / "activity-unfamiliar.xml"
    path.write_text(text, encoding="utf-8")

    conn = connect_migrated(tmp_path / "j.db")
    result = ingest_file(conn, path)
    assert result.trades_inserted > 0
    kinds = {r[0] for r in conn.execute("SELECT type FROM cash_transactions")}
    assert "Broker Fees" in kinds
