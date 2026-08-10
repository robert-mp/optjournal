"""IBKR trade note codes, and the one rule for reading them.

A fill's note field carries a set of codes: `Ep` (expired position), `A`
(assignment), `AFx` (auto-conversion), `P` (partial execution), `SL` (specific
lot matching). Two things make reading them a rule rather than a one-liner.

**They arrive in two shapes.** py_ibkr parses the XML attribute into a list of
`Code` enum members, so the statement path sees `[<Code.AUTOFX>, <Code.P>]`.
`sources.py` joins those on `;` for storage, so the database path sees the
string `"AFx;P"`. Both are the same fact and neither is more canonical, so
`split_notes` accepts either and answers in one vocabulary.

**They must be matched as whole tokens.** `"AFx" in notes` is true for the
string `"AFx;P"` by luck and true for `"A"` -- assignment -- by accident, and
those are different events. Splitting first makes an exact comparison possible;
substring matching cannot be made correct by being careful.

A leaf: the two accounting layers that read codes sit on opposite sides of the
import graph -- `analysis.py` reads a statement, `history.py` reads the database
-- and neither may depend on the other, so the shared rule lives in a module
both can hold. It imports nothing itself, so holding it acquires no direction.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

__all__ = ["AUTOFX", "has_code", "split_notes"]

#: Auto-conversion. IBKR prices these as a markup embedded in the exchange rate
#: and reports zero commission on them, which is why the flag is load-bearing:
#: it is the only thing distinguishing a conversion whose cost must be estimated
#: from a manual one that paid a real commission. See `analysis.AUTOFX_MARKUP_BPS`.
AUTOFX = "AFx"

#: The separator `sources.py` joins stored codes with.
_SEP = ";"


def split_notes(notes: Any) -> tuple[str, ...]:
    """The note codes in `notes`, as exact tokens, from either shape.

    Accepts the stored string (`"AFx;P"`), py_ibkr's list of enum members, a
    bare member, or None. Enum members are read through `.value` so a
    pseudo-member minted by `compat.install_code_fallback` for a code py_ibkr
    does not declare still yields its wire value rather than its repr.
    """
    if not notes:
        return ()
    if isinstance(notes, str):
        parts: Iterable[Any] = notes.split(_SEP)
    elif isinstance(notes, (list, tuple)):
        parts = notes
    else:
        parts = [notes]
    return tuple(
        token
        for token in (str(getattr(p, "value", p)).strip() for p in parts)
        if token
    )


def has_code(notes: Any, code: str) -> bool:
    """Whether `notes` carries `code`, matched as a whole token."""
    return code in split_notes(notes)
