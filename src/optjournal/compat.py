"""Compatibility shims for py_ibkr 0.1.7.

py_ibkr parses the Trade `notes` attribute into its `Code` enum with a plain
`Code(value)` call, so any code IBKR emits that the enum does not declare
raises ValueError and aborts the whole parse. Observed in practice: IBKR sent
`IPO`, which py_ibkr 0.1.7 does not know.

That is the wrong failure mode for us. IBKR's code list grows, we cannot
control it, and one unrecognised code on one fill should not make a year of
statements unreadable. `install_code_fallback()` registers an Enum `_missing_`
hook that mints a pseudo-member for unknown values and warns, so parsing
continues and the unknown code is preserved rather than guessed at or dropped.

This is a shim, not a fix. The real fix is upstream; see README.
"""

from __future__ import annotations

import logging
from typing import Any

from py_ibkr.flex.enums import Code

__all__ = ["install_code_fallback", "unknown_codes"]

log = logging.getLogger(__name__)

#: Codes encountered at runtime that py_ibkr does not declare.
unknown_codes: set[str] = set()

_INSTALLED = False


def install_code_fallback() -> None:
    """Make `Code` tolerate values it does not declare. Idempotent."""
    global _INSTALLED
    if _INSTALLED:
        return

    def _missing_(cls: type[Code], value: Any) -> Code | None:
        if not isinstance(value, str) or not value:
            return None

        unknown_codes.add(value)
        log.debug(
            "IBKR trade code %r is not declared by py_ibkr; "
            "accepting it as an ad-hoc member (surfaced by the CLI)",
            value,
        )

        # Code mixes in str, so the member must be constructed via
        # str.__new__; object.__new__ raises "not safe" for mixin enums.
        if issubclass(cls, str):  # noqa: SIM108 - the else carries a coverage pragma a ternary cannot
            member = str.__new__(cls, value)
        else:  # pragma: no cover - defensive, Code is str-based today
            member = object.__new__(cls)
        member._name_ = value.upper().replace(" ", "_")
        member._value_ = value
        # Register so repeat occurrences resolve to the same object and
        # identity comparisons behave like a declared member.
        cls._value2member_map_[value] = member
        cls._member_map_.setdefault(member._name_, member)
        return member

    Code._missing_ = classmethod(_missing_)  # type: ignore[assignment]
    _INSTALLED = True
