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

__all__ = ["MODELLED_SECTIONS", "raw_sections", "section_tags", "stated_base_currency",
           "statement_blocks"]

#: Statement child elements py_ibkr turns into typed models.
MODELLED_SECTIONS = frozenset({"Trades", "CashTransactions", "CashReport"})


def statement_blocks(path: Path) -> list[ET.Element]:
    """Every FlexStatement element in the file. Raises ValueError when none."""
    root = ET.parse(str(path)).getroot()
    found = root.findall(".//FlexStatement")
    if not found:
        raise ValueError(f"{path}: no FlexStatement element")
    return found


def stated_base_currency(statements: list[ET.Element]) -> str | None:
    """The account's base currency as the statement blocks state it, or None.

    AccountInformation is where IBKR states it. EquitySummaryInBase rows carry
    the same code (every real statement agrees), so they answer when a query
    leaves AccountInformation out. One rule for the ingest's reader and for the
    fetch's check before a body is archived, so the two cannot disagree.
    """
    for name in ("AccountInformation", "EquitySummaryInBase"):
        for stmt in statements:
            for child in (c for c in stmt if c.tag == name):
                rows = [child] if name == "AccountInformation" else list(child)
                for row in rows:
                    code = (row.get("currency") or "").strip()
                    if code:
                        return code
    return None


def section_tags(path: Path) -> list[str]:
    """Every section tag present in the file's statements, in document order."""
    tags: list[str] = []
    for stmt in statement_blocks(path):
        tags += [child.tag for child in stmt if child.tag not in tags]
    return tags


def raw_sections(path: Path) -> dict[str, list[dict[str, str]]]:
    """Return the unmodelled sections as lists of raw attribute dicts.

    AccountInformation carries its data on the element itself rather than on
    child rows, so each statement contributes one item to its list.
    """
    out: dict[str, list[dict[str, str]]] = {}
    for stmt in statement_blocks(path):
        for child in stmt:
            if child.tag in MODELLED_SECTIONS:
                continue
            rows = out.setdefault(child.tag, [])
            if child.tag == "AccountInformation":
                rows += [dict(child.attrib)] if child.attrib else []
            else:
                rows += [dict(row.attrib) for row in child]
    return out
