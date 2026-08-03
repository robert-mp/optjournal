"""Access to statement sections py_ibkr does not model.

py_ibkr's FlexStatement models Trades, CashTransactions and CashReport.
Our Activity query also emits AccountInformation, OpenPositions,
SecuritiesInfo, CorporateActions and Transfers. Those are returned here as
untyped attribute dicts rather than being silently discarded.

Untyped is deliberate: promoting a section to a Pydantic model is only
worth doing once we consume its fields, and `MODELLED_SECTIONS` plus the
drift test in tests/test_flex.py keep the boundary honest.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

__all__ = ["MODELLED_SECTIONS", "raw_sections", "section_tags"]

#: Statement child elements py_ibkr turns into typed models.
MODELLED_SECTIONS = frozenset({"Trades", "CashTransactions", "CashReport"})


def _statement(path: Path) -> ET.Element:
    root = ET.parse(str(path)).getroot()
    stmt = root.find(".//FlexStatement")
    if stmt is None:
        raise ValueError(f"{path}: no FlexStatement element")
    return stmt


def section_tags(path: Path) -> list[str]:
    """Return every section tag present in the statement, in document order."""
    return [child.tag for child in _statement(path)]


def raw_sections(path: Path) -> dict[str, list[dict[str, str]]]:
    """Return the unmodelled sections as lists of raw attribute dicts.

    AccountInformation carries its data on the element itself rather than on
    child rows, so it is normalised to a single-item list for consistency.
    """
    out: dict[str, list[dict[str, str]]] = {}
    for child in _statement(path):
        if child.tag in MODELLED_SECTIONS:
            continue
        if child.tag == "AccountInformation":
            out[child.tag] = [dict(child.attrib)] if child.attrib else []
        else:
            out[child.tag] = [dict(row.attrib) for row in child]
    return out
