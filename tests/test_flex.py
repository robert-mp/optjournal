"""Tests run against real archived statements in raw/.

There are no synthetic fixtures on purpose. The failure mode we care about
is py_ibkr silently discarding data IBKR actually sends, and only genuine
statements exercise that.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from py_ibkr import Trade

from optjournal.flex import load
from optjournal.sections import MODELLED_SECTIONS, raw_sections, section_tags

RAW_DIR = Path(__file__).resolve().parent.parent / "raw"

# `requestID` is an artefact of the Flex request itself, not trade data, and
# py_ibkr deliberately omits it. Anything else appearing here is real drift.
KNOWN_UNMODELLED_TRADE_ATTRS = {"requestID"}

# Sections we know py_ibkr does not model and that we read via the shim.
KNOWN_UNMODELLED_SECTIONS = {
    "AccountInformation",
    "OpenPositions",
    "SecuritiesInfo",
    "CorporateActions",
    "Transfers",
}


def statements() -> list[Path]:
    return sorted(RAW_DIR.glob("activity-*.xml"))


@pytest.fixture(params=statements(), ids=lambda p: p.name)
def statement(request) -> Path:
    return request.param


def test_raw_dir_is_populated():
    assert statements(), (
        f"no archived statements in {RAW_DIR}; run `uv run optjournal fetch <id>`"
    )


def test_parses_without_error(statement: Path):
    resp = load(statement)
    assert resp.FlexStatements, "parsed response contains no statements"


def test_statement_metadata_present(statement: Path):
    for stmt in load(statement).FlexStatements:
        assert stmt.accountId
        assert stmt.fromDate and stmt.toDate
        assert stmt.fromDate <= stmt.toDate


def test_trade_fields_we_depend_on_are_modelled():
    """Fields the grouping and P&L engine will require."""
    required = {
        "ibOrderID",       # deterministic leg grouping
        "ibExecID",        # idempotent upsert key
        "openCloseIndicator",
        "notes",           # assignment / exercise / expiry codes
        "assetCategory",
        "buySell",
        "quantity",
        "tradePrice",
        "putCall",
        "strike",
        "expiry",
        "multiplier",
        "underlyingSymbol",
        "conid",
        "fxRateToBase",    # EUR-base account: non-optional here
        "ibCommission",
        "fifoPnlRealized",
        "levelOfDetail",
    }
    missing = required - set(Trade.model_fields)
    assert not missing, f"py_ibkr Trade is missing required fields: {sorted(missing)}"


def test_execution_level_detail(statement: Path):
    """The query must stay at execution granularity, not aggregated."""
    for stmt in load(statement).FlexStatements:
        levels = {t.levelOfDetail for t in (stmt.Trades or [])}
        assert levels <= {"EXECUTION"}, (
            f"unexpected levelOfDetail {levels}; the Flex query template "
            f"may have been changed away from execution granularity"
        )


def test_field_drift(statement: Path):
    """Fail if IBKR sends Trade attributes py_ibkr would silently drop.

    py_ibkr's models use extra='ignore', so unknown attributes vanish with
    no error. This turns that from a silent hazard into a failing test.
    """
    modelled = {f.lower() for f in Trade.model_fields}
    aliases = {
        v.alias.lower() for v in Trade.model_fields.values() if v.alias
    }
    known = modelled | aliases | {a.lower() for a in KNOWN_UNMODELLED_TRADE_ATTRS}

    seen: set[str] = set()
    for el in ET.parse(str(statement)).getroot().iter("Trade"):
        seen |= set(el.attrib)

    dropped = sorted(a for a in seen if a.lower() not in known)
    assert not dropped, (
        f"IBKR sent Trade attributes py_ibkr will discard: {dropped}. "
        f"Add them to the model or to KNOWN_UNMODELLED_TRADE_ATTRS."
    )


def test_section_drift(statement: Path):
    """Fail if the statement gains a section we neither model nor shim."""
    known = MODELLED_SECTIONS | KNOWN_UNMODELLED_SECTIONS
    unexpected = [t for t in section_tags(statement) if t not in known]
    assert not unexpected, f"unhandled statement sections: {unexpected}"


def test_shim_exposes_unmodelled_sections(statement: Path):
    sections = raw_sections(statement)
    assert not (set(sections) & MODELLED_SECTIONS), (
        "shim must not duplicate sections py_ibkr already models"
    )
    for tag, rows in sections.items():
        for row in rows:
            assert row, f"{tag}: empty attribute dict"
