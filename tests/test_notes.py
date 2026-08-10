"""The note-code rule, tested against both shapes a note field arrives in.

This module exists because the rule was written twice: `history.py` split on `;`
and compared tokens, `analysis.py` compared the whole field to a constant. Both
were right about the input they happened to see and only one was right in
general, which is the failure a shared leaf removes.
"""

from __future__ import annotations

import pytest

from optjournal.notes import AUTOFX, has_code, split_notes


class _Code:
    """Stands in for py_ibkr's `Code` enum, which exposes a wire `value`.

    `compat.install_code_fallback` mints pseudo-members for codes py_ibkr does
    not declare, so the reader must go through `.value` rather than `str()` --
    which on a real member yields `Code.AUTOFX`, not `AFx`.
    """

    def __init__(self, value: str) -> None:
        self.value = value


# --- shapes ------------------------------------------------------------------
#
# The stored string and py_ibkr's list are the same fact, so every case below is
# stated once per shape rather than once per module that reads one.


@pytest.mark.parametrize(
    ("notes", "expected"),
    [
        ("AFx", ("AFx",)),
        ("AFx;P", ("AFx", "P")),
        ("P;AFx", ("P", "AFx")),
        (" AFx ; P ", ("AFx", "P")),
        ("AFx;;P", ("AFx", "P")),
        (";", ()),
        ("", ()),
        (None, ()),
    ],
)
def test_split_reads_the_stored_string(notes, expected):
    """The shape `sources.py` writes: `";".join(codes)`, whitespace and all."""
    assert split_notes(notes) == expected


@pytest.mark.parametrize(
    ("notes", "expected"),
    [
        ([_Code("AFx")], ("AFx",)),
        ([_Code("AFx"), _Code("P")], ("AFx", "P")),
        (["AFx", "P"], ("AFx", "P")),
        ([_Code("AFx"), "P"], ("AFx", "P")),
        (_Code("AFx"), ("AFx",)),
        ((), ()),
        ([], ()),
    ],
)
def test_split_reads_py_ibkrs_list(notes, expected):
    """The shape py_ibkr hands the statement path, including a bare member."""
    assert split_notes(notes) == expected


def test_both_shapes_of_one_fact_agree():
    """The property that makes one rule enough for two readers.

    Stated as an equality rather than as two assertions because it is the whole
    claim: a statement read from XML and the same statement read back out of
    SQLite must answer identically, or the two paths disagree about a cost.
    """
    assert split_notes("AFx;P") == split_notes([_Code("AFx"), _Code("P")])


# --- token matching ----------------------------------------------------------


def test_has_code_matches_a_whole_token():
    assert has_code("AFx;P", AUTOFX)
    assert has_code([_Code("P"), _Code("AFx")], AUTOFX)


@pytest.mark.parametrize("notes", ["A", "A;P", "AFxx", "afx", ""])
def test_has_code_is_not_a_substring_search(notes):
    """`A` is assignment and `AFx` is auto-conversion.

    Every value here is one a substring search gets wrong: `"AFx" in "A"` is
    False but `"A" in "AFx"` is True, so a reader looking for assignment finds
    it in every auto-conversion. Case is not normalised either -- IBKR's codes
    are case-significant (`P` partial, `p` is not a code), so `afx` is not a
    misspelling to be forgiven but a token that does not exist.
    """
    assert not has_code(notes, AUTOFX)


def test_the_autofx_constant_is_ibkrs_wire_value():
    """Pinned because it is compared against stored data, not just parsed data.

    A rename here silently stops matching every row already in the database.
    """
    assert AUTOFX == "AFx"
