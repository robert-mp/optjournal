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
    """The page's inline script, whatever attributes its tag carries.

    Tolerant of attributes because the tag became `<script type="module">` when
    the chart's arithmetic moved to /static/replay.js. That module is NOT scanned
    here and does not need to be: it receives plain arrays and numbers, never the
    payload, so every payload read this contract polices still happens in the
    page. A read moving into the module would show up as a property read on an
    undeclared binding, which is the same failure this guard already raises.
    """
    body = page_html().split("<script", 1)[1]
    script = body.split(">", 1)[1].split("</script>")[0]
    return _IMPORT.sub("", script, count=1)


_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
#: The leading ES module import. Dropped from the scanned script rather than
#: stripped by _code_only, because its quoted path parses as a property read on a
#: binding named `replay` that exists nowhere -- and stripping ALL string
#: literals broke the tests that legitimately assert on them.
_IMPORT = re.compile(r"^\s*import\s*\{[^}]*\}\s*from\s*['\"][^'\"]+['\"];?", re.M)
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

# ------------------------------------------------------------ the contract
#
# The payload contract lives IN THE PAGE: @typedef blocks and a bindings
# table at the top of its script declare every shape the page reads and
# which binding holds which shape. This section PARSES that contract and
# enforces it, so the suite owns no registry of its own. It used to: an
# ~80-entry hand-maintained map here taxed every new template variable with
# a classification edit 500 lines from the code that introduced it -- paid
# four times in one afternoon -- and still drifted (three entries survived
# the render functions that used them). Now declaring a binding is one
# token in page.html, in the same diff as the code, and both a MISSING and
# a STALE declaration are red tests.
#
# The enforcement chain: reads resolve against the typedefs (guard one),
# the typedefs are held to a real payload in both directions (drift test),
# and every binding must be classified with none stale (guard two). The
# contract lives in comments and the reads live in code, which is exactly
# the line _code_only already draws -- the two scanners cannot confuse one
# another's territory.

_TYPEDEF = re.compile(r"/\*\*\s*@typedef\s+\{Object\}\s+(\w+)(.*?)\*/", re.S)
_TYPEDEF_PROP = re.compile(r"@property\s+\{([^}]+)\}\s+(\[)?([a-z_][a-z0-9_]*)")
_BINDING_ROW = re.compile(r"@(payload|local)\s+([^\n]+)")


def _parse_contract(js: str) -> tuple[dict[str, dict[str, bool]], dict[str, str], set[str]]:
    """(shapes, bindings, locals) parsed from RAW script text.

    shapes:   {shape name: {key: is_optional}} -- `[key]` brackets mark keys
              the API sends only sometimes (costs_source, the sync reply's
              branch-dependent fields); required keys must always be sent.
    bindings: {binding name: shape name} from the @payload rows.
    locals:   names declared page machinery (DOM nodes, builtins, local
              collections) from the @local rows.

    Deliberately parses the raw script, not _code_only's output: the
    contract lives in comments, which is exactly what _code_only strips.
    """
    shapes: dict[str, dict[str, bool]] = {}
    for block in _TYPEDEF.finditer(js):
        name, body = block.group(1), block.group(2)
        shapes[name] = {
            prop.group(3): prop.group(2) == "["
            for prop in _TYPEDEF_PROP.finditer(body)
        }
    bindings: dict[str, str] = {}
    locals_: set[str] = set()
    for row in _BINDING_ROW.finditer(js):
        kind, tokens = row.group(1), row.group(2).split()
        if kind == "local":
            locals_.update(tokens)
        else:
            for token in tokens:
                var, _, shape = token.partition(":")
                bindings[var] = shape
    return shapes, bindings, locals_


def test_contract_parser_is_correct():
    """The parser is new load-bearing code, so it is tested directly rather
    than trusted -- the same policy _code_only gets. A parser that silently
    dropped keys would not make the guards pass vacuously (the both-ways
    drift test would report the dropped keys as undeclared), but it would
    make the failure message point at the wrong culprit."""
    doc = (
        "/** @typedef {Object} Thing -- header prose is ignored\n"
        " * @property {string} name\n"
        " * @property {number|null} maybe -- nullability is prose, presence is law\n"
        " * @property {string} [rare] -- optional: not always sent\n"
        " */\n"
        "/** the tables\n"
        " * @payload t:Thing u:Thing\n"
        " * @local foo bar\n"
        " * @local baz\n"
        " */\n"
    )
    shapes, bindings, locals_ = _parse_contract(doc)
    assert shapes == {"Thing": {"name": False, "maybe": False, "rare": True}}
    assert bindings == {"t": "Thing", "u": "Thing"}
    assert locals_ == {"foo", "bar", "baz"}


#: Shapes with no /api/state sample to check against: two page-side
#: constructs (chart points and Positions-tab buckets are built by the page,
#: not sent by the API) and the /api/sync reply (building a real one spends
#: an IBKR request; it is pinned against _do_sync's source instead, in
#: test_sync_response_shape_matches_what_the_page_reads).
_UNSAMPLED = frozenset({"ChartPoint", "Bucket", "SyncResponse"})


def _shape_samples(state: dict) -> dict[str, dict]:
    """One real instance of every sampled shape, from the live payload.

    Keyed per SHAPE, not per binding: this map changes when a new payload
    panel is born (rare), where the old registry changed on every new
    template variable (constant). It is the fixture-side anchor of the
    contract -- the thing that stops the typedefs agreeing with themselves.
    """

    def first(rows):
        return rows[0] if rows else None

    costs = first(state["costs"])
    orders = state["orders"]
    history = state["history"]
    replays = list(state["replays"].values())
    # A replay whose contract has strikes -- an empty strikes list would leave
    # the Strike shape unanchored and quietly exempt it from the contract.
    striped = first([r for r in replays if r["strikes"]])
    # Likewise for events: a snapshot-only replay has none, so sampling the
    # first replay would anchor Annotation to nothing and exempt it.
    evented = first([r for r in replays if r["events"]])
    samples = {
        "State": state,
        "Stats": state["stats"],
        # Anchors the nested money shape to a real figure, so the Money
        # typedef cannot drift from what the serializer actually sends.
        "Money": state["stats"]["commissions"],
        "Day": first(state["stats"]["days"]),
        "Position": first(state["positions"]),
        "Order": first(orders),
        "Leg": first(orders[0]["legs"]) if orders else None,
        "LegMoney": first(orders[0]["legs"])["money"] if orders else None,
        "Episode": first(history["closed"] + history["open"]),
        "History": history,
        "Costs": costs,
        "CostsTotals": costs["totals"] if costs else None,
        "FxRow": first(costs["fx"]) if costs else None,
        "Statement": first(state["statements"]),
        "FxBlock": state["fx"],
        "FxQuote": first(state["fx"]["quotes"]),
        "OdteBlock": state["odte"],
        "Cohort": state["odte"]["cohort"],
        "StrategyGroup": first(state["strategies"]),
        "Lifecycle": first(state["lifecycles"]),
        "Replay": striped,
        "Strike": first(striped["strikes"]) if striped else None,
        "Annotation": first(evented["events"]) if evented else None,
        "Sync": state["sync"],
    }
    missing = sorted(k for k, v in samples.items() if v is None)
    assert not missing, (
        f"the fixture no longer produces a sample for {missing} -- the drift "
        "test would silently stop covering those shapes, so this fails instead"
    )
    return samples


def test_contract_is_coherent(state):
    """Meta-guard: a typo anywhere in the contract machinery is itself red.

    Every binding must name a declared shape, no name may be both payload
    and local, the sample map may only name declared shapes, and every
    declared shape must either have a sample or an explicit exemption --
    so a new payload shape cannot silently skip the drift test.
    """
    shapes, bindings, locals_ = _parse_contract(_js())
    assert shapes and bindings and locals_, "the page lost its contract blocks"

    unknown = sorted(f"{v}:{s}" for v, s in bindings.items() if s not in shapes)
    assert not unknown, f"bindings bound to undeclared shapes: {unknown}"

    both = sorted(set(bindings) & locals_)
    assert not both, f"declared both @payload and @local: {both}"

    samples = _shape_samples(state)
    assert set(samples) <= set(shapes), (
        f"sample map names undeclared shapes: {sorted(set(samples) - set(shapes))}"
    )
    unanchored = sorted(set(shapes) - set(samples) - _UNSAMPLED)
    assert not unanchored, (
        f"shapes with neither a payload sample nor an exemption: {unanchored} "
        "-- add an extractor to _shape_samples or, for a page-side shape, "
        "an entry in _UNSAMPLED"
    )
    assert not _UNSAMPLED & set(samples), "an exempted shape has a sample after all"


def test_contract_matches_the_payload_both_ways(state):
    """The typedefs in page.html are held to a real payload in BOTH
    directions: a required key the API stopped sending fails (the contract
    cannot rot optimistic), and a key the API sends that the contract omits
    fails (the server cannot outrun its documentation). Optional keys --
    `[bracketed]` in the typedef -- are exempt from the first direction
    only."""
    shapes, _, _ = _parse_contract(_js())
    problems = []
    for name, sample in _shape_samples(state).items():
        declared = shapes[name]
        sent = set(sample)
        required = {key for key, optional in declared.items() if not optional}
        problems += [
            f"{name}.{key}: declared required, not sent -- fix the typedef "
            "or the serializer" for key in sorted(required - sent)
        ]
        problems += [
            f"{name}.{key}: sent but undeclared -- add one @property line "
            "to page.html" for key in sorted(sent - set(declared))
        ]
    assert not problems, "\n".join(problems)


def test_every_js_property_read_resolves():
    """Guard one: a read on a payload binding must be a key its declared
    shape carries. Catches a wrong KEY on a known binding -- `o.symbol` when
    the field is really `underlyings`. The typedef consulted here is itself
    held to the real payload by test_contract_matches_the_payload_both_ways,
    so the chain read -> typedef -> payload is closed without this test
    touching a fixture."""
    shapes, bindings, _ = _parse_contract(_js())
    js = _code_only(_js())
    missing = []
    for var, shape in bindings.items():
        declared = shapes.get(shape, {})
        for match in re.finditer(rf"(?<![\w.]){re.escape(var)}\.([a-z_][a-z0-9_]*)\b", js):
            attr = match.group(1)
            if attr in _NOT_PAYLOAD:
                continue
            if attr not in declared:
                missing.append(f"{var}.{attr} (shape {shape})")

    assert not missing, (
        "the page reads keys its contract does not declare, so those cells "
        "render blank rather than failing -- either the read is a typo or "
        f"the typedef in page.html is missing a line: {sorted(set(missing))}"
    )


def test_every_binding_is_classified_and_none_is_stale():
    """Guard two: every binding read in the page must be declared, and every
    declaration must still be read.

    The first direction closes the hole that let a blank Positions panel
    ship: an unregistered binding is invisible to guard one, so a stale
    rename renders blank instead of failing. The second direction is new,
    and earned: when the registry lived in this file, three entries
    (morders, olegs, orders) survived the render functions that used them
    by two rewrites -- nothing noticed, because nothing checked. A table
    that cannot outlive its code stays trustworthy.
    """
    _, bindings, locals_ = _parse_contract(_js())
    used = {m.group(1) for m in _PROPERTY_READ.finditer(_code_only(_js()))}
    declared = set(bindings) | locals_

    unclassified = sorted(used - declared)
    assert not unclassified, (
        f"read in the page but declared nowhere: {unclassified}. Add each to "
        "the tables at the top of page.html's script -- `name:Shape` on a "
        "@payload row if it holds API data, or to a @local row if it is a "
        "DOM node, builtin, or local collection."
    )

    stale = sorted(declared - used)
    assert not stale, (
        f"declared in page.html's tables but no longer read anywhere: {stale}. "
        "Remove the token -- a stale entry is exactly how the old registry "
        "rotted."
    )


def test_stats_panel_keys_present(state):
    """The dashboard's ten stat cards each need a real key."""
    s = state["stats"]
    for key in (
        "total_trades", "orders", "net_pnl", "commissions", "fees",
        "wins", "losses", "win_rate", "avg_win", "avg_loss",
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
    assert st["stats"]["net_pnl"]["base"] in (0, 0.0, None)


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
    assert opened["net_pnl"]["base"] == pytest.approx(
        sum(e.realized_pnl_base for e in closed_in(open_month))
    ), "the spanning episode's outcome must not leak into the month that opened it"
    assert opened["wins"] + opened["losses"] == opened["closed_episodes"]
    # Commission rides the same rule: IBKR's episode P&L is already net of
    # every leg's commission, so fill-date commission showed the same euros
    # twice -- once in the open month's card, again inside the close month's
    # net P&L. The open month reports only commission of trades closed in it.
    assert opened["commissions"]["base"] == pytest.approx(
        sum(e.commission_base for e in closed_in(open_month))
    )

    # The close month carries the outcome, spanning episode included.
    assert closed["closed_episodes"] == len(closed_in(close_month)) >= 1
    assert closed["net_pnl"]["base"] == pytest.approx(
        sum(e.realized_pnl_base for e in closed_in(close_month))
    )
    # ... and the round trip's WHOLE commission, opening legs included.
    assert closed["commissions"]["base"] == pytest.approx(
        sum(e.commission_base for e in closed_in(close_month))
    )
    assert abs(closed["commissions"]["base"]) > abs(opened["commissions"]["base"])


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
        )["stats"]["commissions"]["base"]
        for m in everything["month_range"]
    )
    closed_total = sum(e.commission_base for e in report.closed)
    assert monthly_sum == pytest.approx(closed_total)
    assert everything["stats"]["commissions"]["base"] == pytest.approx(closed_total)
    open_total = sum(e.commission_base for e in report.open)
    assert everything["stats"]["open_commission"]["base"] == pytest.approx(open_total)
    if open_total:  # strictness: real data currently has open META shorts
        assert everything["stats"]["commissions"]["base"] != pytest.approx(
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
    assert lc["realized_pnl"]["base"] == pytest.approx(ep.realized_pnl_base)
    # An open lifecycle keeps the Dashboard's rule: nothing until flat.
    for open_lc in (x for x in st["lifecycles"] if x["status"] == "open"):
        assert open_lc["realized_pnl"] is None


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
    assert s["options_friction"]["base"] == abs(s["commissions"]["base"])
    assert s["account_friction_base"] == abs(s["fees"]["base"])
    # The split reapportions; it must not change or drop anything.
    assert (
        s["options_friction"]["base"] + s["account_friction_base"]
        == s["total_friction_base"]
    )
    # Guards the actual bug: the attributable figure must exclude fees.
    assert s["options_friction"]["base"] != s["total_friction_base"], (
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
    assert sum(y["net_pnl"]["base"] for y in years) == pytest.approx(
        everything["net_pnl"]["base"], abs=1e-9
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
        assert sum(m["net_pnl"]["base"] for m in rows) == pytest.approx(
            y["net_pnl"]["base"], abs=1e-9
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
    and the tab reset to Dashboard. `calday` joined the set late: the
    drill-down selection was the one piece of view state the URL did not carry,
    so "that day" was unshareable and a reload dropped the panel while leaving
    the calendar looking untouched.
    """
    js = _code_only(_js())
    for key in ("'tab'", "'type'", "'month'", "'ccy'", "'calday'"):
        assert f"hs.set({key}," in js, f"{key} is not written to the hash"
        assert f"hs.get({key})" in js, f"{key} is not read back from the hash"


def test_a_calday_the_payload_cannot_show_heals_out_of_the_hash():
    """Validated in draw(), BEFORE syncHash writes the URL.

    dayDetail has its own guard, but it runs during render -- by which point the
    stale key is already in the address bar. A day from another month, or one
    the current filter excludes, must not survive in a URL describing nothing.
    Client-side only, so a calday change redraws without refetching: the
    hashchange handler compares only the server-side keys.
    """
    js = _code_only(_js()).replace(" ", "").replace("\n", "")
    assert "if(S.calday&&S.state&&!(((S.state.stats||{}).days)||[])" in js, \
        "calday is not healed against the payload"
    draw = _fn("draw").replace(" ", "").replace("\n", "")
    assert draw.index("S.calday=null") < draw.index("syncHash("), \
        "the heal must run before the hash is written"
    assert "calday" not in _code_only(_js()).split("window.onhashchange")[1], \
        "a calday change must not trigger a refetch"


def test_the_grid_shows_the_month_the_calday_names():
    """A calday from the URL outranks the month the dropdown would pick.

    The bug: #calday=2026-07-24 rendered AUGUST's grid with a panel captioned
    "2026-07-24 -- 1 fill" beneath it and no cell highlighted, because the month
    came from the dropdown while the panel came from the hash. Detail for a day
    that is not on screen.

    Safe precisely because draw() heals a calday the payload cannot show BEFORE
    the calendar runs (see the test above): anything reaching here is in
    `s.days`, so the month it names is inside the period and its grid cannot
    come back empty.

    Found by `optjournal sweep`, which asserts exactly one selected cell -- a
    state that was unreachable, and so uncheckable, until calday joined the
    hash.
    """
    js = _fn("calendar").replace(" ", "").replace("\n", "")
    assert "constmonth=S.calday?S.calday.slice(0,7)" in js, \
        "the calendar grid does not follow the calday from the URL"


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


def test_positions_table_shows_the_side_column():
    """Long/Short straight from IBKR's snapshot `side` field -- explicit,
    not left to the reader inferring it from a signed quantity."""
    js = _code_only(_js())
    assert "<th>side</th>" in js, "side column header missing"
    assert "pos.side" in js, "side cell not bound to the snapshot field"


def test_lifecycle_event_labels_are_contextual_inside_their_card():
    """Within its own card, an event that merely repeats the card's shape is
    captioned Opened/Closed; an event carrying new information (a Roll, a
    different shape) keeps its full name. Presentation only -- g.label is
    unchanged in the payload, and the calendar drill-down (no card context)
    still shows the full name."""
    js = _code_only(_js())
    assert "g.label===lc.label?'Opened'" in js.replace(" ", "")
    assert "g.label===lc.label+'close'?'Closed':g.label" in js.replace(" ", "")


# ---------------------------------------------------------------- UI defect pins
#
# Eight rendering defects, each pinned by the narrowest assertion that would
# have failed before its fix. Four were invisible to the existing suite because
# they lived in CSS, which nothing here had ever read -- hence _css().


def _css() -> str:
    return page_html().split("<style>")[1].split("</style>")[0]


def _fn(name: str) -> str:
    """One render function's source, so a pin cannot be satisfied elsewhere."""
    js = _code_only(_js())
    start = js.index(f"function {name}(")
    nxt = js.find("\nfunction ", start + 1)
    return js[start : nxt if nxt != -1 else len(js)]


def test_header_cluster_right_aligns_and_groups_its_icons():
    """`align-items` is pinned at BOTH levels, for two different reasons.

    On .hdr-actions the default `stretch` was opted out of by .icobtn's
    explicit width, and a definite cross-size lands an item at the cross-axis
    start -- so the sync and cog buttons sat hard left of the right-aligned
    note above them. Inside #ccywrap `stretch` was NOT opted out of: .ccytog
    has no width, so it inflated to the wrap's width, itself widened to 210px
    by .ccynote's max-width, leaving the rounded border extending past the
    active button with dead space inside it.
    """
    css = _css().replace(" ", "").replace("\n", "")
    assert "align-items:flex-end" in css.split(".hdr-actions{")[1].split("}")[0]
    assert "align-items:flex-end" in css.split("#ccywrap{")[1].split("}")[0], \
        "the currency toggle will stretch to the note's width again"
    assert ".hdr-icons{display:flex" in css
    # Both icons in the row wrapper, or they stack again.
    head = page_html().split("</style>")[1]
    icons = head.split('class="hdr-icons"')[1].split("</div>")[0]
    assert 'id="sync"' in icons and 'id="cog"' in icons


def test_the_drilldown_leg_row_gets_a_sixth_grid_column():
    """legRow emits six children when `extra` is passed (the calendar
    drill-down wedges a strategy label in), and .leg declares five columns --
    so the sixth child, the proceeds figure, wrapped onto a second row and
    left-aligned under the 52px action column, away from the price it belongs
    beside. The variant carries the extra track so Trades rows keep exactly
    five and gain no stray gap.
    """
    css = _css().replace(" ", "").replace("\n", "")
    base = css.split(".leg{")[1].split("}")[0]
    assert base.count("auto") == 3, "base .leg should stay a five-column grid"
    ctx = css.split(".leg.ctx{")[1].split("}")[0]
    assert ctx.count("auto") == 4, "the drill-down variant needs a sixth track"
    # Compared against the space-stripped source, so the class literal's own
    # leading space is gone here too.
    assert '"leg${extra?\'ctx\':\'\'}"' in _fn("legRow").replace(" ", ""), \
        "legRow must tag the six-child variant or the CSS never applies"


def test_the_positions_side_cell_has_a_colour_rule_that_matches():
    """The only buy/sell rules were compound (.act.buy / .act.sell), so the
    bare <td class="sell"> the table emitted matched nothing and Long/Short
    both rendered default white -- colour coding the markup asked for and
    never got. Scoped to .side so the hue cannot leak onto other elements.
    """
    css = _css().replace(" ", "").replace("\n", "")
    assert ".side.buy{color:" in css and ".side.sell{color:" in css
    assert 'class="side${String(pos.side)' in _fn("positions").replace(" ", "")


def test_position_group_subtotal_sits_under_the_value_column():
    """The subtotal sums position_value_base, so it belongs in `value`. It used
    to ride a colspan=8 label into the last column, under `record`, four columns
    from the figures it totals.

    The arithmetic is DERIVED from the header row rather than hardcoded. The
    hardcoded form failed merely because a column was added (the `trend`
    miniature), which says nothing about whether the subtotal still lines up --
    the thing actually worth guarding. Now it fails only when the alignment
    genuinely breaks.
    """
    body = _fn("positions").replace(" ", "").replace("\n", "")
    headers = re.findall(r"<th[^>]*>(.*?)</th>", _fn("positions"))
    total = len(headers)
    value_at = next(i for i, h in enumerate(headers) if h.strip() == "value")
    grp = body.split('<trclass="grp">')[1].split("</tr>")[0]
    assert f'colspan="{value_at}"' in grp, (
        f"the label span must cover the {value_at} column(s) that precede `value`"
    )
    assert f'colspan="{total - value_at - 1}"' in grp, (
        "the trailing span must cover every column after `value`"
    )
    assert f'colspan="{total}"' not in grp, "the label must not span the whole row"


def test_the_chart_axis_follows_the_display_currency():
    """Tick values come from realized_base, so labelling them with num() left
    the axis in the base currency while the headline, the cards and every dot
    tooltip on the same chart restated -- the one figure on the page that
    ignored the toggle, and it carried no symbol to admit which currency it
    meant.
    """
    chart = _fn("chart").replace(" ", "")
    assert "text-anchor=\"end\">${cash(v,0)}" in chart, "axis ticks not restated"
    assert "${num(v,0)}" not in chart


def test_dashboard_commission_reads_the_same_as_the_tables():
    """The same all-time figure read "-EUR26.25" on the Dashboard card and
    "EUR26.25" in the Annual and Month tables, so cross-checking one against
    the other required knowing each surface's sign convention.

    Agreement is now structural rather than coincidental: all three read one
    helper, so a change to the basis cannot land on one surface only. That is
    a stronger guarantee than the identical-expression assertion this replaced,
    which passed only while the three call sites happened to be spelled alike.
    """
    js = _code_only(_js()).replace(" ", "").replace("\n", "")
    assert "constchargeOf=(nat,ccy,base)=>isNativeCharge(nat,ccy)" in js, \
        "the shared charge helper is gone"
    for fn in ("dashboard", "annual", "monthlyTable"):
        body = _fn(fn).replace(" ", "").replace("\n", "")
        assert "moneyOf(s.commissions)" in body, \
            f"{fn} does not use the shared helper"
        assert "cash(Math.abs(s.commissions.base))" not in body, \
            f"{fn} bypasses the helper and can drift from the others"
    # Magnitude, not sign: the tint went with the sign it no longer shows.
    assert "cls(s.commissions.base)" not in _fn("dashboard").replace(" ", "")


def test_commission_shows_the_charge_when_the_reader_is_in_that_currency():
    """Commission is billed and debited in one currency -- IBKR takes dollars
    for a US option, never euros. `commissions_base` is an accounting
    translation, so restating it into a display currency round-trips a USD
    charge through two different rates (its own trade-date rate to EUR at
    ingest, one snapshot rate back out) and does not return: $6.9652 billed
    read $7.0021 on screen.

    The native figure is exact but unaddable, so it is used only when the
    display currency IS the currency charged; a scope spanning currencies
    serves null and the display falls back to the restatement.
    """
    js = _code_only(_js()).replace(" ", "").replace("\n", "")
    # The gate is currency identity, not merely presence of a native figure.
    assert "constisNativeCharge=(nat,ccy)=>nat!=null&&ccy===DISP()" in js
    # money(), not cash(): a native amount must NOT be passed through conv(),
    # which would reintroduce the round trip the native figure exists to avoid.
    assert "money(Math.abs(nat),ccy):cash(Math.abs(base))" in js
    assert "cash(Math.abs(nat)" not in js

    # BOTH commission figures route through the one rule. They share a sentence
    # on the card -- "as charged - $2.83 on open positions" -- so one being a
    # restatement while the other is a charge would be a contradiction in a
    # single line of prose.
    # One entry point now, not a wrapper per figure: `moneyOf` applies the rule
    # to any Money-shaped key, so a new gated figure needs no new helper and
    # cannot arrive with a subtly different rule of its own.
    assert "constmoneyOf=mo=>mo==null?cash(null):chargeOf(mo.native,mo.ccy,mo.base);" in js
    card = _fn("dashboard").replace(" ", "").replace("\n", "")
    assert "moneyOf(s.commissions)" in card and "moneyOf(s.open_commission)" in card
    assert "cash(Math.abs(s.open_commission.base))" not in card, \
        "the open-positions figure bypasses the shared rule"


def test_net_pnl_is_shown_as_realised_not_restated(state):
    """The headline number took the same treatment as commission, and the error
    it was carrying was far larger.

    IBKR books each episode's realised P&L in the currency it settled in, and
    stores that native figure alongside the base translation. Restating the base
    sum into a display currency sends every episode on a round trip -- its own
    date's rate out, one snapshot rate back -- so the figure drifted by
    $1.62 on this account's Net P&L, against $0.03 on its commission. The
    dashboard leads with that number.
    """
    pnl = state["stats"]["net_pnl"]
    assert set(pnl) == {"base", "native", "ccy"}
    # Every closed option episode on this account settled in USD, so the gate
    # answers rather than withholding.
    assert pnl["ccy"] == "USD"
    assert pnl["native"] is not None
    # The two readings are close but NOT equal -- if they were, the conversion
    # would be a no-op and this test would prove nothing.
    assert pnl["native"] != pytest.approx(pnl["base"])

    # Averages divide both halves by the same count, so an average can never be
    # an exact numerator over a restated denominator.
    win = state["stats"]["avg_win"]
    if win is not None and win["native"] is not None:
        assert win["ccy"] == pnl["ccy"]

    # Daily rows carry it too, so the calendar and the chart agree with the card.
    days = [d for d in state["stats"]["days"] if d["realized"]["base"]]
    assert days, "fixture has no day with realised P&L"
    for day in days:
        assert set(day["realized"]) == {"base", "native", "ccy"}

    # And the page shows it through the one rule.
    js = _code_only(_js()).replace(" ", "").replace("\n", "")
    assert "moneyOf(s.net_pnl)" in js
    assert "cash(s.net_pnl" not in js, "a display site bypasses the rule"


def test_journal_taxes_is_a_money_and_the_gate_reaches_it():
    """The last cash figure that shipped as a bare `_base` float.

    `journal_taxes_base` had a scoped base total and no path to the as-charged
    amount, because the journal-scoped tax ledger existed only inlined inside
    `journal_friction_native_by_ccy` -- aggregated, but not reachable.

    Both branches are exercised here rather than on the live payload, because
    neither journal has a single taxed trade: `journal_taxes_base` is 0.0 on
    both, so real data cannot distinguish a working gate from one that never
    runs.
    """
    from decimal import Decimal
    from types import SimpleNamespace

    from optjournal.analysis import analyse
    from optjournal.serialize import costs_data

    def fill(taxes, ccy, rate="1"):
        return SimpleNamespace(
            assetCategory=SimpleNamespace(value="OPT"), symbol="TSLA",
            currency=ccy, ibCommissionCurrency=ccy,
            proceeds=Decimal("-1000"), ibCommission=Decimal("-1.00"),
            taxes=Decimal(taxes), fxRateToBase=Decimal(rate),
            quantity=Decimal("1"), notes=[],
        )

    def totals(trades):
        stmt = SimpleNamespace(fromDate="20250801", toDate="20260731",
                              Trades=trades, CashTransactions=[])
        return costs_data(analyse(stmt))["totals"]

    uniform = totals([fill("-0.25", "USD"), fill("-0.75", "USD")])
    assert set(uniform["journal_taxes"]) == {"base", "native", "ccy"}
    assert uniform["journal_taxes"] == {"base": 1.0, "native": 1.0, "ccy": "USD"}
    assert "journal_taxes_base" not in uniform, "the flat key outlived its Money"

    # A second currency must withhold the native while keeping the base whole:
    # an exact-looking figure covering part of a total is the failure the gate
    # exists to prevent.
    mixed = totals([fill("-0.25", "USD"), fill("-3.00", "SEK", rate="0.09")])
    assert mixed["journal_taxes"]["native"] is None
    assert mixed["journal_taxes"]["ccy"] is None
    assert mixed["journal_taxes"]["base"] == pytest.approx(0.25 + 3.00 * 0.09)

    # And the page reads it through the one rule, not a second `cash()` call.
    js = _code_only(_js()).replace(" ", "").replace("\n", "")
    assert "moneyOf(T.journal_taxes)" in js
    assert "cash(T.journal_taxes" not in js, "a display site bypasses the rule"


def test_the_page_has_exactly_one_native_first_rule():
    """`natCash(v, rate)` was a second implementation of native-first display,
    surviving beside `chargeOf` for the Positions tab. Two hops rather than one:
    it multiplied the native by the row's rate to reach base, then applied the
    display rate -- so a USD figure shown under a USD toggle round-tripped
    through EUR. That is lossless only while the display rate happens to be the
    row's rate inverted, which holds on a single-snapshot single-currency
    account and stops holding the moment positions span report dates.

    Money.at_rate carries the native, so the display uses it verbatim. One
    concept, one implementation.
    """
    js = _code_only(_js()).replace(" ", "").replace("\n", "")
    assert "natCash" not in js, "a second native-first converter is back"
    for key in ("pos.value", "pos.cost_basis", "pos.unrealized"):
        assert f"moneyOf({key})" in js, f"{key} does not route through the rule"
    # The raw columns stay in the payload (the row IS the snapshot record) but
    # the page must not reach past the Money to reach them.
    for raw in ("pos.position_value_base", "pos.cost_basis_money",
                "pos.fifo_pnl_unrealized"):
        assert raw not in js, f"the page reads {raw} instead of its Money"


def test_the_calendar_pills_describe_the_month_on_the_grid():
    """Under "All time" the payload's days span the account while the grid can
    draw only one month, so the pills described a scope the grid could not
    account for -- on the demo journal, a blank August beside "Green days 7".
    The grid now lands on the newest month that HAS a day, and the pills are
    derived from the days actually on it, which for a specific month
    reproduces green_days/red_days exactly.
    """
    cal = _fn("calendar")
    assert "s.green_days" not in cal and "s.red_days" not in cal, \
        "pills read the period-wide counts again"
    flat = cal.replace(" ", "").replace("\n", "")
    assert "shown.filter(dy=>dy.realized.base>0).length" in flat
    assert "shown.filter(dy=>dy.realized.base<0).length" in flat
    # The fallback is the newest ACTIVE month, not the newest month of the
    # account's life -- month_range[0] is only the last resort.
    assert "active[active.length-1]" in flat


def test_strike_keeps_a_half_and_stays_bare_when_whole():
    """A strike is an identifier as much as a number: 267.5 and 268 are
    different contracts, and num(v,0) rounded one into the other. Latent on
    this account only because every strike it has held is whole -- which is
    also why whole strikes must keep rendering bare rather than being padded
    to two places for the rare half.
    """
    js = _code_only(_js()).replace(" ", "").replace("\n", "")
    assert "conststrike=v=>v==null?''" in js, "the strike helper is gone"
    assert "Number.isInteger(Number(v))?num(v,0)" in js
    leg = _fn("legRow").replace(" ", "")
    assert "${strike(l.strike)}" in leg, "legRow still formats the strike inline"
    assert "num(l.strike,0)" not in leg, "the rounding call survives"


def test_a_leg_falls_back_to_its_own_symbol_for_display():
    """A stock leg's underlying is the stock, so a blank underlying_symbol must
    not blank the contract cell -- it rendered as empty space beside a real
    position on the synthetic journal.
    """
    leg = _fn("legRow").replace(" ", "")
    assert "esc(l.underlying_symbol||l.symbol||'')" in leg


def test_proceeds_and_friction_follow_the_same_charge_rule_as_commission():
    """Premium and friction are cash in a contract's own currency, so they take
    the treatment commission takes: exact when one currency accounts for the
    figure, restated when mixed. Routed through the same chargeOf() so a change
    to the rule cannot reach one figure and miss another.
    """
    js = _code_only(_js()).replace(" ", "").replace("\n", "")
    # A leg is the one payload shape still carrying the triple flat, so it
    # reaches chargeOf directly rather than through moneyOf. Both paths are the
    # SAME rule -- moneyOf delegates to chargeOf -- which is the property that
    # stops a change reaching one figure and missing another.
    # There is now exactly ONE entry point. `legProceedsOf` existed only because
    # a leg carried its triple flat; the leaf now carries a Money too, so every
    # display site in the page goes through `moneyOf`.
    assert "legProceedsOf" not in js, "the second entry point is back"
    assert "constmoneyOf=mo=>mo==null?cash(null):chargeOf(" in js, \
        "moneyOf no longer delegates to the shared rule"
    card = _fn("dashboard").replace(" ", "").replace("\n", "")
    assert "moneyOf(s.open_premium)" in card and "moneyOf(s.options_friction)" in card
    assert "cash(s.open_premium.base)}</b>" not in card, "pill bypasses the rule"
    assert "cash(s.options_friction.base)" not in card, "pill bypasses the rule"
    assert "moneyOf(l.money.proceeds)" in _fn("legRow").replace(" ", "")


# --------------------------------------------------------------------------
# event annotations
# --------------------------------------------------------------------------

def _event(label, *legs, at="2026-07-24 10:35:01", **money):
    """One lifecycle event in the shape strategies.py actually emits."""
    return {
        "label": label,
        "first_fill_at": at,
        "orders": [{"legs": list(legs)}],
        "proceeds": money.get("proceeds"),
        "commission": money.get("commission"),
        "realized_pnl": money.get("realized_pnl"),
    }


def _leg(strike, right, side, oc, qty, price):
    return {
        "strike": strike, "put_call": right, "buy_sell": side,
        "open_close": oc, "quantity": qty, "avg_price": price,
    }


def test_an_events_kind_comes_from_its_legs_not_its_label():
    """The label is prose meant for a human ("Short put close"), so matching on
    it would break the moment classify() rewords. A ROLL is the case that
    matters: it closes and opens in one act, so neither marker alone describes
    it, and colouring it as an open or a close would misreport which.
    """
    lifecycle = {"events": [
        _event("Short put", _leg(270, "P", "SELL", "O", -3, 5.24)),
        _event("Short put close", _leg(270, "P", "BUY", "C", 3, 2.60),
               at="2026-08-03 09:55:23"),
        _event("Roll", _leg(420, "C", "BUY", "C", 1, 1.10),
               _leg(410, "C", "SELL", "O", -1, 2.30), at="2026-08-10 11:00:00"),
    ]}
    kinds = [row["kind"] for row in web._annotations(lifecycle, [])]
    assert kinds == ["open", "close", "roll"]


def test_an_opening_event_reports_no_realised_pnl():
    """The episode layer reports 0.0 on an opening event, and a card reading
    "realised $0.00" beside an opening credit invites the reader to conclude the
    trade made nothing rather than that it has not finished.
    """
    zero = {"base": 0.0, "native": None, "ccy": None}
    lifecycle = {"events": [
        _event("Short put", _leg(270, "P", "SELL", "O", -3, 5.24),
               realized_pnl=zero),
    ]}
    assert web._annotations(lifecycle, [])[0]["realized"] is None

    closed = {"events": [
        _event("Short put close", _leg(270, "P", "BUY", "C", 3, 2.60),
               realized_pnl={"base": 684.59, "native": 787.86, "ccy": "USD"}),
    ]}
    got = web._annotations(closed, [])[0]["realized"]
    assert got is not None and got["native"] == 787.86, (
        "the control: a CLOSING event must keep its realised figure"
    )


def test_annotations_carry_the_delta_an_event_changed():
    """What a roll is judged by. The opening event reports None -> x because
    there was no position to have a delta.
    """
    lifecycle = {"events": [
        _event("Short put", _leg(270, "P", "SELL", "O", -3, 5.24)),
        _event("Short put close", _leg(270, "P", "BUY", "C", 3, 2.60),
               at="2026-08-03 09:55:23"),
    ]}
    open_ts = web.epoch_et("2026-07-24 10:35:01")
    close_ts = web.epoch_et("2026-08-03 09:55:23")
    marks = [[open_ts, 0.0, 0.52], [close_ts - 3600, 700.0, 0.32],
             [close_ts, 792.0, 0.0]]
    rows = web._annotations(lifecycle, marks)
    assert (rows[0]["delta_before"], rows[0]["delta_after"]) == (None, 0.52)
    assert (rows[1]["delta_before"], rows[1]["delta_after"]) == (0.32, 0.0)


def test_an_event_without_a_timestamp_is_dropped():
    """It could not be placed on the timeline, and defaulting it to the epoch
    would put it at the far left of every chart as if it happened first.
    """
    lifecycle = {"events": [
        _event("Short put", _leg(270, "P", "SELL", "O", -3, 5.24), at=None),
    ]}
    assert web._annotations(lifecycle, []) == []


def test_a_stat_cards_note_is_a_hoverable_element_not_a_title_attribute():
    """Two failures this excludes, and the second was shipped.

    A visible grey line under every figure is the clutter this removed -- the
    same six words ("closed round trips") appeared on five cards.

    But the first fix used a `title` attribute, and a native tooltip waits one
    to two seconds, is trivially missed and does not exist on a touch device --
    so the note was reported as simply gone. A real element appears instantly,
    stays in the DOM for a screen reader, and is markup this test can read.
    """
    card = _fn("statCard").replace(" ", "").replace("\n", "")
    assert '<divclass="tip">${esc(note)}</div>' in card, (
        "the note no longer reaches a hoverable element"
    )
    assert 'title=' not in card, (
        "back to a native title tooltip, which is the mechanism that failed to "
        "surface the note at all"
    )
    assert "class=\"stat${note?'tipped':''}\"" in card, "no tooltip trigger class"
    assert 'tabindex="0"' in card, (
        "not focusable, so the note is reachable by mouse only"
    )
    assert "class=\"k${note?'hint':''}\"" in card, (
        "no hint class, so a card with a tooltip looks identical to one without"
    )


def test_a_tooltip_is_hidden_until_hovered_or_focused():
    """The note lives in the DOM, so if the reveal rules ever go it renders as a
    visible block on every card -- worse than the clutter it replaced.
    """
    css = _css().replace(" ", "").replace("\n", "")
    assert "visibility:hidden" in css and ".tip{" in css, "the tip is not hidden"
    for trigger in (".tipped:hover>.tip", ".tipped:focus>.tip",
                    ".tipped:focus-within>.tip"):
        assert trigger.replace(" ", "") in css, f"no reveal on {trigger}"
    assert ".tip{position:absolute" in css, (
        "a static tip would change the card's height when shown and shift the grid"
    )


def test_commission_is_tinted_as_a_cost_and_sized_like_its_neighbours():
    """Two separate defects on one card. It is displayed as a magnitude, so an
    untinted figure among green ones reads as unfinished rather than neutral --
    commission is never a gain, so the tint is unconditional. The SIZE is not:
    an unconditional `sm` made it the only card permanently smaller than its
    neighbours, which reads as a different typeface. Long figures shrink, by the
    same length rule Net P&L uses.
    """
    dash = _fn("dashboard").replace(" ", "").replace("\n", "")
    assert "'neg'+(String(moneyOf(s.commissions)).length>10?'sm':'')" in dash, (
        "the Commissions card lost its cost tint or its conditional sizing"
    )
    assert "'smneg'" not in dash, "back to an unconditional small size"


def test_the_open_pill_counts_positions_not_contracts():
    """A strangle is one position holding two contracts. Reading open_episodes
    here made the Dashboard say 5 while the Positions tab showed 3 cards.
    """
    dash = _fn("dashboard").replace(" ", "").replace("\n", "")
    assert ">open<b>${s.open_positions}</b>" in dash, (
        "the open pill is not reading the position count"
    )
    assert ">open<b>${s.open_episodes}</b>" not in dash
