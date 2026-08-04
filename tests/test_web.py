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

from optjournal import web
from optjournal.cli import main
from optjournal.config import (
    DEFAULT_ARCHIVE,
    DEFAULT_DB,
    DEFAULT_DEMO_DB,
    DEFAULT_DEMO_DIR,
)
from optjournal.db import connect, migrate
from optjournal.history import build_history
from optjournal.ingest import ASSET_FILTER_ALL, ingest_file
from optjournal.web import build_state, page_html, serve

RAW_DIR = Path(__file__).resolve().parent.parent / "raw"
STATEMENTS = sorted(RAW_DIR.glob("activity-*.xml"))

#: Attributes on DOM nodes, promises and builtins -- not API payload keys.
_NOT_PAYLOAD = {
    "addEventListener", "background", "catch", "className", "color", "disabled",
    "filter", "isoformat", "join", "json", "length", "map", "ok", "push",
    "querySelector", "replace", "status", "style", "textContent", "then",
    "title", "toLocaleString", "some", "find", "forEach", "concat", "padStart",
    "split", "slice", "onclick", "onchange", "classList", "dataset",
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
    "b", "sel", "m", "r", "el",
    # browser globals the view-state-in-the-hash code reads
    "location", "window",
    # the URLSearchParams the state request and the hash are built from
    "qs", "hs",
    # local collections; the reads are array methods, not payload keys
    "cells", "days", "buckets", "evs", "glegs", "groups", "jrows", "lcs",
    "legs", "mons", "month", "months", "morders", "oc", "odtes", "olegs",
    "open", "opts", "orders", "out", "ps", "pts", "range", "rows", "yrs",
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
        "od": state["odte"],
        "co": state["odte"]["cohort"],
        # An annual row is the same stats shape as `s`, so the Annual view maps
        # over them as `s` and this only registers the running best-year
        # binding. Two names for one shape is fine -- `ep` and `x` already are;
        # what breaks the guard is one name for two shapes.
        "best": state["annual"][0],
    }
    # A strategy group and its member order: `g` is the group payload; the
    # member order is rendered as `o`, the same shape the flat orders list
    # uses, so it stays registered under `o` below.
    if state["strategies"]:
        roots["g"] = state["strategies"][0]
    # A position lifecycle; its `events` are strategy-group shaped and render
    # as `g`. The Positions tab's bucket is a page-side construct pairing a
    # lifecycle with its snapshot rows, like `pt` is for chart points.
    if state["lifecycles"]:
        roots["lc"] = state["lifecycles"][0]
    roots["bkt"] = {"lc": None, "rows": None}
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


def test_net_liq_is_absent_not_zero_without_equity_summaries(tmp_path):
    """No equity_summaries rows -> None, never 0.

    Reporting 0 would render 'Gain % of Net Liq: 0.0%', a wrong answer rather
    than an absent one. Pinned against an empty database on purpose: the real
    archive gained EquitySummaryInBase rows on 2026-08-03, so asserting this
    against ingested statements would pin the archive's contents, not the rule.
    """
    db = tmp_path / "no-nav.db"
    conn = connect(db)
    migrate(conn)
    conn.close()
    state = build_state(db_path=db, archive_dir=RAW_DIR, query_id=None)
    assert state["stats"]["net_liq_base"] is None
    assert state["stats"]["gain_pct_of_net_liq"] is None


def test_net_liq_populates_once_equity_summaries_exist(state):
    """The other half of the rule: rows present -> a dated, positive NAV."""
    if state["stats"]["net_liq_base"] is None:
        pytest.skip("archive carries no EquitySummaryInBase statement")
    assert state["stats"]["net_liq_base"] > 0
    assert state["stats"]["net_liq_date"]


def test_month_range_spans_account_life_and_contains_months(state):
    """`month_range` walks first activity to now; `months` is a subset of it.

    The range is what the calendar chevrons and the dropdown walk. It must be
    contiguous and newest-first, or prev/next would jump or reverse.
    """
    rng, months = state["month_range"], state["months"]
    assert set(months) <= set(rng)
    assert rng == sorted(rng, reverse=True)
    # contiguous: every consecutive pair is exactly one month apart
    for newer, older in zip(rng, rng[1:], strict=False):
        y, m = int(newer[:4]), int(newer[5:7])
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        assert older == f"{y:04d}-{m:02d}", f"gap between {newer} and {older}"


def test_a_fill_free_month_is_an_honest_zero_not_all_time(populated):
    """Selecting an in-range month with no option fills must not silently show
    the all-time figures -- that was the 'month selector does nothing' bug."""
    everything = build_state(db_path=populated, archive_dir=RAW_DIR, query_id=None)
    empty = [m for m in everything["month_range"] if m not in everything["months"]]
    if not empty:
        pytest.skip("every month in range has option fills")
    chosen = empty[0]
    st = build_state(
        db_path=populated, archive_dir=RAW_DIR, query_id=None, month=chosen
    )
    assert st["selected_month"] == chosen, "in-range month must be honoured"
    assert st["stats"]["total_trades"] == 0
    assert st["stats"]["net_pnl_base"] in (0, 0.0, None)


def test_a_month_outside_the_account_life_still_heals_to_all_time(populated):
    st = build_state(
        db_path=populated, archive_dir=RAW_DIR, query_id=None, month="1999-01"
    )
    assert st["selected_month"] is None


def test_a_trade_counts_only_in_the_month_it_closed(populated):
    """A round trip opened in one month and closed in the next belongs -- as a
    trade, a win/loss and P&L -- to the close month alone. The open month gets
    fills (activity) but no outcome. Verified against a real spanning episode
    rather than asserted in the abstract, with an independent recount as the
    oracle so other episodes in either month cannot mask a leak.
    """
    conn = connect(populated)
    try:
        report = build_history(conn, asset_category="OPT")
    finally:
        conn.close()
    spanning = [
        e for e in report.closed
        if e.opened_at and e.closed_at and e.opened_at[:7] != e.closed_at[:7]
    ]
    if not spanning:
        pytest.skip("archive has no closed round trip spanning two months")
    ep = spanning[0]
    open_month, close_month = ep.opened_at[:7], ep.closed_at[:7]

    def closed_in(month: str) -> list:
        return [e for e in report.closed if (e.closed_at or "")[:7] == month]

    opened = build_state(
        db_path=populated, archive_dir=RAW_DIR, query_id=None, month=open_month
    )["stats"]
    closed = build_state(
        db_path=populated, archive_dir=RAW_DIR, query_id=None, month=close_month
    )["stats"]

    # The open month has the fills but only the outcomes that closed IN it.
    assert opened["total_trades"] > 0, "the opening fills are that month's activity"
    assert opened["closed_episodes"] == len(closed_in(open_month))
    assert opened["net_pnl_base"] == pytest.approx(
        sum(e.realized_pnl_base for e in closed_in(open_month))
    ), "the spanning episode's outcome must not leak into the month that opened it"
    assert opened["wins"] + opened["losses"] == opened["closed_episodes"]
    # Commission rides the same rule: IBKR's episode P&L is already net of
    # every leg's commission, so fill-date commission showed the same euros
    # twice -- once in the open month's card, again inside the close month's
    # net P&L. The open month reports only commission of trades closed in it.
    assert opened["commissions_base"] == pytest.approx(
        sum(e.commission_base for e in closed_in(open_month))
    )

    # The close month carries the outcome, spanning episode included.
    assert closed["closed_episodes"] == len(closed_in(close_month)) >= 1
    assert closed["net_pnl_base"] == pytest.approx(
        sum(e.realized_pnl_base for e in closed_in(close_month))
    )
    # ... and the round trip's WHOLE commission, opening legs included.
    assert closed["commissions_base"] == pytest.approx(
        sum(e.commission_base for e in closed_in(close_month))
    )
    assert abs(closed["commissions_base"]) > abs(opened["commissions_base"])


def test_options_commission_reconciles_and_open_commission_is_separate(populated):
    """Monthly commissions must sum to the closed-episodes total, with the
    commission of still-open positions reported separately -- excluded for the
    same reason open premium is excluded from P&L, visible for the same reason
    the premium is: real cash, no outcome yet."""
    conn = connect(populated)
    try:
        report = build_history(conn, asset_category="OPT")
    finally:
        conn.close()
    everything = build_state(db_path=populated, archive_dir=RAW_DIR, query_id=None)
    monthly_sum = sum(
        build_state(
            db_path=populated, archive_dir=RAW_DIR, query_id=None, month=m
        )["stats"]["commissions_base"]
        for m in everything["month_range"]
    )
    closed_total = sum(e.commission_base for e in report.closed)
    assert monthly_sum == pytest.approx(closed_total)
    assert everything["stats"]["commissions_base"] == pytest.approx(closed_total)
    open_total = sum(e.commission_base for e in report.open)
    assert everything["stats"]["open_commission_base"] == pytest.approx(open_total)
    if open_total:  # strictness: real data currently has open META shorts
        assert everything["stats"]["commissions_base"] != pytest.approx(
            closed_total + open_total
        ), "open commission must not be folded into the headline figure"


def test_calendar_day_drilldown_is_wired():
    """A day with fills is clickable and toggles a detail panel drawn from the
    strategies payload already in hand -- a redraw, not a refetch."""
    js = _js()
    assert "data-calday=" in js, "day cells with activity carry the target"
    assert "S.calday=S.calday===k?null:k;draw();" in js.replace(" ", "").replace(
        "\n", ""
    ) or "S.calday===k?null:k" in js, "clicking the selected day clears it"
    assert "[data-calday]" in js and "[data-calday-clear]" in js
    assert "function dayDetail" in js
    # The drill-down redraws; only month navigation refetches.
    handler = js.split("[data-calday]")[1].split("forEach")[1][:200]
    assert "load()" not in handler, "day selection must not spend a request"


def test_trades_view_renders_strategy_groups():
    """One card per strategy, member orders visible beneath -- the strangle
    sold as two same-second orders is the case order-grouping cannot show."""
    js = _js()
    assert "S.state.strategies" in js
    assert "g.orders" in js and "g.label" in js and "g.order_ids" in js


def test_trades_view_renders_lifecycles_and_positions_group_by_them():
    """The Trades tab is one card per position lifecycle (open->close is one
    position, not two trades); the Positions tab buckets snapshot rows by the
    open lifecycle that owns their conids."""
    js = _js()
    assert "S.state.lifecycles" in js
    assert "lc.events" in js and "lc.status" in js and "lc.conids" in js


def test_a_lifecycle_spans_open_and_close_and_matches_the_dashboard(populated):
    """The real naked put: opened July, bought back August -- ONE closed
    lifecycle whose P&L equals the episode accounting the Dashboard uses."""
    conn = connect(populated)
    try:
        report = build_history(conn, asset_category="OPT")
    finally:
        conn.close()
    spanning = [e for e in report.closed
                if e.opened_at and e.closed_at
                and e.opened_at[:7] != e.closed_at[:7]]
    if not spanning:
        pytest.skip("archive has no closed round trip spanning two months")
    st = build_state(db_path=populated, archive_dir=RAW_DIR, query_id=None)
    ep = spanning[0]
    owning = [lc for lc in st["lifecycles"] if str(ep.conid) in lc["conids"]]
    assert len(owning) == 1, "exactly one lifecycle owns the contract"
    lc = owning[0]
    assert lc["status"] == "closed"
    assert len(lc["events"]) >= 2, "the open and the close are both present"
    assert lc["opened_at"][:10] == ep.opened_at[:10]
    assert lc["closed_at"][:10] == ep.closed_at[:10]
    assert lc["realized_pnl_base"] == pytest.approx(ep.realized_pnl_base)
    # An open lifecycle keeps the Dashboard's rule: nothing until flat.
    for open_lc in (x for x in st["lifecycles"] if x["status"] == "open"):
        assert open_lc["realized_pnl_base"] is None


def test_dashboard_headline_counts_closed_round_trips_for_options():
    """'Total Trades' as a fill count let a month claim trades whose outcome
    belonged to a later month -- open in July, close in August, and July's card
    said '3 trades' while its P&L, wins and losses all correctly read zero. For
    options the headline is closed round trips, the same population every other
    card on the row measures; fills survive in the sub-note, named as fills."""
    js = _js()
    assert "statCard('Trades', s.closed_episodes," in js
    assert "fill(s), ${s.orders} order(s)" in js, "fills stay visible as activity"
    # The fill-count headline remains only as the non-options branch.
    assert js.count("statCard('Total Trades', s.total_trades,") == 1


def test_fx_quotes_offer_only_option_trade_currencies(populated):
    """The toggle restates an options journal; SEK/KRW from stock positions are
    rate-quotable but not information. Codes must be a subset of currencies
    that appear on OPT trades."""
    conn = connect(populated)
    opt_codes = {
        str(r["currency"]).upper()
        for r in conn.execute(
            "SELECT DISTINCT currency FROM trades WHERE asset_category='OPT'"
        )
    }
    snapshot_codes = {
        str(r["currency"]).upper()
        for r in conn.execute("SELECT DISTINCT currency FROM position_snapshots")
    }
    conn.close()
    state = build_state(db_path=populated, archive_dir=RAW_DIR, query_id=None)
    offered = {q["code"] for q in state["fx"]["quotes"]}
    assert offered <= opt_codes, f"non-option currencies offered: {offered - opt_codes}"
    if snapshot_codes - opt_codes - {state["fx"]["base"]}:
        # The restriction is only proven when there was something to exclude.
        assert offered < snapshot_codes


def test_month_filter_narrows_the_payload(populated):
    from optjournal.db import connect
    from optjournal.stats import available_months

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
    import inspect

    from optjournal.web import _do_sync  # noqa: PLC0415 - private by design
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
    assert s["account_friction_base"] == abs(s["fees_base"])
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


# --------------------------------------------------------- annual / 0DTE tabs


def test_annual_and_odte_are_in_the_payload(state):
    """Both tabs were disabled with hardcoded reasons; now they have data."""
    assert state["annual"], "the Annual tab renders from this"
    assert set(state["odte"]) == {"cohort", "rest", "unknown_dte", "selectable"}
    # A cohort in isolation says nothing, so the comparison set must be present.
    assert state["odte"]["rest"]["episodes"] >= 0


def test_annual_rows_reconcile_with_the_all_time_row(state):
    """The Annual table shows a total row, so it must equal the sum of the years.

    `annual_total` is computed independently of the per-year rows, which is what
    makes this worth asserting: it is the same check a reader performs by eye.
    """
    years, everything = state["annual"], state["annual_total"]
    assert sum(y["total_trades"] for y in years) == everything["total_trades"]
    assert sum(y["closed_episodes"] for y in years) == everything["closed_episodes"]
    assert sum(y["net_pnl_base"] for y in years) == pytest.approx(
        everything["net_pnl_base"], abs=1e-9
    )


def test_every_episode_carries_an_odte_verdict(state):
    """The 0DTE tab filters on this flag, so it cannot be absent."""
    for ep in state["history"]["closed"] + state["history"]["open"]:
        assert "is_odte" in ep, ep["symbol"]
        assert ep["is_odte"] in (True, False, None)


def test_annual_and_odte_ignore_the_month_selector(populated):
    """Both are all-time by construction.

    A year-by-year table filtered to one month has a single row, and a 0DTE
    cohort of one month's trades is too small to compare against anything. The
    month selector stays wired to `stats` alone.
    """
    kw = dict(db_path=populated, archive_dir=RAW_DIR, query_id=None)
    unfiltered = build_state(**kw)
    month = unfiltered["months"][0]
    filtered = build_state(**kw, month=month)
    assert filtered["selected_month"] == month, "precondition: the filter applied"
    assert filtered["stats"]["month"] == month
    assert filtered["annual"] == unfiltered["annual"]
    assert filtered["odte"] == unfiltered["odte"]


# ------------------------------------------------------------- serve --demo


def _serve_kwargs(monkeypatch, argv: list[str]) -> dict:
    """Run `main` against a stubbed `serve`, returning the kwargs it received."""
    captured: dict = {}
    monkeypatch.setattr(web, "serve", lambda **kw: captured.update(kw))
    assert main(argv) == 0
    return captured


def test_demo_flag_redirects_both_paths(monkeypatch):
    """One flag, because pointing only --db at the demo is a silent mismatch.

    The archive is where the cost report is read from, so a demo database served
    beside the real archive would show synthetic trades against real costs.
    """
    kw = _serve_kwargs(monkeypatch, ["serve", "--demo"])
    assert kw["db_path"] == DEFAULT_DEMO_DB
    assert kw["archive_dir"] == DEFAULT_DEMO_DIR


def test_without_demo_the_real_paths_are_served(monkeypatch):
    kw = _serve_kwargs(monkeypatch, ["serve"])
    assert kw["db_path"] == DEFAULT_DB
    assert kw["archive_dir"] == DEFAULT_ARCHIVE


def test_an_explicit_path_wins_over_demo(monkeypatch, tmp_path):
    """--db and --archive default to None here so this is decidable at all.

    With the shared parents' defaults left in place, an explicit path equal to
    the default is indistinguishable from an absent one, and --demo would have
    had to overwrite it.
    """
    kw = _serve_kwargs(
        monkeypatch, ["serve", "--demo", "--db", str(tmp_path / "mine.db")]
    )
    assert kw["db_path"] == tmp_path / "mine.db"
    assert kw["archive_dir"] == DEFAULT_DEMO_DIR, "only --db was overridden"


def test_demo_refuses_a_query_id(monkeypatch, capsys):
    """A sync would put real trades in the synthetic database.

    Sync writes into the archive and database being served, so one click on a
    demo server with a query id spends an IBKR request to mix real fills with
    generated ones -- after which no figure in the journal means anything.
    """
    monkeypatch.setattr(web, "serve", lambda **kw: pytest.fail("must not serve"))
    assert main(["serve", "--demo", "--query-id", "1591754"]) == 2
    assert "Refused" in capsys.readouterr().err


# ------------------------------------------- monthly breakdown / trade scope


def test_monthly_is_in_the_payload_and_reconciles_with_annual(state):
    """The Annual tab groups months under years, so the two must agree."""
    months, years = state["monthly"], state["annual"]
    assert months, "the Month by month table renders from this"
    by_year: dict[str, list] = {}
    for m in months:
        by_year.setdefault(str(m["month"])[:4], []).append(m)
    assert set(by_year) == {y["month"] for y in years}
    for y in years:
        rows = by_year[y["month"]]
        assert sum(m["total_trades"] for m in rows) == y["total_trades"]
        assert sum(m["net_pnl_base"] for m in rows) == pytest.approx(
            y["net_pnl_base"], abs=1e-9
        )


def test_trade_type_defaults_to_everything(state):
    assert state["trade_type"] == "all"
    assert state["trade_type_label"]
    assert "selectable" in state["odte"], "the page derives the button state from this"


def test_trade_type_scope_narrows_the_payload(populated):
    """Wired end to end: the parameter must reach the aggregations.

    Skips when the archive has no 0DTE round trip to filter to -- the real
    account does not, which is why the demo suite carries the arithmetic.
    """
    kw = dict(db_path=populated, archive_dir=RAW_DIR, query_id=None)
    everything = build_state(**kw)
    if not everything["odte"]["selectable"]:
        pytest.skip("no 0DTE round trip in the archive to scope to")
    scoped = build_state(**kw, trade_type="odte")
    assert scoped["trade_type"] == "odte"
    assert scoped["stats"]["total_trades"] < everything["stats"]["total_trades"]
    assert len(scoped["orders"]) < len(everything["orders"])
    assert set(scoped["months"]) < set(everything["months"])


def test_an_unknown_trade_type_serves_the_whole_journal(populated):
    """The value comes from a query string, so it must fail open."""
    kw = dict(db_path=populated, archive_dir=RAW_DIR, query_id=None)
    baseline = build_state(**kw)
    for bad in ("", "nonsense", "OPTIONS"):
        state = build_state(**kw, trade_type=bad)
        assert state["trade_type"] == "all", bad
        assert state["stats"]["total_trades"] == baseline["stats"]["total_trades"]


def test_scope_does_not_reach_the_tabs_without_a_filter_bar(populated):
    """Positions, Costs, Annual and the cohorts render no filter, so must not narrow.

    A tab whose numbers move with a control it does not display gives the
    reader no way to explain the change.
    """
    kw = dict(db_path=populated, archive_dir=RAW_DIR, query_id=None)
    everything = build_state(**kw)
    scoped = build_state(**kw, trade_type="odte")
    assert scoped["positions"] == everything["positions"]
    assert scoped["costs"] == everything["costs"]
    assert scoped["annual"] == everything["annual"]
    assert scoped["monthly"] == everything["monthly"]
    assert scoped["odte"] == everything["odte"], (
        "the cohorts compare 0DTE against the rest, so scoping them to 0DTE "
        "would empty the other side of the comparison"
    )
    assert scoped["history"] == everything["history"]


# --------------------------------------------------------- view state in URL


def test_history_discipline_push_for_tabs_replace_for_the_rest():
    """Tab jumps are navigations; filter, month and currency changes are not.

    So the tab click is the one path allowed to push -- that is what makes the
    back button walk tabs -- while everything else mutates the current entry.
    Assigning location.hash would push indiscriminately, one entry per click.
    """
    js = _code_only(_js())
    assert "history.replaceState" in js
    assert "history.pushState" in js
    assert not re.search(r"location\.hash\s*=", js), (
        "assigning location.hash pushes history for every change"
    )
    # The push flag flows tab-click -> draw(true) -> syncHash(push); no other
    # caller passes it, so only tab jumps can mint history entries.
    assert "draw(true)" in js
    assert js.count("draw(true)") == 1, "only the tab click may push"
    assert re.search(r"function syncHash\(push\)", js)
    assert re.search(r"function draw\(push\)", js)


def test_hash_carries_every_piece_of_view_state():
    """Persisting only the filter restores a view that was never on screen.

    Reload would come back with the 0DTE scope applied but the month dropped
    and the tab reset to Dashboard.
    """
    js = _code_only(_js())
    for key in ("'tab'", "'type'", "'month'", "'ccy'"):
        assert f"hs.set({key}," in js, f"{key} is not written to the hash"
        assert f"hs.get({key})" in js, f"{key} is not read back from the hash"


def test_calendar_chevrons_walk_the_range_through_load():
    """Prev/next must mutate S.month and go through load(), the same path as
    the dropdown -- a chevron that only redraws would show a month the server
    never filtered for, and the two controls could disagree."""
    js = _js()
    assert "data-calmonth" in js
    binding = re.search(
        r"data-calmonth.*?b\.onclick=\(\)=>\{.*?S\.month=b\.dataset\.calmonth;load\(\);",
        js, re.S)
    assert binding, "chevron click must set S.month from the button and call load()"


def test_calendar_chevrons_disable_at_the_ends_of_the_range():
    """At the account's first month and the current month there is nowhere to
    go; a live button that does nothing reads as broken."""
    js = _js()
    assert re.search(r"data-calmonth=\"\$\{prev\|\|''\}\"\s*\$\{prev\?'':'disabled'\}", js)
    assert re.search(r"data-calmonth=\"\$\{next\|\|''\}\"\s*\$\{next\?'':'disabled'\}", js)
    # Walks month_range (the browsable range), not the fills-only months list.
    assert re.search(r"const range=S\.state\.month_range\|\|S\.state\.months", js)


def test_hash_is_applied_before_the_first_load():
    """Applied after loading would fetch the default view and then discard it."""
    js = _code_only(_js())
    assert re.search(r"applyHash\(\);\s*load\(\);", js), (
        "applyHash must run before the initial load"
    )


def test_a_tab_from_the_hash_is_validated_against_the_enabled_tabs():
    """An unknown or disabled id in the URL must not render an empty tab.

    And an ABSENT key must reset to the default rather than leave the current
    tab standing: the back button lands on entries whose hash has no tab key,
    and keeping the old tab made syncHash rewrite it into the entry just
    navigated to -- the back button appeared to do nothing.
    """
    js = _code_only(_js())
    assert "HASH_TABS().includes(tab)" in js
    assert re.search(r"S\.tab=\(tab&&HASH_TABS\(\)\.includes\(tab\)\)\?tab:'dashboard'", js)
    # Built from TABS with the disabled ones filtered out, so it cannot drift
    # from the tab bar as tabs are added or gated.
    assert re.search(r"HASH_TABS\s*=\s*\(\)\s*=>\s*TABS\.filter", js)


def test_hashchange_only_refetches_when_the_server_side_keys_moved():
    """The tab is drawn from state in hand; refetching for it wastes a request."""
    js = _code_only(_js())
    handler = js.split("onhashchange")[1]
    assert "load()" in handler and "draw()" in handler, (
        "the handler must choose between refetching and redrawing"
    )


def test_serves_path_defaults_do_not_poison_other_subcommands():
    """serve's None-path defaults must stay on serve.

    Overriding the shared parent parsers' defaults with set_defaults mutates
    the shared action objects, so `serve` setting archive=None reached every
    subcommand built from the same parents: `optjournal ingest` -- and the
    nightly cron's `sync` -- crashed on `None.glob(...)`. Found by running
    the command, not by the suite, which never parses these subcommands
    without explicit paths. Now it does.
    """
    from optjournal.cli import build_parser

    ap = build_parser()
    assert ap.parse_args(["ingest"]).archive == DEFAULT_ARCHIVE
    assert ap.parse_args(["ingest"]).db == DEFAULT_DB
    assert ap.parse_args(["sync", "0"]).archive == DEFAULT_ARCHIVE
    assert ap.parse_args(["statements"]).db == DEFAULT_DB
    # And serve keeps its deliberate None, which is what --demo relies on.
    assert ap.parse_args(["serve"]).archive is None
    assert ap.parse_args(["serve"]).db is None
