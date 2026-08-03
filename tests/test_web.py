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
from optjournal import web
from optjournal.web import build_state, page_html, serve

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
    return page_html().split("<script>")[1].split("</script>")[0]


_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
#: The `(?<!:)` keeps `://` in a URL from being mistaken for a comment start.
#: A protocol-relative `"//host"` would still be stripped, which is acceptable
#: here: `test_page_loads_no_external_resources` asserts the page has none.
_LINE_COMMENT = re.compile(r"(?<!:)//[^\n]*")


def _code_only(js: str) -> str:
    """The script with comments removed, so prose is not scanned as code.

    The guards below look for `ident.attr`. A comment that mentions a dotted
    expression in passing -- "this used to read from history.open" -- is
    indistinguishable from a real property access, and tripped the
    classification guard with a binding that exists nowhere in the code. A
    comment is not code, so it must not be scanned.

    Stripping is regex-based rather than a real tokenizer, which is sound for
    this file: it uses block comments exclusively, they are balanced, and it
    contains no `://` and no comment markers inside string literals. The
    helper is tested directly rather than trusted.
    """
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", js))


def test_code_only_strips_comments_and_keeps_code():
    js = """
    /* prose mentioning history.open and pos.bogus_key */
    const a = real.value;
    // a line comment about foo.bar
    const u = "https://example.com/x";
    """
    out = _code_only(js)
    assert "history.open" not in out
    assert "pos.bogus_key" not in out
    assert "foo.bar" not in out, "line comments must be stripped too"
    assert "real.value" in out, "real code must survive"
    assert "https://example.com/x" in out, "a URL is not a line comment"


def test_page_comments_are_balanced():
    """The regex stripper assumes balanced block comments; assert that holds.

    Deliberately reads the raw script: counting markers after stripping them
    would compare 0 to 0 and pass no matter what the file contained.
    """
    js = _js()
    assert js.count("/*") == js.count("*/"), "unbalanced block comments in page.html"


def test_state_is_pure_json(state):
    """No default=str crutch: the payload must serialise on its own.

    Decimal money and datetime.date periods both violate this, and both make
    it out of `analysis` unless coerced.
    """
    json.dumps(state)


def test_state_has_every_panel(state):
    for key in ("positions", "orders", "history", "statements", "costs", "sync"):
        assert key in state, f"panel data {key!r} missing"


#: Matches `ident.attr` where `ident` is not itself part of a property access,
#: so `S.state.positions` yields only `S.state`.
_PROPERTY_READ = re.compile(r"(?<![\w.$])([A-Za-z_$][\w$]*)\.([a-z_][a-z0-9_]*)\b")

#: Bindings that carry a dot but hold no API payload: page globals, DOM nodes,
#: fetch responses, and local collections whose reads are array methods.
#: Explicit by design -- see test_every_binding_is_classified.
_NOT_PAYLOAD_BINDINGS = frozenset({
    # page globals and builtins
    "Math", "S", "TABS",
    # DOM nodes and the fetch response
    "b", "sel", "m", "r",
    # local collections; the reads are array methods, not payload keys
    "arows", "cells", "days", "jrows", "legs", "month", "months", "oc",
    "open", "opts", "orders", "ps", "pts", "rows",
})


def _roots(state: dict) -> dict[str, dict]:
    """Every JS binding that holds an API payload object, mapped to that object.

    One name per shape, deliberately: `pos` for a position, `ep` for an episode,
    `dy` for a calendar day, `pt` for a chart point, `stm` for a statement.
    Reusing `d` for three different shapes once made this unbindable.

    Shared by both guards below. Registering a binding here is what brings it
    inside their reach, so this is the single place a new binding is declared.
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
        # `x` is the find-predicate binding over the same episode shape. It went
        # unregistered until guard two started demanding every binding be
        # classified, which means `x.conid` was never actually checked.
        roots["x"] = episodes[0]
    if costs["fx"]:
        roots["f"] = costs["fx"][0]
    if state["statements"]:
        roots["stm"] = state["statements"][0]
    fx = state.get("fx") or {}
    roots["fx"] = fx
    if fx.get("quotes"):
        roots["q"] = fx["quotes"][0]
        roots["qo"] = fx["quotes"][0]
    return roots


def test_every_js_property_read_resolves(state):
    """Guard one: a read on a registered binding must resolve against the payload.

    Catches a wrong KEY on a known binding -- `o.symbol` when the field is
    really `underlyings`. Blind to a binding it does not know about, which is
    what guard two exists for.
    """
    roots = _roots(state)
    js = _code_only(_js())
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


def test_every_binding_is_classified(state):
    """Guard two: every binding read in the page must be classified somewhere.

    This is the complement of guard one, and it closes the hole that let a blank
    Positions panel ship. Guard one only inspects bindings listed in `_roots`,
    so when a map binding was renamed `p` -> `pos`, the registered key moved and
    the single straggler `p.fifo_pnl_unrealized` fell outside everything that
    looks. Reading a property of an undefined variable throws, and a throw
    inside a template callback renders the whole table blank -- so the failure
    surfaced as an empty tab, not as a red test.

    A binding must therefore be either a payload object (`_roots`) or explicitly
    declared not to be (`_NOT_PAYLOAD_BINDINGS`). Introducing a name becomes a
    deliberate decision instead of a silent omission.
    """
    used = {m.group(1) for m in _PROPERTY_READ.finditer(_code_only(_js()))}
    unclassified = used - (set(_roots(state)) | _NOT_PAYLOAD_BINDINGS)

    assert not unclassified, (
        "these bindings are read in the page but classified nowhere, so guard "
        "one cannot see them and a stale rename or typo in any of them would "
        f"render blank instead of failing a test: {sorted(unclassified)}. Add "
        "each to _roots (holds a payload object) or to _NOT_PAYLOAD_BINDINGS "
        "(DOM node, builtin, or local collection)."
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
    page = page_html()
    assert not re.search(r'(src|href)="https?://', page)
    assert "cdn." not in page


def test_page_escapes_interpolated_values():
    """Statement filenames and symbols come from IBKR, so they are untrusted.

    Checks the invariant rather than a literal spelling: every template
    interpolation mentioning one of these fields must route through esc().
    An earlier version looked for the exact string `esc(l.symbol)` and broke
    on `esc(l.underlying_symbol||'')`, which is correct code.
    """
    js = _code_only(_js())
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


def test_dashboard_friction_is_split_by_scope(state):
    """The panel is headed by an asset category, so it must not blend scopes.

    It presented one `total_friction_base` pill labelled "friction (options)"
    that was options commission plus account-level fees -- the same defect the
    cost report carried. Fees carry no assetCategory, so ingest does not filter
    them and they cannot be attributed to options.
    """
    s = state["stats"]
    assert s["options_friction_base"] == abs(s["commissions_base"])
    assert s["account_friction_base"] == abs(s["fees_base"]) + abs(s["autofx_base"])
    # The split reapportions; it must not change or drop anything.
    assert (
        s["options_friction_base"] + s["account_friction_base"]
        == s["total_friction_base"]
    )
    # Guards the actual bug: the attributable figure must exclude fees.
    assert s["options_friction_base"] != s["total_friction_base"], (
        "fees are being counted as options friction again"
    )


def test_every_position_carries_a_cost_basis(state):
    """The book table shows a cost basis per row, so every row must have one.

    It used to read this only from `history.open`, where `cost_basis` is set
    exclusively for snapshot-only episodes. The short put has fills on record,
    so its episode carried None and the cell rendered a dash -- while
    position_snapshots held -1569.907847 all along. A negative basis is
    correct for a short: it is premium received, not money paid.
    """
    for pos in state["positions"]:
        assert pos.get("cost_basis_money") is not None, pos["symbol"]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "127.0.0.2"])
def test_loopback_addresses_accepted(host):
    from optjournal.web import _is_loopback

    assert _is_loopback(host)


def test_page_is_reread_per_call(tmp_path, monkeypatch):
    """An edit to page.html must show on reload, with no server restart.

    This used to be cached at import, and the stale copy was actively
    misleading: a server left running from an earlier session served a
    pre-edit page, so a screenshot taken to verify a UI change showed the old
    layout and looked like the change had failed.
    """
    page = tmp_path / "page.html"
    page.write_text("<html>first</html>", encoding="utf-8")
    monkeypatch.setattr(web, "PAGE_PATH", page)
    assert "first" in page_html()

    page.write_text("<html>second</html>", encoding="utf-8")
    assert "second" in page_html(), "edit not picked up -- the page is cached again"


def test_serve_fails_fast_when_the_page_is_missing(tmp_path, monkeypatch):
    """Reading per request must not defer a missing page to a browser 500."""
    monkeypatch.setattr(web, "PAGE_PATH", tmp_path / "absent.html")
    with pytest.raises(FileNotFoundError):
        serve(db_path=tmp_path / "x.db", archive_dir=tmp_path, host="127.0.0.1")


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


def test_fx_offers_the_base_and_at_least_one_quote(state):
    """The toggle needs a base and something to switch to.

    Quotes are whatever the snapshot holds, not a hardcoded list: an options-only
    journal yields USD alone, while ingesting every asset class also surfaces the
    KRW and SEK positions. Pinning this to exactly ["USD"] would encode the
    narrower ingest as if it were the only one.
    """
    fx = state["fx"]
    assert fx["base"] == state["stats"]["base_currency"]
    assert fx["base"]
    codes = [q["code"] for q in fx["quotes"]]
    assert "USD" in codes
    assert len(codes) == len(set(codes)), "a currency must not be offered twice"


def test_fx_never_quotes_the_base_currency(state):
    """Offering EUR->EUR would imply a conversion that does not happen."""
    fx = state["fx"]
    assert fx["base"] not in [q["code"] for q in fx["quotes"]]


def _usd_quote(state) -> dict:
    """The USD quote, selected by code -- index 0 is not guaranteed to be USD."""
    for quote in state["fx"]["quotes"]:
        if quote["code"] == "USD":
            return quote
    pytest.skip("needs a USD rate in the snapshot")


def test_fx_quote_inverts_the_stored_rate(state, populated):
    """`fx_rate_to_base` is native->base, so a base->native quote must invert it.

    Getting this backwards is silent: 0.867 and 1.153 are both plausible-looking
    EUR/USD rates, and the page would render totals 33% adrift with no error.
    """
    conn = sqlite3.connect(populated)
    stored = conn.execute(
        "SELECT fx_rate_to_base FROM position_snapshots WHERE currency = 'USD'"
        " ORDER BY report_date DESC LIMIT 1"
    ).fetchone()[0]
    conn.close()
    quote = _usd_quote(state)
    assert quote["per_base"] == pytest.approx(1.0 / stored, rel=1e-12)
    assert quote["per_base"] > 1.0 > stored


def test_fx_quote_carries_its_provenance(state):
    """A rate with no date or source is not auditable, so it must not ship bare."""
    quote = _usd_quote(state)
    assert quote["as_of"]
    assert quote["source"]


def test_fx_has_no_quotes_without_a_snapshot(tmp_path):
    """No dated rate means no toggle -- the page must not invent one."""
    db = tmp_path / "empty.db"
    conn = connect(db)
    migrate(conn)
    conn.close()
    state = build_state(db_path=db, archive_dir=tmp_path, query_id=None)
    assert state["fx"]["quotes"] == []


def test_restating_positions_reproduces_ibkrs_own_native_figures(state):
    """The strongest available check that the rate is applied the right way up.

    Position values were converted by IBKR at this very snapshot's rate, so
    restating the base figures back into the native currency must reproduce the
    native figures exactly. It holds only because both sides share one rate and
    one date -- the cost report spans a year of conversions at many rates, so
    there the restatement is genuinely an approximation, which is why the page
    labels it rather than presenting it as IBKR's own.
    """
    usd = [p for p in state["positions"] if p["currency"] == "USD"]
    if not usd:
        pytest.skip("needs a USD position")
    per_base = _usd_quote(state)["per_base"]
    restated = sum(p["position_value_base"] for p in usd) * per_base
    native = sum(p["position_value"] for p in usd)
    assert restated == pytest.approx(native, rel=1e-9)
