"""Tests for the local web UI.

The interesting failure mode here is not a crash, it is a page that renders
blank cells because the JavaScript reads a key the API never sends. That
happened four times while building this -- `total_friction_base` (a property,
so `asdict` dropped it), `o.fill_count` (really `fills`), `o.symbol` (really
`underlyings`), `t.size` (really `bytes`) and `l.trade_price` (really
`avg_price`) -- and none of it would have failed a conventional test, because
JavaScript reading a missing property yields `undefined` rather than raising.

So the main test here parses the property reads out of the embedded script and
asserts every one resolves against a real payload. It is crude, and it is the
only thing that would have caught any of those five.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from optjournal.db import connect, migrate
from optjournal.ingest import ASSET_FILTER_ALL, ingest_file
from optjournal.web import PAGE, build_state, serve

RAW_DIR = Path(__file__).resolve().parent.parent / "raw"
STATEMENTS = sorted(RAW_DIR.glob("activity-*.xml"))

#: Attributes on DOM nodes, promises and builtins -- not API payload keys.
_NOT_PAYLOAD = {
    "addEventListener", "background", "catch", "className", "color", "disabled",
    "filter", "isoformat", "join", "json", "length", "map", "ok", "push",
    "querySelector", "replace", "status", "style", "textContent", "then",
    "title", "toLocaleString", "some", "find", "forEach", "concat", "padStart",
    "split", "slice", "onclick", "onchange", "classList", "dataset", "disabled",
    "innerHTML", "add", "remove", "getDay", "getDate", "toFixed",
}


@pytest.fixture
def populated(tmp_path) -> Path:
    """A journal database with every archived statement ingested."""
    if not STATEMENTS:
        pytest.skip("needs an archived statement")
    db = tmp_path / "web.db"
    conn = connect(db)
    migrate(conn)
    for path in STATEMENTS:
        ingest_file(conn, path, assets=ASSET_FILTER_ALL)
    conn.close()
    return db


@pytest.fixture
def state(populated) -> dict:
    return build_state(db_path=populated, archive_dir=RAW_DIR, query_id="1591754")


def _js() -> str:
    return PAGE.split("<script>")[1].split("</script>")[0]


def test_state_is_pure_json(state):
    """No default=str crutch: the payload must serialise on its own.

    Decimal money and datetime.date periods both violate this, and both make
    it out of `analysis` unless coerced.
    """
    json.dumps(state)


def test_state_has_every_panel(state):
    for key in ("positions", "orders", "history", "statements", "costs", "sync"):
        assert key in state, f"panel data {key!r} missing"


def test_every_js_property_read_resolves(state):
    """The regression guard for silently-blank panels.

    Each JS variable is bound to the payload object it actually holds. The page
    keeps those names distinct on purpose -- `pos` for a position, `ep` for an
    episode, `dy` for a calendar day, `pt` for a chart point, `stm` for a
    statement -- precisely so this mapping is one-to-one and the check means
    something. Reusing `d` for three different shapes made it unbindable.
    """
    costs = state["costs"][0]
    history = state["history"]
    episodes = history["open"] or history["closed"]
    orders = state["orders"]

    roots: dict[str, dict] = {
        "st": state,
        "s": state["stats"],
        "c": costs,
        "T": costs["totals"],
        "h": history,
        # The /api/sync response, which has no fixture -- keys asserted by
        # test_sync_response_shape below.
        "res": {
            "kind": None, "ok": None, "message": None, "new_trades": None,
            "new_cash": None, "reused_archive": None, "warnings": None,
        },
        # Chart points are constructed client-side from stats.days, so their
        # shape is the page's own contract rather than the API's.
        "pt": {"x": None, "y": None, "n": None},
    }
    if state["stats"]["days"]:
        roots["dy"] = state["stats"]["days"][0]
    if state["positions"]:
        roots["pos"] = state["positions"][0]
    if orders:
        roots["o"] = orders[0]
        if orders[0].get("legs"):
            roots["l"] = orders[0]["legs"][0]
    if episodes:
        roots["ep"] = episodes[0]
    if costs["fx"]:
        roots["f"] = costs["fx"][0]
    if state["statements"]:
        roots["stm"] = state["statements"][0]

    js = _js()
    missing = []
    for var, obj in roots.items():
        for match in re.finditer(rf"(?<![\w.]){re.escape(var)}\.([a-z_][a-z0-9_]*)\b", js):
            attr = match.group(1)
            if attr in _NOT_PAYLOAD:
                continue
            if attr not in obj:
                missing.append(f"{var}.{attr}")

    assert not missing, (
        "the page reads keys the API does not send, so those cells render "
        f"blank rather than failing: {sorted(set(missing))}"
    )


def test_stats_panel_keys_present(state):
    """The dashboard's ten stat cards each need a real key."""
    s = state["stats"]
    for key in (
        "total_trades", "orders", "net_pnl_base", "commissions_base", "fees_base",
        "wins", "losses", "win_rate", "avg_win_base", "avg_loss_base",
        "closed_episodes", "open_episodes", "green_days", "red_days", "days",
        "total_friction_base", "net_liq_base", "gain_pct_of_net_liq",
    ):
        assert key in s, f"stats.{key} missing"


def test_net_liq_is_unavailable_not_zero(state):
    """Flex activity statements carry no NAV, so this must be None.

    Reporting 0 would render 'Gain % of Net Liq: 0.0%', which is a wrong
    answer rather than an absent one.
    """
    assert state["stats"]["net_liq_base"] is None
    assert state["stats"]["gain_pct_of_net_liq"] is None


def test_month_filter_narrows_the_payload(populated):
    from optjournal.stats import available_months
    from optjournal.db import connect

    conn = connect(populated)
    months = available_months(conn)
    conn.close()
    if not months:
        pytest.skip("no months with trades")

    scoped = build_state(
        db_path=populated, archive_dir=RAW_DIR, query_id=None, month=months[0]
    )
    assert scoped["selected_month"] == months[0]
    for day in scoped["stats"]["days"]:
        assert day["day"].startswith(months[0])


def test_unknown_month_falls_back_to_all_time(populated):
    state = build_state(
        db_path=populated, archive_dir=RAW_DIR, query_id=None, month="1999-01"
    )
    assert state["selected_month"] is None
    assert state["stats"]["month"] == "ALL"


def test_sync_response_shape_matches_what_the_page_reads():
    """Pin the /api/sync contract, which has no fixture to check against."""
    from optjournal.web import _do_sync  # noqa: PLC0415 - private by design

    import inspect
    src = inspect.getsource(_do_sync)
    for key in ("new_trades", "new_cash", "reused_archive", "warnings", "kind", "ok"):
        assert f'"{key}"' in src, f"/api/sync no longer returns {key!r}"


def test_page_loads_no_external_resources():
    """Offline by construction, and the CSP header assumes it."""
    assert not re.search(r'(src|href)="https?://', PAGE)
    assert "cdn." not in PAGE


def test_page_escapes_interpolated_values():
    """Statement filenames and symbols come from IBKR, so they are untrusted.

    Checks the invariant rather than a literal spelling: every template
    interpolation mentioning one of these fields must route through esc().
    An earlier version looked for the exact string `esc(l.symbol)` and broke
    on `esc(l.underlying_symbol||'')`, which is correct code.
    """
    js = _js()
    assert "const esc=" in js, "no escaping helper defined"

    untrusted = ("pos.symbol", "stm.file", "l.underlying_symbol", "l.expiry",
                 "o.underlyings", "o.ib_order_id")
    unescaped = []
    for expr in re.finditer(r"\$\{([^{}]*)\}", js):
        body = expr.group(1)
        for field in untrusted:
            if field in body and "esc(" not in body:
                unescaped.append(f"${{{body.strip()[:60]}}}")
    assert not unescaped, f"untrusted values interpolated without esc(): {unescaped}"


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.10", "example.com"])
def test_serve_refuses_non_loopback(host, tmp_path):
    """The page has no auth and exposes an entire account. Loopback or nothing."""
    with pytest.raises(ValueError, match="Loopback only"):
        serve(db_path=tmp_path / "x.db", archive_dir=tmp_path, host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "127.0.0.2"])
def test_loopback_addresses_accepted(host):
    from optjournal.web import _is_loopback

    assert _is_loopback(host)


def test_sync_panel_reports_cooldown(state):
    sync = state["sync"]
    assert sync["configured"] is True
    assert sync["cooldown_s"] > 0
    assert sync["cooldown_remaining_s"] >= 0


def test_sync_panel_disabled_without_query_id(populated):
    state = build_state(db_path=populated, archive_dir=RAW_DIR, query_id=None)
    assert state["sync"]["configured"] is False
    assert state["sync"]["cooldown_remaining_s"] == 0


def test_build_state_opens_its_own_connection(populated):
    """sqlite3 handles cannot cross threads and the server is threaded."""
    first = build_state(db_path=populated, archive_dir=RAW_DIR, query_id=None)
    second = build_state(db_path=populated, archive_dir=RAW_DIR, query_id=None)
    assert first["positions"] == second["positions"]


def test_build_state_survives_an_empty_database(tmp_path):
    db = tmp_path / "empty.db"
    conn = connect(db)
    migrate(conn)
    conn.close()
    state = build_state(db_path=db, archive_dir=tmp_path, query_id=None)
    assert state["positions"] == []
    assert state["costs"] == []
    json.dumps(state)
