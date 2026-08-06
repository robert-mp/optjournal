"""The terminal reports, over the payloads they actually receive.

These exist because `render.py` had no test at all, and it shipped broken:
the Money conversion replaced an order's `proceeds`/`proceeds_base` pair and
an episode's `realized_pnl_base`/`commission_base` with nested `Money`
objects, `render_orders` and `render_history` kept reading the removed flat
keys, and `optjournal orders` and `optjournal history` both died on
`float(dict)`. Every other layer was held to the payload by a test -- the page
by test_web's contract guards, the JSON by the sweep -- so the renderers were
the one consumer nothing bound to the serializer.

The binding here is deliberately the same shape as the page's: the payload is
built by the REAL serializer from a real database rather than hand-written, so
a serializer that changes shape fails these tests instead of only failing at a
terminal. A hand-built dict would have passed the whole time the CLI was
crashing.
"""

from __future__ import annotations

import sqlite3

import pytest
from conftest import RAW_DIR, STATEMENTS, connect_migrated

from optjournal.history import build_history
from optjournal.ingest import ASSET_FILTER_ALL, ingest_file
from optjournal.render import (
    render_history,
    render_orders,
    render_positions,
    render_statements,
    render_summary,
)
from optjournal.serialize import (
    history_data,
    orders_data,
    positions_data,
    statements_data,
)


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    """A journal with every archived statement folded in, as the CLI sees it."""
    if not STATEMENTS:
        pytest.skip("needs an archived statement")
    c = connect_migrated(tmp_path / "render.db")
    for path in STATEMENTS:
        ingest_file(c, path, assets=ASSET_FILTER_ALL)
    return c


# The failure mode these close: a renderer reaching a key the serializer no
# longer sends. `float(dict)` raises TypeError, and a missing key raises
# KeyError, so simply calling each renderer over the real payload catches both.


def test_orders_render_over_the_real_payload(conn):
    data = orders_data(conn)
    assert data, "the archive should hold option orders"
    out = render_orders(data)
    assert "option order(s)" in out
    # The Money keys are read through their own halves, not as a dict: a
    # stringified dict would put "{'base'" in the output rather than raising.
    assert "{" not in out


def test_history_renders_both_tables_over_the_real_payload(conn):
    data = history_data(build_history(conn))
    out = render_history(data)
    assert "Position history" in out
    assert "Closed" in out and "Open" in out
    assert "{" not in out


def test_positions_render_over_the_real_payload(conn):
    out = render_positions(positions_data(conn))
    assert "{" not in out


def test_statements_render_over_the_real_payload(conn):
    out = render_statements(statements_data(RAW_DIR, conn))
    assert "archived statement(s)" in out


def test_an_order_shows_the_charge_and_the_translation(conn):
    """Both readings, from one `Money` -- not one figure printed twice.

    The bug replaced `proceeds_base` with a nested object, so the "base"
    column silently became the same source as the proceeds column. Asserting
    they come from different halves is what pins that they still differ.
    """
    orders = orders_data(conn)
    order = next(
        o for o in orders
        if o["proceeds"]["native"] is not None
        and o["proceeds"]["base"] != o["proceeds"]["native"]
    )
    out = render_orders([order])
    assert f"{order['proceeds']['native']:,.2f}" in out
    assert f"{order['proceeds']['base']:,.2f}" in out


def test_a_withheld_native_renders_as_a_dash_not_a_crash():
    """An order spanning currencies has `native: null`, and must still render.

    Hand-built because this account's option orders are all single-currency,
    so real data cannot reach the withheld branch -- the same reason
    test_analysis builds statements for the tax gate.
    """
    order = {
        "ib_order_id": "1", "underlyings": "GOOG", "leg_count": 1, "fills": 1,
        "first_fill_at": "2026-08-04 11:24:00",
        "proceeds": {"base": 373.74, "native": None, "ccy": None},
        "commission": {"base": -0.61, "native": None, "ccy": None},
        "legs": [],
    }
    out = render_orders([order])
    assert "proceeds -" in out
    # The base translation always answers, so the row is never information-free.
    assert "373.74" in out


def test_summary_renders_without_a_statement_path():
    out = render_summary({"statements": [{
        "account_id": "U1", "from_date": "2026-01-01", "to_date": "2026-12-31",
        "trades": 2, "distinct_orders": 2, "underlyings": 1,
        "cash_transactions": 0, "by_asset": {"OPT": 2},
        "by_open_close": {"O": 2}, "cash_by_type": {},
    }]})
    assert "account U1" in out


@pytest.mark.parametrize(
    "render, empty",
    [
        (render_orders, []),
        (render_positions, []),
        (render_statements, []),
    ],
)
def test_an_empty_payload_says_so_rather_than_raising(render, empty):
    assert render(empty)
