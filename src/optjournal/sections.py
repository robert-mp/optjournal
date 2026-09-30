"""Access to statement sections py_ibkr does not model.

py_ibkr's FlexStatement models Trades, CashTransactions and CashReport.
Our Activity query also emits AccountInformation, OpenPositions,
SecuritiesInfo, CorporateActions and Transfers. Those are returned here as
untyped attribute dicts rather than being silently discarded.

Untyped is deliberate: promoting a section to a Pydantic model is only
worth doing once we consume its fields, and `MODELLED_SECTIONS` plus the
drift test in tests/test_flex.py keep the boundary honest.

EVERY statement block is read. A Flex file holds one FlexStatement per account,
and each row carries its own `accountId`, so the rows are concatenated in
document order. Reading the first block only kept one account's positions,
contracts and NAV from a file that held two.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

__all__ = ["MODELLED_SECTIONS", "raw_sections", "section_tags"]

#: Statement child elements py_ibkr turns into typed models.
MODELLED_SECTIONS = frozenset({"Trades", "CashTransactions", "CashReport"})


def _statements(path: Path) -> list[ET.Element]:
    root = ET.parse(str(path)).getroot()
    found = root.findall(".//FlexStatement")
    if not found:
        raise ValueError(f"{path}: no FlexStatement element")
    return found


def section_tags(path: Path) -> list[str]:
    """Every section tag present in the file's statements, in document order."""
    tags: list[str] = []
    for stmt in _statements(path):
        tags += [child.tag for child in stmt if child.tag not in tags]
    return tags


def raw_sections(path: Path) -> dict[str, list[dict[str, str]]]:
    """Return the unmodelled sections as lists of raw attribute dicts.

    AccountInformation carries its data on the element itself rather than on
    child rows, so each statement contributes one item to its list.
    """
    out: dict[str, list[dict[str, str]]] = {}
    for stmt in _statements(path):
        for child in stmt:
            if child.tag in MODELLED_SECTIONS:
                continue
            rows = out.setdefault(child.tag, [])
            if child.tag == "AccountInformation":
                rows += [dict(child.attrib)] if child.attrib else []
            else:
                rows += [dict(row.attrib) for row in child]
    return out
