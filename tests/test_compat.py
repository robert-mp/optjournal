"""Tests for the py_ibkr Code enum fallback."""

from __future__ import annotations

import pytest
from py_ibkr.flex.enums import Code

from optjournal.compat import install_code_fallback, unknown_codes


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
