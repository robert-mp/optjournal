"""Compatibility shims for py_ibkr 0.1.7.

py_ibkr models IBKR's vocabularies as closed enums: trade codes (`Code`), order
types, trade types, cash actions, asset classes and a few more. Any value IBKR
emits that an enum does not declare raises and aborts the whole parse: `Code`
through py_ibkr's own `Code(value)` call, every other enum through pydantic's
validation of the model field. Observed in practice: IBKR sent the trade code
`IPO`, which py_ibkr 0.1.7 does not know, and an order type such as `LIT` or
`MOO`, or a new cash transaction type, fails the same way.

That is the wrong failure mode for us. IBKR's lists grow, we cannot control
them, and one unrecognised value on one row should not make a year of
statements unreadable. `install_code_fallback()` registers an Enum `_missing_`
hook on every enum py_ibkr declares. It mints a pseudo-member for an unknown
value and records it, so parsing continues and the value is preserved rather
than guessed at or dropped. Pydantic consults `_missing_` when it validates an
enum field, so the one hook covers both paths.

The same call also puts IBKR's error CODE back into every Flex error py_ibkr
raises. py_ibkr maps six codes to classes with message templates that drop the
number, and it files 1009 ("the server is under heavy load") beside 1012 ("token
has expired") under one class and one template. Only the code tells a busy
server from a dead token, and `flex` needs that to send the reader to the right
remedy.

This is a shim, not a fix. The real fix is upstream; see README.
"""

from __future__ import annotations

import enum
import logging
from typing import Any

from py_ibkr.flex import client, enums
from py_ibkr.flex.enums import Code

__all__ = ["install_code_fallback", "unknown_codes", "unknown_values"]

log = logging.getLogger(__name__)

#: Trade codes encountered at runtime that py_ibkr does not declare. Printed by
#: the CLI after a run.
unknown_codes: set[str] = set()

#: Values of py_ibkr's OTHER enums encountered at runtime and not declared, as
#: "EnumName=value" (for example "OrderType=LIT").
unknown_values: set[str] = set()

_INSTALLED = False


def _py_ibkr_enums() -> list[type[enum.Enum]]:
    """Every enum class py_ibkr's Flex models declare."""
    return [
        obj for obj in vars(enums).values()
        if isinstance(obj, type) and issubclass(obj, enum.Enum)
        and obj.__module__ == enums.__name__
    ]


def _missing_(cls: type[enum.Enum], value: Any) -> enum.Enum | None:
    if not isinstance(value, str) or not value:
        return None

    if cls is Code:
        unknown_codes.add(value)
    else:
        unknown_values.add(f"{cls.__name__}={value}")
    log.debug(
        "IBKR value %r is not declared by py_ibkr's %s; accepting it as an "
        "ad-hoc member", value, cls.__name__,
    )

    # Every py_ibkr enum mixes in str, so the member must be constructed via
    # str.__new__; object.__new__ raises "not safe" for mixin enums.
    member: Any
    if issubclass(cls, str):  # noqa: SIM108 - the else carries a coverage pragma a ternary cannot
        member = str.__new__(cls, value)
    else:  # pragma: no cover - defensive, every py_ibkr enum is str-based today
        member = object.__new__(cls)
    member._name_ = value.upper().replace(" ", "_")
    member._value_ = value
    # Register so repeat occurrences resolve to the same object and identity
    # comparisons behave like a declared member.
    cls._value2member_map_[value] = member
    cls._member_map_.setdefault(member._name_, member)
    return member


def _keep_error_codes() -> None:
    """Prefix each of py_ibkr's error templates with the code it stands for.

    The prefix is the one py_ibkr already writes for a code it does NOT map,
    `Flex API Error {code}: `, so `flex._FLEX_CODE` reads every error the same
    way. The class is unchanged, so py_ibkr's own retry of 1003 and 1019 is too.
    """
    for code, (cls, template) in list(client._ERROR_EXCEPTIONS.items()):
        prefix = f"Flex API Error {code}: "
        if not template.startswith(prefix):
            client._ERROR_EXCEPTIONS[code] = (cls, prefix + template)


def install_code_fallback() -> None:
    """Install both py_ibkr shims. Idempotent.

    Every py_ibkr enum tolerates values it does not declare, and every Flex
    error py_ibkr raises carries IBKR's error code in its message.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    for cls in _py_ibkr_enums():
        cls._missing_ = classmethod(_missing_)  # type: ignore[assignment,method-assign]
    _keep_error_codes()
    _INSTALLED = True
