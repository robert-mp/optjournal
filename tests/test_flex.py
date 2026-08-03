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

from optjournal import flex
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
    # Anticipated, not yet emitted. The Flex query does not request Equity
    # Summary today, which is why `Gain % of Net Liq` is a hardcoded em-dash --
    # net liquidation value is the missing denominator for every return metric.
    # Declared here in advance so enabling that section is a one-click change
    # in IBKR rather than a one-click change plus a red build: the parser
    # tolerates the section (verified by injecting it into a real statement) and
    # `raw_sections` surfaces it generically, so nothing else needs to change to
    # ingest it. Persisting it still requires a table and an ingest branch.
    "EquitySummaryInBase",
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


# --- retry budget -------------------------------------------------------------
#
# The polling ceiling is not a free parameter: callers size their timeouts from
# it. A cron script had FETCH_TIMEOUT_S=240 against a real worst case of 2,100s,
# on a stale comment claiming 84s, so a routine slow statement generation became
# a raw traceback and a spent request with no cooldown recorded. These pin the
# arithmetic and, more importantly, fail if MAX_RETRIES grows past what a daily
# cron can wait for.


def test_poll_worst_case_matches_backoff_arithmetic():
    """Recomputed independently of the module's own expression."""
    per_stage = sum(
        min(flex.RETRY_INTERVAL * (2**i), flex.MAX_RETRY_INTERVAL)
        for i in range(flex.MAX_RETRIES)
    )
    assert flex.POLL_WORST_CASE_S == 2 * per_stage, (
        "worst case must cover both py_ibkr poll stages (SendRequest and "
        "GetStatement), each of which gets the full retry budget"
    )


def test_poll_worst_case_is_hand_computable():
    """MAX_RETRIES=4 -> [30, 60, 120, 120] = 330s/stage -> 660s."""
    assert flex.MAX_RETRIES == 4
    assert flex.POLL_WORST_CASE_S == 660


def test_retry_budget_stays_within_a_daily_cron_window():
    """The guard that makes the timeout fix durable.

    `~/.meshclaw/crons/optjournal_sync.py` sets a subprocess timeout above
    POLL_WORST_CASE_S, and its cron registration sets a timeout above that.
    Raising MAX_RETRIES silently invalidates both. Fail here instead, where the
    message can say so, rather than at 07:00 in a sandboxed subprocess.
    """
    assert flex.POLL_WORST_CASE_S <= 720, (
        f"POLL_WORST_CASE_S is {flex.POLL_WORST_CASE_S}s. Raise "
        f"FETCH_TIMEOUT_S in optjournal_sync.py above it, and the cron's own "
        f"timeout above that, or lower MAX_RETRIES."
    )


def test_backoff_is_capped_not_unbounded():
    waits = [
        min(flex.RETRY_INTERVAL * (2**i), flex.MAX_RETRY_INTERVAL)
        for i in range(flex.MAX_RETRIES)
    ]
    assert max(waits) == flex.MAX_RETRY_INTERVAL
    assert waits == sorted(waits), "backoff must be monotonically non-decreasing"
