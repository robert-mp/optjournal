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
import socket
import sqlite3
from pathlib import Path

import pytest
from conftest import RAW_DIR, ROOT, code_only

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
from optjournal.web import _origin_is_same, build_state, page_html, serve


@pytest.fixture
def populated(populated_db) -> Path:
    """A journal with every archived statement ingested.

    An alias for the shared `populated_db`, kept because this module names it
    forty times and the indirection costs nothing.
    """
    return populated_db


@pytest.fixture
def state(populated) -> dict:
    """The payload, with a calendar stored.

    Events are seeded rather than fetched -- no test here touches the network --
    but they ARE stored, because the contract guard anchors `MarketEvent` to a
    real payload row and an empty list would quietly exempt that shape.

    Dated relative to now, so the events land inside the week `market_data`
    derives. A fixed date would fall out of the window and stop anchoring the
    shape the moment the calendar rolled over -- the same trap a hard-coded row
    count is, one layer up.

    The watchlist is seeded for the same reason, and with the UNDERLYING of a
    contract the archive actually holds -- otherwise `WatchOption` anchors to
    nothing and the drift test quietly stops covering it. TSLA is a real open
    position in this journal (the LEAP), which is what makes the "your options"
    column non-empty.
    """
    from datetime import UTC, datetime, timedelta

    from optjournal.db import connect
    from optjournal.events import parse_events, store_events

    today = datetime.now(UTC).date()
    conn = connect(populated)
    store_events(conn, parse_events([
        {"title": "Non-Farm Employment Change", "country": "USD",
         "date": f"{today}T08:30:00-04:00", "impact": "High",
         "forecast": "85K", "previous": "57K"},
        {"title": "Bank Holiday", "country": "AUD",
         "date": f"{today + timedelta(days=1)}T17:00:00-04:00",
         "impact": "Holiday", "forecast": "", "previous": ""},
    ]))
    conn.execute(
        "INSERT OR IGNORE INTO watchlist (symbol, note, added_at) VALUES"
        " ('TSLA', 'held: the LEAP', ?), ('SPY', NULL, ?)",
        (str(today), str(today)),
    )
    # A job_state row and its run, for the same reason the events are seeded: the
    # contract guard anchors `JobRow` to a real payload row, and on a journal where
    # no job has ever run -- every fresh one -- the list is empty and the shape
    # would be quietly exempted from the guard rather than checked by it.
    fired = int(datetime.now(UTC).timestamp()) - 3600
    conn.execute(
        "INSERT OR REPLACE INTO job_state"
        " (job, last_fired_for, last_status, consecutive_failures, heartbeat_at)"
        " VALUES ('sync', ?, 'ok', 0, ?)",
        (fired, int(datetime.now(UTC).timestamp())),
    )
    conn.execute(
        "INSERT INTO job_runs (job, fired_for, started_at, finished_at, status,"
        " detail, done, total) VALUES ('sync', ?, ?, ?, 'ok', '2 new trades', 1, 1)",
        (fired, str(today), str(today)),
    )
    conn.commit()
    conn.close()
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


#: The leading ES module import. Dropped from the scanned script rather than
#: stripped by `code_only`, because its quoted path parses as a property read on
#: a binding named `replay` that exists nowhere -- and stripping ALL string
#: literals broke the tests that legitimately assert on them.
_IMPORT = re.compile(r"^\s*import\s*\{[^}]*\}\s*from\s*['\"][^'\"]+['\"];?", re.M)

#: Shared with test_frontend, which needs the same stripping over replay.js. The
#: guards below look for `ident.attr`, and a comment mentioning a dotted
#: expression in passing -- "this used to read from history.open" -- is
#: indistinguishable from a real property access.
_code_only = code_only


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
#: not sent by the API) and the replies of the four endpoints that are NOT
#: /api/state. Those four are pinned against their handlers' source instead --
#: see test_endpoint_reply_shapes_match_what_the_page_reads -- because building a
#: real sample would spend an IBKR request, hit a rate-limited feed, or make one
#: HTTP call per watched symbol.
_UNSAMPLED = frozenset({
    "ChartPoint", "Bucket", "SyncResponse",
    "MarketFetch", "WatchWrite", "QuoteReply", "Quote",
    # `/api/jobs/run`, both verbs. Exempt for the same reason as the rest and one
    # more: its keys are CONDITIONAL on the HTTP status (`jobs` only on 400,
    # `run_id` on 202 and 409, `status` only on the GET), so no single reply
    # carries them all and a sampled one would make four of them look absent.
    # Pinned against the handlers' source below instead.
    "JobReply",
})


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
        "Market": state["market"],
        # The strip is always seven days, so [0] is always real. The event list
        # is empty until the calendar has been fetched, which is why the sampler
        # tolerates None here -- but the fixture DOES store events (see the
        # `market` fixture), so in practice this anchors a real row.
        "MarketDay": first(state["market"]["week"]),
        "MarketEvent": first(state["market"]["events"]),
        # One filter chip. Sampled from `impacts` rather than `countries` because
        # both axes carry the SAME shape, and the impact axis is the one whose
        # order is asserted elsewhere -- so a drift in either is caught here once.
        # Non-empty for the same reason MarketEvent is: the fixture stores events,
        # and a facet list is built from what those events carry.
        "MarketFacet": first(state["market"]["impacts"]),
        "Watch": first(state["watchlist"]),
        # A watched symbol that actually HOLDS an option, so WatchOption is
        # anchored to a real row. Sampling the first watch row would anchor it to
        # nothing whenever the first symbol alphabetically happens to be unheld.
        "WatchOption": first([
            option for row in state["watchlist"] for option in row["options"]
        ]),
        "Scheduler": state["scheduler"],
        # Sampled from the real payload, and the `scheduler` fixture seeds a
        # job_state row so this anchors something rather than being None on a
        # journal where no job has ever run -- which is every fresh journal, and
        # would quietly exempt the shape.
        "JobRow": first(state["scheduler"]["jobs"]),
        # The nested ledger row. Anchored to a REAL run rather than exempted,
        # because the strip reads four of its keys and an untyped `Object` (what
        # `last_run` was) exempts every one of them -- so a rename in `job_runs`
        # would blank the cells silently. The fixture's job_state row has a
        # matching job_runs row for exactly this.
        "JobRun": first([
            row["last_run"] for row in state["scheduler"]["jobs"] if row["last_run"]
        ]),
        "Audit": state["audit"],
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
    # No suppression list. There used to be one -- 39 DOM, promise and builtin
    # attribute names -- left over from before the contract moved into the page.
    # It suppressed nothing: the `@payload`/`@local` table now decides which
    # bindings are scanned at all, so a DOM node's binding is classified `@local`
    # and never reaches this loop. Keeping it was actively unsafe, because `ok`
    # and `status` are BOTH in that list and real declared payload keys, so a
    # typo on either would have passed silently -- exactly the bug class this
    # test exists to catch.
    missing = []
    for var, shape in bindings.items():
        declared = shapes.get(shape, {})
        for match in re.finditer(rf"(?<![\w.]){re.escape(var)}\.([a-z_][a-z0-9_]*)\b", js):
            attr = match.group(1)
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


def test_a_month_outside_the_account_life_falls_back_to_all_time(populated):
    """A hand-edited `#month=1999-01` must not render a calendar of nothing.

    Distinct from `test_a_fill_free_month_is_an_honest_zero`: a month INSIDE the
    account's life with no fills is selectable and shows zeros, while one outside
    it heals to all-time. Both directions of `month_range` matter, which is why
    each has a test.
    """
    state = build_state(
        db_path=populated, archive_dir=RAW_DIR, query_id=None, month="1999-01"
    )
    assert state["selected_month"] is None
    assert state["stats"]["month"] == "ALL"


def test_sync_response_shape_matches_what_the_page_reads():
    """Pin the /api/sync contract, which has no fixture to check against.

    Reads BOTH functions on the path, because the reply is assembled by two: the
    success keys come from `sync_journal` (the one sync path, shared with
    `optjournal sync` and the `sync` job) and the refusal shapes from `_do_sync`,
    which exists precisely to turn its two typed exceptions into an HTTP body.
    Reading only the endpoint stopped covering the success keys the moment they
    moved -- caught here, which is the whole reason both are named.
    """
    import inspect

    from optjournal.web import (  # noqa: PLC0415 - private by design
        _do_sync,
        sync_journal,
    )
    src = inspect.getsource(sync_journal) + inspect.getsource(_do_sync)
    for key in ("new_trades", "new_cash", "reused_archive", "warnings", "kind", "ok"):
        assert f'"{key}"' in src, f"/api/sync no longer returns {key!r}"
    # The refusal shapes are the endpoint's own, and the page renders a countdown
    # off `retry_after_s` as a number.
    endpoint = inspect.getsource(_do_sync)
    for key in ("cooldown", "retry_after_s", "config"):
        assert f'"{key}"' in endpoint, (
            f"/api/sync no longer distinguishes {key!r}, so the page cannot tell a "
            "cooldown from a real failure"
        )


def test_endpoint_reply_shapes_match_what_the_page_reads():
    """The same pin, for the endpoints that are not /api/state.

    These have no payload sample for a reason rather than by neglect: refreshing
    the calendar hits a feed that answers 429, and a quote costs one HTTP request
    per watched symbol. So the keys are checked against the handlers' SOURCE,
    which is what `_UNSAMPLED` trades away the fixture for.

    Crude, and it catches the failure that matters: a handler renaming a key while
    the page keeps reading the old one renders a blank toast rather than raising,
    because JavaScript reading a missing property yields undefined. That is the
    exact defect this whole contract exists for.
    """
    import inspect

    from optjournal.web import _Handler  # noqa: PLC0415 - private by design

    expected = {
        _Handler._market_fetch: ("ok", "kind", "fetched", "stored", "message"),
        _Handler._watchlist_write: ("ok", "kind", "action", "symbol", "changed",
                                    "message"),
        _Handler._quotes: ("ok", "quotes", "failed", "asked_at",
                           # one Quote entry, read per row in the watchlist
                           "price", "at", "previous_close", "currency"),
        # Both verbs of /api/jobs/run. Its keys are conditional on the status --
        # `jobs` only on 400, `run_id` on 202 and 409 -- so a sampled reply would
        # make four of them look absent, which is why it is source-pinned.
        _Handler._job_run: ("ok", "kind", "message", "jobs", "run_id", "job"),
        _Handler._job_status: ("ok", "kind", "message"),
    }
    for handler, keys in expected.items():
        src = inspect.getsource(handler)
        for key in keys:
            assert f'"{key}"' in src, (
                f"{handler.__name__} no longer returns {key!r}, which the page "
                f"reads -- the cell or toast would render blank"
            )


def test_page_loads_no_external_resources():
    """Offline by construction, and the CSP header assumes it.

    "No external resources" has always meant NOTHING OFF-ORIGIN rather than
    nothing linked -- the favicon `<link>` predates the stylesheet and passes this
    unchanged. Same-origin `/static/` assets are exactly what the CSP's
    `default-src 'self'` permits.
    """
    page = page_html()
    assert not re.search(r'(src|href)="https?://', page)
    assert "cdn." not in page
    # Every local reference must be root-relative, so the page cannot depend on
    # which URL it was loaded from.
    for ref in re.findall(r'(?:src|href)="([^"]+)"', page):
        assert ref.startswith("/"), f"{ref} is not a root-relative local path"


@pytest.mark.parametrize("ref", ["/static/app.css", "/static/mark.svg"])
def test_every_asset_the_page_links_is_actually_servable(ref):
    """A `<link>` the server will not serve renders an unstyled page, silently.

    Nothing covered `/static/` before this: the CSS lived inline, the favicon was
    the only asset, and `STATIC_TYPES` is an ALLOWLIST -- so an extension missing
    from it 404s rather than being guessed. Extracting the stylesheet made that a
    live hazard, because `nosniff` means a wrong or absent Content-Type has the
    browser drop the file rather than sniff it, and the page then loads with no
    rules at all while every Python test still passes.

    Checks three things that can each break independently: the page references it,
    the file exists on disk, and its extension is one the handler will answer for.
    """
    from optjournal.web import STATIC_TYPES  # noqa: PLC0415 - local to this test

    assert ref in page_html(), f"the page no longer links {ref}"
    asset = ROOT / "src" / "optjournal" / ref.removeprefix("/")
    assert asset.is_file(), f"{ref} is linked but not on disk"
    assert asset.suffix in STATIC_TYPES, (
        f"{asset.suffix} is not in STATIC_TYPES, so /static/ will 404 it"
    )


def test_the_stylesheet_is_served_and_its_rules_reach_the_page():
    """End to end through a real server, because the Python half cannot see this.

    Every layout assertion in this file reads `static/app.css` off disk. That
    proves the RULES are right and says nothing about whether the browser ever
    receives them -- a missing STATIC_TYPES entry, a typo'd href or a stray
    `<style>` left behind would all leave those tests green.

    Verified in a real browser when the extraction shipped: 222 rules in the
    CSSOM, zero `<style>` tags, and computed styles applied (body background
    rgb(10, 8, 6), card radius 14px). This is the automated half of that.
    """
    import urllib.request  # noqa: PLC0415 - local to this test

    with web.serve_ephemeral(db_path=DEFAULT_DEMO_DB, archive_dir=DEFAULT_DEMO_DIR) as base:
        with urllib.request.urlopen(f"{base}/static/app.css", timeout=10) as resp:  # noqa: S310
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/css"), (
                "a wrong Content-Type plus nosniff means the browser drops it"
            )
            body = resp.read().decode()

    assert ".card{" in body.replace(" ", ""), "the served file is not the stylesheet"
    assert "<style>" not in page_html(), "a stylesheet was left embedded in the page"


def test_no_element_carries_two_class_attributes():
    """HTML keeps the FIRST `class` and silently drops the rest.

    Written because I did exactly this while converting the 20 `style="..."`
    attributes to classes: an element that already had `class="v mono ${...}"`
    gained a second `class="big"`, so the 22px headline figure quietly rendered at
    the inherited size. The suite passed, the page looked almost right, and only a
    computed-style check in a browser found it.

    The failure mode is the dangerous kind -- no error, no warning, just a rule
    that never applies. One regex is cheaper than noticing by eye.
    """
    duplicates = [
        tag.group(0).replace("\n", " ")[:90]
        for tag in re.finditer(r"<[a-zA-Z][^>]*>", page_html(), re.S)
        if tag.group(0).count("class=") > 1
    ]
    assert not duplicates, (
        f"these elements declare `class` twice, so all but the first are ignored: "
        f"{duplicates}"
    )


def test_styling_lives_in_the_stylesheet_not_in_the_markup():
    """No `style="..."` attributes, so every rule is somewhere a test can read it.

    The layout assertions in this file reach CSS through `_css()`, which reads
    `static/app.css`. An inline attribute is invisible to them -- and four real
    layout defects have already shipped in CSS that nothing looked at, which is why
    that section exists at all.

    All 20 attributes were converted. Four were DEAD: `.stats` is `display:flex`
    (app.css), so `grid-template-columns` on it never did anything -- confirmed in a
    browser, `INERT: true` on the computed value. Those were deleted rather than
    translated, including the one dynamic declaration
    (`repeat(${jrows.length},1fr)`), which means nothing here needs an escape
    hatch for a computed value today. If one is ever genuinely needed, add it with
    a comment saying why and this test will need an allowlist -- deliberately not
    pre-built, because an unused exemption invites use.
    """
    attrs = re.findall(r'style="([^"]*)"', page_html())
    assert not attrs, (
        f"styling belongs in static/app.css, where the layout tests can see it: "
        f"{attrs}"
    )


# --------------------------------------------------------------------------
# The class contract: the page and the stylesheet name the same things.
#
# Extracting the CSS into its own file bought reachability -- the layout tests
# can read every rule -- and cost co-location. A class and its rules now live in
# two files that nothing checks against each other, which is the same shape as
# the payload contract below, and the same shape as the defect that prompted
# this: `.stats` was `display:grid` with four inline `grid-template-columns`
# overrides, commit 01b34ff changed it to `display:flex` and left the overrides
# behind, and they were inert from that moment with the whole suite green.
#
# Two directions, both strict. Measured before writing them: 168 classes in
# app.css, 148 literal in page.html, exactly ONE used-but-undefined (`tbl`, on
# the watchlist table, since deleted) and ZERO unreachable rules. Neither
# direction needs an allowlist today, so neither has one.
# --------------------------------------------------------------------------


def _css_classes() -> set[str]:
    """Every class named by a selector in the stylesheet, comments stripped.

    Comments matter: app.css explains its own layout decisions, and prose about
    `.stats` would otherwise read as a definition of it.
    """
    return set(re.findall(r"\.([A-Za-z][A-Za-z0-9_-]*)",
                          re.sub(r"/\*.*?\*/", "", _css(), flags=re.S)))


def _literal_page_classes() -> set[str]:
    """Class tokens the page states outright, with `${...}` blanked out.

    Blanked rather than parsed: an interpolation is a JS expression whose value
    this cannot know, so a token that only ever arrives through one is checked by
    the other direction instead. What is left is every class written down as
    text, which is where a typo lands.
    """
    tokens: set[str] = set()
    for attr in re.finditer(r'class="([^"]*)"', page_html()):
        plain = re.sub(r"\$\{[^{}]*\}", " ", attr.group(1))
        tokens |= set(plain.split())
    return tokens


def test_every_class_the_page_writes_down_has_rules():
    """A class with no rules is a silent no-op, and one had already shipped.

    `<table class="tbl">` on the watchlist carried a class app.css never defined
    -- zero `.tbl` rules -- and rendered acceptably only because the bare `table`
    rule covers it. Nothing noticed for a week. The class is deleted now; this
    stops the next one.

    Not merely a typo check. It is what makes a RENAME safe: change a selector in
    app.css and this names every element still asking for the old one, which is
    precisely the step commit 01b34ff skipped.
    """
    orphans = sorted(_literal_page_classes() - _css_classes())
    assert not orphans, (
        f"the page asks for these classes and static/app.css defines none of them, "
        f"so they style nothing: {orphans}"
    )


def test_every_rule_in_the_stylesheet_is_reachable_from_the_page():
    """The other direction, so deleting an element cannot leave rules behind.

    Dead rules are not merely clutter: they are what a reader trusts when
    deciding what a class does, and they hide the fact that the element is gone.

    Reachability, not literal use -- roughly a fifth of the classes are assembled
    in JS (`'sm '+cls(x)`, `classList.add('busy')`, `${shown?' open':''}`), so a
    literal-only check would fail on 21 rules that are all live. Whole-word match
    anywhere in the page is the weakest rule that admits those, and it is
    deliberately weak: it proves the NAME is written somewhere, not that the code
    path runs. It still caught what it needed to -- with word boundaries off,
    `.g`/`.lo`/`.hi` matched inside `logo` and `hidden` and the check was
    vacuous.
    """
    page = page_html()
    unreachable = sorted(
        cls for cls in _css_classes()
        if not re.search(rf"(?<![A-Za-z0-9_-]){re.escape(cls)}(?![A-Za-z0-9_-])", page)
    )
    assert not unreachable, (
        f"static/app.css defines these and the page never names them, so they are "
        f"dead: {unreachable}"
    )


def _css_rules() -> list[tuple[str, str]]:
    """(selector, body) for every rule, comments stripped, at-rules flattened.

    `@media(...){.stats{...}}` yields the inner rule, which is what wants
    checking -- a responsive override is exactly where a stale property hides.
    """
    return re.findall(r"([^{}]+)\{([^{}]*)\}",
                      re.sub(r"/\*.*?\*/", "", _css(), flags=re.S))


def test_no_rule_sets_a_layout_property_its_display_mode_cannot_use():
    """THE mechanism behind the dead-CSS finding, checked directly.

    `grid-template-columns` on a `display:flex` box is not an error, not a
    warning, and not visible: the property parses, sits in the CSSOM, and does
    nothing. That is how commit 01b34ff shipped four inert declarations -- it
    changed `.stats` from `display:grid;grid-template-columns:repeat(5,1fr)` to
    `display:flex;flex-wrap:wrap;--sw:18%` and left four inline
    `grid-template-columns` overrides pointing at a box that had stopped being a
    grid. The suite was green, the page looked right, and the overrides were dead
    from that commit until a browser check found them a day later.

    Ablated by re-creating that exact rule (`.stats.s4{grid-template-columns:
    repeat(4,1fr)}` beside the flex `.stats`): this reports it, and names `flex`
    as the mode the element actually has.

    Deliberately narrow. Only properties with NO meaning outside their mode are
    listed -- `gap`, `align-items` and `justify-content` work in both and are
    absent on purpose. A selector's display mode may be set by any rule sharing
    one of its classes, because that is how `.filters` and `.leg.ctx`
    legitimately set grid properties: their base rules declare `display:grid` in
    this same file, which is the co-location the inline overrides lacked.
    """
    grid_only = ("grid-template-columns", "grid-template-rows", "grid-template-areas",
                 "grid-auto-flow", "grid-column", "grid-row", "grid-area")
    flex_only = ("flex-wrap", "flex-direction", "flex-basis", "flex-grow", "flex-shrink")

    def classes(sel: str) -> set[str]:
        return set(re.findall(r"\.([A-Za-z][A-Za-z0-9_-]*)", sel))

    rules = _css_rules()
    modes: dict[str, set[frozenset[str]]] = {"grid": set(), "flex": set()}
    for sel, body in rules:
        found = re.search(r"display:\s*([a-z-]+)", body)
        if not found:
            continue
        for mode in modes:
            if mode in found.group(1):
                modes[mode] |= {frozenset(classes(s)) for s in sel.split(",")}

    def has_mode(sel: str, mode: str) -> bool:
        want = classes(sel)
        # An element or id selector carries no class to match on; not checked.
        return not want or any(want & set(m) for m in modes[mode])

    orphans = []
    for sel, body in rules:
        for one in (s.strip() for s in sel.split(",")):
            for mode, props in (("grid", grid_only), ("flex", flex_only)):
                used = [p for p in props if re.search(rf"(?<![-a-z]){p}\s*:", body)]
                if used and not has_mode(one, mode):
                    actual = next(
                        (m for m in modes if m != mode and has_mode(one, m)), "neither"
                    )
                    orphans.append(f"{one} sets {used} but is {actual}, not {mode}")

    assert not orphans, (
        "these declarations are inert -- the property means nothing in the display "
        f"mode the element actually has: {orphans}"
    )


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
def test_serve_refuses_non_loopback(host, tmp_path, monkeypatch):
    """The page has no auth and exposes an entire account. Loopback or nothing.

    FAILS FAST, and that is the whole design of this test rather than a detail. If
    the guard is ever removed, `serve` gets past the raise and BINDS, and a test
    that expected an exception instead serves forever: it does not fail, it hangs.
    A hanging sentinel is worse than a missing one, because the suite reports
    nothing rather than something -- the mutation survey called this exact guard
    `UNCAUGHT` for that reason, so the one test standing between an unauthenticated
    brokerage dashboard and a public interface read as untested.

    So the bind is made impossible instead of merely cheap: `socket.socket` is
    replaced for the duration, and reaching it is itself the failure. `port=0` is
    kept as the second layer -- if some future path binds without going through
    this name, the OS picks an ephemeral port rather than 8765, the port a
    developer is about to use. (Found the honest way: a leftover pytest from a
    survey was still holding 8765 a day later.) `mutate` now reports a killed
    suite as `hung` rather than as a measured zero, which is the third layer.
    """
    def _refuse(*a, **k):
        raise AssertionError(
            "serve() tried to open a socket for a non-loopback host: the guard "
            "did not refuse, so this would have bound a public interface"
        )

    monkeypatch.setattr(socket, "socket", _refuse)
    with pytest.raises(ValueError, match="Loopback only"):
        serve(db_path=tmp_path / "x.db", archive_dir=tmp_path, host=host, port=0)


# --- cross-origin writes --------------------------------------------------


@pytest.mark.parametrize("origin", [
    "https://evil.example",
    "http://evil.example:8765",
    # A prefix comparison would pass this. The hostname is `127.0.0.1.evil.com`,
    # which resolves to whatever that domain's owner wants.
    "http://127.0.0.1.evil.com",
    "http://localhost.evil.com",
    "null",                       # a sandboxed iframe or a file:// page
    # THE PORT. These were the hole in the first version of the guard, which
    # compared only the hostname -- see the docstring below.
    "http://127.0.0.1:8799",
    "http://localhost:8799",
    "http://127.0.0.1",           # port 80: a different origin from 8765
])
def test_a_cross_origin_post_is_refused(origin):
    """Binding loopback stops the network, not your own browser.

    The journal has no authentication, and its POST endpoint SPENDS AN IBKR
    REQUEST against a lockout budget. So any page open in a tab could push you
    toward a lockout while the journal is running. Not theorised: a POST carrying
    `Origin: https://evil.example` ran a real sync against the live server and
    moved `last_fetch`, which is how this was found.

    THE PORT CASES ARE THE INTERESTING ONES, and they exist because the first
    version of this guard let them through. It checked that the origin's hostname
    was loopback, which sounds right and is not: an attacker page served on
    `http://127.0.0.1:8799` POSTed to the journal on another port and got past it,
    demonstrated in a real browser. Anything able to serve one file locally -- a
    dev server, `python -m http.server` in a downloads folder, another tool's UI
    -- could then spend the request budget. Two ports are two origins, which is
    what the browser's own same-origin rule says, and the reasoning that skipped
    the port is exactly the reasoning a reviewer would nod along with.

    Parametrised over every shape a plausible-but-wrong implementation would
    accept, rather than one hostile origin: `127.0.0.1.evil.com` defeats a prefix
    test, and `127.0.0.1:8799` defeats a hostname-only test.
    """
    assert not _origin_is_same(origin, host="127.0.0.1", port=8765), (
        f"{origin} may not write to this journal"
    )


@pytest.mark.parametrize("origin", [
    "http://127.0.0.1:8765",
    # The SAME server under its other names. A browser sends whichever was typed,
    # so string equality against the bound host would refuse the page its own
    # journal served -- which is why both sides go through `_is_loopback`.
    "http://localhost:8765",
    "http://[::1]:8765",
    None,        # curl, the CLI: not a browser, so not the threat model
])
def test_the_pages_own_origin_may_write(origin):
    """The other direction, and the reason `None` is allowed.

    A guard that refuses the page's own POSTs would break the Sync button, which
    is worse than useless -- it would be a security fix that removes a feature and
    teaches you to disable it. Verified in a real browser before relying on it: a
    same-origin `fetch(..., {method:'POST'})` sends `Origin: http://127.0.0.1:<port>`
    and `Sec-Fetch-Site: same-origin`, so a MISSING Origin means the caller is not
    a browser at all. Confirmed end to end afterwards: the real page's Sync POST
    returned 400 (no query id configured), not 403, so it passed the guard.
    """
    assert _origin_is_same(origin, host="127.0.0.1", port=8765)


def test_the_origin_guard_runs_before_every_route_so_new_endpoints_inherit_it():
    """Ordering, asserted over the source: the check precedes EVERY route match.

    If the guard sat inside one branch, the next write endpoint would ship
    unguarded unless someone remembered -- and the person adding an endpoint is
    thinking about the feature, not about a page in another tab. Cheap to get
    right once; invisible when got wrong.

    Checks against the FIRST path comparison rather than against `/api/sync`
    specifically. The original version pinned `/api/sync`, and two endpoints were
    then added that route BEFORE it -- so the assertion still passed while proving
    nothing about them. A guard test that only covers the oldest route is the
    shape of test that rots silently as the file grows.
    """
    body = code_only((ROOT / "src" / "optjournal" / "web.py").read_text())
    post = body[body.index("def do_POST"):]
    guard = post.index("_same_origin")
    routes = [m.start() for m in re.finditer(r'path (?:==|!=) "', post)]
    assert routes, "do_POST no longer compares a path; this test needs rewriting"
    assert guard < min(routes), (
        "the Origin check must come before the FIRST path comparison in do_POST, "
        "so every write endpoint -- including ones added later -- is covered by "
        "default rather than by remembering"
    )


def _post(base: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """A POST through a real server, so the routing and guard are exercised."""
    import urllib.error  # noqa: PLC0415 - local to this helper
    import urllib.request  # noqa: PLC0415

    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{base}{path}", method="POST", data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.mark.parametrize(("body", "kind"), [
    ({"symbol": "../etc/passwd"}, "symbol"),
    ({"symbol": ""}, "symbol"),
    ({"symbol": "A" * 25}, "symbol"),
    ({"symbol": "SPY DROP TABLE"}, "symbol"),
    ({"symbol": "SPY", "action": "drop"}, "action"),
    ({}, "symbol"),
])
def test_the_watchlist_endpoint_refuses_a_bad_request(populated, body, kind):
    """A user-input table reached over HTTP, so the input is not trusted.

    `watchlist` is the only table in this journal written from typed input rather
    than from a broker statement, and now it is written from a browser. The pattern
    accepts letters, digits, dot and dash -- enough for BRK.B and a foreign
    listing, and not enough for a path, a space-separated injection or a quote.

    Deliberately NOT validated against a ticker universe: this journal has no such
    list, and inventing one would reject a legitimate listing. A symbol that does
    not exist stores a row with no bars, which the tab renders as a dash.
    """
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        status, payload = _post(base, "/api/watchlist", body)
    assert status == 400
    assert payload["kind"] == kind
    assert payload["ok"] is False


def test_the_watchlist_endpoint_round_trips_a_symbol(populated):
    """Add, re-add, remove, remove again -- through the real server.

    Four assertions in one test because they are one behaviour: the SEQUENCE is
    what matters. Re-adding must not blank a note (COALESCE, matching
    `optjournal watch`), and removing what is absent must report `changed: 0`
    rather than claiming a removal, so the page can say "was not on the list"
    instead of "stopped watching" something it never watched.
    """
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        # Lower case in, upper case stored: the page shows what the DB holds.
        status, added = _post(base, "/api/watchlist",
                              {"symbol": "amd", "note": "a note"})
        assert (status, added["symbol"], added["action"]) == (200, "AMD", "add")

        _, again = _post(base, "/api/watchlist", {"symbol": "AMD"})
        assert again["ok"] is True

        _, removed = _post(base, "/api/watchlist",
                           {"symbol": "AMD", "action": "remove"})
        assert removed["changed"] == 1

        _, absent = _post(base, "/api/watchlist",
                          {"symbol": "AMD", "action": "remove"})
        assert absent["changed"] == 0, (
            "removing an absent symbol must report 0 rather than claiming a "
            "removal, or the page reports one that did not happen"
        )

    conn = connect(populated)
    note = conn.execute(
        "SELECT note FROM watchlist WHERE symbol = 'AMD'").fetchone()
    conn.close()
    assert note is None, "the row should be gone"


def test_a_re_add_keeps_the_existing_note(populated):
    """COALESCE, asserted: a note is typed by hand and has no other source."""
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        _post(base, "/api/watchlist", {"symbol": "AMD", "note": "keep me"})
        _post(base, "/api/watchlist", {"symbol": "AMD"})
        conn = connect(populated)
        note = conn.execute(
            "SELECT note FROM watchlist WHERE symbol = 'AMD'").fetchone()["note"]
        conn.close()
    assert note == "keep me", "a bare re-add blanked the note"


def test_an_oversized_body_is_refused_rather_than_read(populated):
    """`rfile.read` on a client-chosen Content-Length is an unbounded allocation.

    No framework here does this for us, so the cap is explicit. 8KB is four orders
    of magnitude more than any body this server accepts.
    """
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        status, payload = _post(base, "/api/watchlist",
                                {"symbol": "SPY", "note": "x" * 9000})
    # The body is dropped, so the request looks empty and fails the symbol check.
    assert status == 400
    assert payload["kind"] == "symbol"


def test_a_render_preserves_what_the_user_was_typing():
    """The page re-renders by replacing innerHTML, which destroys live inputs.

    Verified in a real browser before the fix: typing `AAP` into the watchlist's
    add field and then triggering any redraw discarded the text, the focus AND the
    cursor position. The trigger is not exotic -- `loadQuotes()` calls `draw()` on
    its own when the tab opens, so a quote landing mid-keystroke ate the input.

    Asserted over the source because the behaviour is DOM-dependent: `node --test`
    cannot reach it (that is why replay.js is kept free of `document`), and the
    project has no browser test runner. So this pins the three things that make the
    fix work, and the browser check is recorded in the commit rather than automated:

    * the capture happens BEFORE innerHTML is replaced,
    * the restore happens AFTER,
    * and the helpers are generic over `#body input` rather than hard-coding
      `#wadd`, so the next input inherits the fix instead of rediscovering the bug.
    """
    js = code_only(_js())
    capture = js.index("preserveInputs()")
    render = js.index("$('#body').innerHTML=")
    restore = js.index("restoreInputs(")
    assert capture < render < restore, (
        "the input capture must straddle the innerHTML replacement, or typing is "
        "lost on every redraw"
    )
    helper = js[js.index("function preserveInputs"):]
    assert "#body input" in helper, (
        "preserveInputs must scan every input, not one known id -- a fix that only "
        "covers #wadd leaves the next field broken"
    )
    assert "selectionStart" in helper and "setSelectionRange" in helper, (
        "the CURSOR has to survive too: restoring the value but not the caret "
        "jumps the cursor to the start mid-word"
    )


def test_nothing_this_server_sends_is_cacheable():
    """A cached page is a stale page, and a cached payload is a stale account.

    The page is read from disk per request precisely so an edit takes effect, and
    the payload is a live brokerage position list. Neither may be reused.

    Sent because the ABSENCE bit: with no cache headers at all, a browser may keep
    the old page indefinitely. That happened while fixing the input bug -- the
    server was serving corrected JS to a tab still running the previous copy, and
    the fix appeared not to work. Everything here is loopback and a few KB, so
    there is no bandwidth being saved by caching.
    """
    import inspect

    from optjournal.web import _Handler  # noqa: PLC0415 - private by design

    src = inspect.getsource(_Handler._send)
    assert "Cache-Control" in src, "responses no longer forbid caching"
    assert "no-store" in src


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


def test_a_snapshot_leg_keeps_the_sign_of_the_position_it_seeds():
    """A short snapshot-only contract must seed a NEGATIVE quantity.

    Found by mutation: wrapping `seed_quantity` in `abs()` passed all 579 tests.
    Neither journal reaches it -- every short position they hold is claimed by an
    open lifecycle, so it never takes the `_snapshot_leg` path -- but the path is
    reachable the moment a short is held with no fills in the archive, which is
    exactly what the LEAP was before it was traded.

    The sign is the whole meaning of the row. It decides the strike's side (a
    level you are defending versus one you paid for) and it flips the modelled
    P&L: a short position gains as the option decays, so `abs()` would draw the
    curve upside down and label the strike 'long'.

    A dict rather than a database row because `_snapshot_leg` reads a mapping,
    and the property is about the sign, not about SQL.
    """
    from optjournal.web import _snapshot_leg, _strikes_of

    short = {"conid": "C1", "strike": 270.0, "put_call": "P", "expiry": "20260904",
             "multiplier": 100.0, "position": -5, "cost_basis_price": 3.20}
    leg = _snapshot_leg(short)
    assert leg.seed_quantity == -5.0, "the short sign was discarded"
    assert _strikes_of([leg])[0]["side"] == "short"

    long_ = dict(short, position=2)
    assert _snapshot_leg(long_).seed_quantity == 2.0
    assert _strikes_of([_snapshot_leg(long_)])[0]["side"] == "long"


def test_a_closed_contract_takes_its_side_from_the_OPENING_fill():
    """A round trip holds the same strike twice, so which fill decides is the bug.

    Sold to open then bought to close: the closing fill is a BUY, so reading
    side off the last fill labels every short you sold as a long you bought --
    and the chart's whole point is that difference (a level you are defending
    versus one you paid for). Every closed position in a journal inverts at once,
    which paradoxically makes it harder to notice: nothing looks inconsistent.

    Two fills with opposite signs, because a single-fill leg cannot tell the two
    readings apart -- and that is why the existing snapshot test above, whose leg
    has no fills at all, does not cover this.
    """
    from optjournal.bars import ReplayLeg
    from optjournal.web import _strikes_of

    sold_to_open = ReplayLeg(
        conid="C1", strike=105.0, right="P", expiry="20260904",
        fills=((1_000, -3.0, 2.50), (2_000, 3.0, 0.40)),
    )
    row = _strikes_of([sold_to_open])[0]
    assert row["side"] == "short", "side was read off the closing fill"
    # The window is the other half of the meaning: the segment must END where
    # the contract went flat, not run to the right edge as if still held.
    assert row["frm"] == 1_000 and row["to"] == 2_000

    bought_to_open = ReplayLeg(
        conid="C2", strike=580.0, right="C", expiry="20260904",
        fills=((1_000, 4.0, 1.10), (2_000, -4.0, 3.30)),
    )
    assert _strikes_of([bought_to_open])[0]["side"] == "long"


def test_the_segment_ends_where_the_position_goes_flat_not_at_the_last_fill():
    """A partial close leaves the contract held, so the segment stays open.

    The running position is what distinguishes the two: sold 3, bought back 1,
    and the contract is still short 2 -- so `to` must be None (drawn to the right
    edge) rather than the timestamp of that second fill. Reading the last fill
    instead would retire a live strike from the chart.
    """
    from optjournal.bars import ReplayLeg
    from optjournal.web import _strikes_of

    partly_closed = ReplayLeg(
        conid="C3", strike=590.0, right="C", expiry="20260904",
        fills=((1_000, -3.0, 2.50), (2_000, 1.0, 1.20)),
    )
    row = _strikes_of([partly_closed])[0]
    assert row["side"] == "short"
    assert row["to"] is None, "a partial close retired a strike that is still held"


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


def test_the_basis_comes_from_the_position_row_and_needs_no_fallback(populated):
    """`positions_data` needs no help from `history`, and pinning that is the point.

    There used to be a `cost_basis_fallback` argument here -- a snapshot row
    without a basis borrowing the open episode's, matched on conid -- and it
    could never fire, because both sides read the SAME COLUMN of the same table
    for the same report_date. It was removed; this is the guard that keeps it
    removed, by asserting the property that made it dead rather than by trusting
    a comment.

    Read straight from the database with no history pass at all: if a row is ever
    genuinely missing a basis, this fails and says so, which is the signal that
    would justify a real fallback (from somewhere other than the same column).
    """
    from optjournal.db import connect
    from optjournal.serialize import positions_data

    conn = connect(populated)
    rows = conn.execute("SELECT * FROM current_option_positions").fetchall()
    assert rows, "the archive should hold open option positions"
    assert all(r["cost_basis_money"] is not None for r in rows), (
        "a snapshot row has no basis, so a fallback would now be earning its "
        "place -- but not from Episode.cost_basis, which reads this same column"
    )
    # And the payload is derived from it, not merely adjacent to it. Matched on
    # conid rather than by position, since positions_data sorts by expiry/strike.
    basis_of = {str(r["conid"]): r["cost_basis_money"] for r in rows}
    payload = positions_data(conn)
    assert len(payload) == len(rows)
    for pos in payload:
        assert pos["cost_basis"]["native"] == basis_of[str(pos["conid"])]


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


def test_a_tab_from_the_hash_is_validated_against_the_tabs_that_exist():
    """An unknown id in the URL must not render an empty tab.

    And an ABSENT key must reset to the default rather than leave the current
    tab standing: the back button lands on entries whose hash has no tab key,
    and keeping the old tab made syncHash rewrite it into the entry just
    navigated to -- the back button appeared to do nothing.
    """
    js = _code_only(_js())
    assert "HASH_TABS().includes(tab)" in js
    assert re.search(r"S\.tab=\(tab&&HASH_TABS\(\)\.includes\(tab\)\)\?tab:'dashboard'", js)
    # DERIVED from TABS, whatever the derivation -- that is the invariant, so the
    # list cannot drift from the tab bar as tabs are added. It used to be pinned
    # as `TABS.filter`, over a third "disabled reason" slot that was null in every
    # entry; pinning the spelling made removing the dead slot fail a test about
    # hash routing, which is not what this test is for.
    assert re.search(r"HASH_TABS\s*=\s*\(\)\s*=>\s*TABS\b", js), \
        "HASH_TABS must be derived from TABS, not restate the ids"
    # Every tab in the bar is reachable by hash, and nothing else is.
    tabs = re.search(r"const TABS=\[(.*?)\n\];", _js(), re.S)
    declared = re.findall(r"\['([a-z0-9]+)'", tabs.group(1))
    assert declared, "no tab ids parsed"
    assert len(declared) == len(set(declared)), "a duplicate tab id would shadow"


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
    """The stylesheet, wherever it lives.

    Reads `static/app.css` now that the rules have moved out of page.html's
    `<style>` block. Deliberately still ONE helper: thirteen layout assertions call
    it, and they exist because four real defects lived in CSS that nothing had ever
    read -- so the extraction had to keep the rules reachable through a single seam
    rather than through thirteen splits on markup.
    """
    return (ROOT / "src" / "optjournal" / "static" / "app.css").read_text()


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
    # Both icons in the row wrapper, or they stack again. Searched in the whole
    # page rather than after `</style>`: that split existed only to skip PAST the
    # embedded stylesheet, and the rules now live in static/app.css, so there is
    # no CSS left in here for a marker string to collide with.
    icons = page_html().split('class="hdr-icons"')[1].split("</div>")[0]
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

    The two tables reach it through `statsRow`, which is a further step in the
    same direction -- they no longer spell the row out at all, so they cannot
    spell it differently. What matters is that no surface computes the figure its
    own way, which is what the negative assertion below pins.
    """
    js = _code_only(_js()).replace(" ", "").replace("\n", "")
    assert "constchargeOf=(nat,ccy,base)=>isNativeCharge(nat,ccy)" in js, \
        "the shared charge helper is gone"
    # The Dashboard card reads the payload key directly; both tables render
    # through `statsRow`, which is the only place a stats row's cells exist.
    assert "moneyOf(s.commissions)" in _fn("dashboard").replace(" ", ""), \
        "the dashboard card does not use the shared helper"
    row = _fn("statsRow").replace(" ", "").replace("\n", "")
    assert "moneyOf(s.commissions)" in row, \
        "statsRow does not use the shared helper, so both tables bypass it"
    for fn in ("dashboard", "annual", "monthlyTable", "statsRow"):
        body = _fn(fn).replace(" ", "").replace("\n", "")
        assert "cash(Math.abs(s.commissions.base))" not in body, \
            f"{fn} bypasses the helper and can drift from the others"
    # Neither table may re-derive a commission cell of its own: one row renderer
    # is the whole point, and a second `<td>` carrying commission would be a
    # second answer.
    for fn in ("annual", "monthlyTable"):
        assert "commissions" not in _fn(fn), \
            f"{fn} reads commissions directly again instead of through statsRow"
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


def test_a_gain_carries_a_sign_glyph_and_not_only_a_hue():
    """Colour alone does not survive red-green colour blindness.

    Measured rather than assumed: --ok against --bad separates by ΔE 2.4 under
    deuteranopia, so a gain and a cost rendered in the two of them are the same
    colour to roughly 8% of men. Simulated over the real page, "Net P&L
    +€1,451.99" and "Commissions €9.72" came out an identical lilac, which is
    the whole reason the glyph exists.

    Pinned at the two places that must agree: `cls()` has to emit the marker,
    and the stylesheet has to turn it into a character.
    """
    js = _code_only(_js())
    assert "'pos signed'" in js and "'neg signed'" in js, (
        "cls() no longer marks signed values, so no gain gets a + and the sign "
        "is carried by hue alone again"
    )
    css = _css().replace(" ", "").replace("\n", "")
    assert '.pos.signed::before{content:"+"}' in css, (
        "the + glyph is gone from the stylesheet"
    )


def test_a_cost_tint_is_never_given_a_minus_it_did_not_earn():
    """The counterpart, and the reason the glyph is not simply `.neg::before`.

    Commissions and Losing Trades are tinted --bad while holding a POSITIVE
    number: there the red means "this is a cost", not "this is below zero".
    A blanket rule over .neg would render €9.72 of commission as "−€9.72" and
    assert something false. money() already emits its own minus for genuinely
    negative values, so .neg must stay glyph-free on both counts.
    """
    css = _css().replace(" ", "").replace("\n", "")
    assert ".neg.signed::before" not in css and ".neg::before{content" not in css, (
        "a minus is being prefixed to .neg, which double-signs a real loss "
        "(money() already renders one) and mislabels the cost cards as negative"
    )
    stats = _fn("statsPanel") if "function statsPanel(" in _code_only(_js()) else _js()
    assert "'neg '+(String(moneyOf(s.commissions))" in stats.replace("\n", ""), (
        "the Commissions card no longer hardcodes its tint; if it now routes "
        "through cls() it will grow a + on a positive cost"
    )


def test_a_sign_tint_outranks_the_default_colour_of_the_element_it_lands_on():
    """A tint that loses the cascade is a silent no-op, and four had shipped.

    `.pill b{color:var(--fg)}` is specificity (0,1,1); `.pos`/`.neg` are (0,1,0).
    So every pill rendering `<b class="${cls(v)}">` came out --fg with no hue --
    open premium, Month P/L, book value and best month, all four of them.
    Measured in a browser rather than reasoned about: the `<b>` computed
    rgb(245,234,217) instead of rgb(79,209,160), while the `+` glyph from
    `.pos.signed::before` still appeared, so the number carried a sign with no
    colour beside unsigned neighbours.

    Same class of defect as the dead grid rules: a well-formed declaration under
    a selector that cannot reach the element. Nothing was broken enough to
    notice.

    The check is structural, not a hex comparison. For every element whose class
    is composed by `cls()`, find any rule that sets `color` on that element via a
    DESCENDANT selector, and require the stylesheet to qualify `.pos`/`.neg` at
    least as specifically. Only `color` matters -- `.pill b`'s font-weight is
    meant to be unconditional, and a tint should not change the weight.

    Does NOT use `_css().replace(" ","")`, the idiom the other layout tests use,
    and that is the point: stripping whitespace destroys the descendant
    combinator, so `.tab` and `.ta b` become one string. The first version of
    this test did exactly that and reported three rules that do not exist
    (`.ta b`, `.su b`, `.rscru b` -- slices of `.tab`, `.sub`, `.rscrub`) while
    it could not have seen a real `.pill b` either.
    """
    tags = {
        match.group(1)
        for match in re.finditer(r"<([a-z]+) class=\"\$\{cls\(", page_html())
    }
    assert tags, "no element composes its class from cls() any more"

    rules = _css_rules()
    problems = []
    for selector, body in rules:
        if not re.search(r"(?<![-a-z])color\s*:", body):
            continue
        for one in (s.strip() for s in selector.split(",")):
            # `.pill b` -- a class, whitespace, then a bare tag the page tints.
            ancestor = re.fullmatch(r"(\.[a-z0-9_-]+)\s+([a-z]+)", one)
            if not ancestor or ancestor.group(2) not in tags:
                continue
            parent, tag = ancestor.group(1), ancestor.group(2)
            for sign in ("pos", "neg"):
                qualified = rf"{re.escape(parent)}\s+{tag}\.{sign}\b"
                if not any(re.search(qualified, sel) for sel, _ in rules):
                    problems.append(
                        f"`{parent} {tag}` sets color, so `.{sign}` on that {tag} "
                        f"loses the cascade -- add `{parent} {tag}.{sign}`"
                    )
    assert not problems, (
        "these sign tints render with no hue at all: " + "; ".join(problems)
    )


def test_muted_text_meets_wcag_aa_on_every_surface_it_sits_on():
    """--dim2 measured 3.31:1 on --bg and 3.02:1 on --panel2, against the 4.5:1
    that 12px body text requires, and it dressed the footer and every
    explanatory caption -- the prose a newcomer reads first.

    Computed here rather than pinned to a hex, so re-tuning the palette is free
    while regressing legibility is not.
    """
    css = _css()
    def _var(name: str) -> str:
        match = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", css)
        assert match, f"--{name} is gone from the stylesheet"
        return match.group(1)

    def _lum(hex_colour: str) -> float:
        parts = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        chan = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in parts]
        return 0.2126 * chan[0] + 0.7152 * chan[1] + 0.0722 * chan[2]

    def _ratio(fg: str, bg: str) -> float:
        a, b = _lum(fg), _lum(bg)
        hi, lo = max(a, b), min(a, b)
        return (hi + 0.05) / (lo + 0.05)

    fg = _var("dim2")
    # Every surface muted text actually lands on.
    for surface in ("bg", "panel", "panel2"):
        ratio = _ratio(fg, _var(surface))
        assert ratio >= 4.5, (
            f"--dim2 ({fg}) is {ratio:.2f}:1 on --{surface} ({_var(surface)}), "
            f"below the 4.5:1 WCAG AA needs for 12px text. Lighten --dim2."
        )


def test_every_control_has_a_visible_keyboard_focus_ring():
    """There was none: .tab and .icobtn both computed outline-style:none, so
    tabbing through the page left no way to see where you were, and <select>
    wore Chrome's default blue -- the only off-palette colour on the page.

    Declared once for `:focus-visible` rather than per control, so a button
    added later is reachable by default instead of by remembering.
    """
    css = _css().replace(" ", "").replace("\n", "")
    assert ":focus-visible{outline:2pxsolidvar(--leather1)" in css, (
        "the global focus ring is gone, so keyboard users cannot see focus"
    )
    # :focus-visible, not :focus -- otherwise a mouse click leaves a ring that
    # reads as a stuck selection.
    assert ".tab:focus-visible,.icobtn:focus-visible{outline-offset:-2px}" in css, (
        "the inset offset is gone; these two sit flush to a panel edge, where "
        "an outset ring is clipped"
    )


def test_the_stat_row_distributes_its_remainder_instead_of_leaving_a_hole():
    """The dashboard renders exactly nine cards and nine divides evenly into
    none of this page's column counts, so a fixed grid always orphans one.

    Arithmetic, not preference: 9 into 5 leaves one empty cell and 9 into 2
    leaves one, and no card count is gapless across 5, 3 and 2 at once. The two
    obvious fixes measured worse -- four columns leaves THREE gaps, and a
    two-cell hero for Net P&L closes the wide row while opening two at the
    1180px breakpoint, moving the hole rather than removing it.

    So the cards flex and the last row absorbs the leftover width. Verified in a
    browser at three widths: every row ends flush with the container.
    """
    css = _css().replace(" ", "").replace("\n", "")
    assert ".stats{display:flex;flex-wrap:wrap" in css, (
        "the stat row is back to a fixed grid, which orphans a card at 5 and 2 "
        "columns because the dashboard always renders nine"
    )
    assert ".stats>*{flex:11var(--sw)" in css, (
        "cards no longer grow, so the last row stops short of the row above"
    )
    # The basis must stay UNDER the true fraction at every breakpoint, or
    # rounding overflows a row and drops one card onto a line of its own.
    for basis, cols in ((18, 5), (30, 3), (46, 2)):
        assert basis * cols < 100, (
            f"--sw:{basis}% x {cols} exceeds the line, so a row will wrap early"
        )
        assert f"--sw:{basis}%" in css, f"the {cols}-per-row basis is gone"


def test_the_dashboard_renders_exactly_nine_stat_cards(state):
    """The premise the flex row rests on. Both of the dashboard's conditionals
    are either/or -- options-or-not, net-liq-or-not -- so the count is
    structural. If a tenth card lands, the basis widths above want rechecking.
    """
    body = _fn("dashboard")
    # Count the cards the function can emit, minus the alternates that can
    # never both render.
    emitted = body.count("statCard(")
    alternates = body.count("? statCard(")
    assert emitted - alternates == 9, (
        f"the dashboard now renders {emitted - alternates} cards, not 9. The "
        f"flex basis in `.stats` was chosen for nine; recheck it wraps cleanly."
    )


def test_a_zero_crossing_chart_marks_break_even_and_colours_the_loss_side():
    """Cumulative P&L above zero and below it mean opposite things, and the
    chart used to render both in the same warm brass with a zero line drawn at
    the same .055 opacity as the decorative gridlines either side of it. A
    drawdown therefore looked like ordinary variation.

    The fill is split WITHOUT cutting the geometry: one area path painted twice,
    each pass clipped to a half-plane at the zero line. No zero-crossing search,
    no interpolated junctions, and a series that dives and recovers repeatedly
    needs no extra code -- confirmed in a browser on a series that crosses
    twice, which produced three red dots and three brass ones.
    """
    js = _code_only(_js())
    chart = _fn("chart")
    assert 'clip-path="url(#cabove)"' in chart and 'clip-path="url(#cbelow)"' in chart, (
        "the fill is no longer clipped per side, so loss and gain share a colour"
    )
    assert 'id="gneg"' in chart and 'id="gpos"' in chart, (
        "the two sign gradients are gone"
    )
    # Both halves must be driven off the SAME area expression; two hand-built
    # paths would reintroduce the crossing arithmetic this avoids.
    assert chart.count('d="${area}"') == 2, (
        "the two fills no longer share one area path, which is what keeps the "
        "geometry free of zero-crossing special cases"
    )
    assert 'class="zero"' in chart and "break even" in chart, (
        "break-even is no longer labelled when the series crosses it"
    )
    # Drawn only when it means something.
    assert "const crosses=lo<0&&hi>0" in js, (
        "the zero rule is no longer conditional on an actual crossing"
    )
    css = _css().replace(" ", "").replace("\n", "")
    assert ".chart.zero{" in css and "stroke-dasharray:54" in css, (
        "the break-even rule lost the dash that distinguishes it from data"
    )


def test_an_empty_loss_population_reads_as_a_fact_not_a_missing_number():
    """`—` on Avg Loss with zero losing trades reads as "failed to load". There
    have been no losses, which is information; the card should say so.

    Only when the population is genuinely empty AND something has closed, so a
    real average loss still renders as a number and a journal with nothing
    closed still shows the em-dash it should.
    """
    body = _fn("dashboard")
    assert "s.losses===0&&s.closed_episodes>0" in body.replace(" ", ""), (
        "the empty-population case is gone, so Avg Loss shows a bare em-dash "
        "again when there are no losses"
    )
    assert "none yet" in body, "the replacement text is gone"
    css = _css().replace(" ", "").replace("\n", "")
    assert ".stat.v.nil{" in css, (
        "the nil styling is gone, so prose renders at a figure's size and reads "
        "as a value"
    )


def test_the_kicker_does_not_merely_translate_the_title():
    """`Cuaderno de Bitácora` above a title reading `Bitácora` spent the page's
    most prominent small slot restating the next line. The kicker now names what
    the journal is made of, in the vocabulary its own tabs use.
    """
    # The rendered element, not the whole file: the comment beside it names the
    # rejected string on purpose, to say why it was rejected.
    match = re.search(r'<div class="kicker">([^<]*)</div>', page_html())
    assert match, "the header kicker is gone"
    kicker = match.group(1).strip()
    assert kicker == "Strikes · Fills · Round trips", (
        f"unexpected kicker {kicker!r}"
    )
    title = re.search(r'<div class="title">([^<]*)', page_html()).group(1).strip()
    assert title.lower() not in kicker.lower(), (
        f"the kicker {kicker!r} restates the title {title!r}, which spends the "
        f"page's most prominent small slot saying the next line over again"
    )


# --------------------------------------------------------------------------
# The collection strip (SCHEDULER_PLAN.md step 4c).
#
# The one panel whose job is to say whether this journal is still being fed.
# Every assertion here exists because the FIRST version of this surface -- the
# MeshClaw crons -- answered that question wrongly for two days: `last_status`
# read `ok` for bars-live, bars-daily and bars-audit while `price_bars` gained
# nothing. So the tests are about which signal the page reads, not about layout.
# --------------------------------------------------------------------------


def test_the_collection_strip_reads_witnesses_and_blackout_never_ok():
    """`audit.ok` IS GREEN IN A TOTAL BLACKOUT, and the page must not show it.

    `ok` is `not market_traded or not missing`, and `market_traded` is answered by
    "does any underlying have hourly bars that day" -- an oracle that fails the
    same way as the thing it certifies. Reproduced on three copies of the real
    journal: delete every hourly bar, as a fully dead collector would, and `ok`
    goes green because "no bars for anyone" reads as a holiday.

    So this pins the NEGATIVE too. A reader of the payload would reasonably reach
    for `au.ok` -- it is right there, it is a boolean, and it is named for exactly
    this question. That is what makes it worth a test rather than a comment.
    """
    strip = _fn("collection")
    assert "au.witnesses" in strip and "au.blackout" in strip, (
        "the strip no longer reads the two fields that survive a blackout"
    )
    assert "au.ok" not in strip, (
        "the strip is reading audit.ok, which is GREEN in a total collection "
        "blackout -- read witnesses/blackout instead (serialize.audit_data says why)"
    )


def test_a_journal_that_never_collected_is_not_shown_as_a_failure():
    """Two absences that look identical in the payload and need opposite responses.

    `ever_ran:false` (no scheduler has written a heartbeat here) and
    `ever_collected:false` (no bar has ever been stored) are the NORMAL state of a
    CLI-driven journal, and the demo journal is in both. Tinting either red would
    make the page cry wolf on a first run, which is the noise that teaches a reader
    to ignore the panel -- and then it cannot do its job when something is really
    wrong.

    Verified in a browser at both states: `bars none collected` and `scheduler not
    running` render with no tinted `<b>` at all, while `stale` and `blackout`
    render rgb(240,137,154).
    """
    strip = _fn("collection").replace(" ", "").replace("\n", "")
    assert "!au.ever_collected" in strip, (
        "the strip no longer distinguishes a journal that never collected from a "
        "collector that died -- measured identical in the payload without it"
    )
    assert "!sc.ever_ran" in strip, "the never-scheduled case is gone"
    # The untinted branches come FIRST, so a red one cannot shadow them.
    never_bars = strip.index("!au.ever_collected")
    blackout = strip.index("au.blackout?")
    assert never_bars < blackout, (
        "the blackout branch is evaluated before the never-collected one, so a "
        "fresh journal shows a red alarm for bars it never had"
    )
    # And neither untinted branch may carry a tint class. Matched on the ELEMENT
    # in the raw source rather than searched for in a window of the space-stripped
    # text: stripping turns `<b class="neg">` into `<bclass="neg">`, so the first
    # version of this assertion could not have matched whatever the code said --
    # and the ablation proved it, passing with the pill tinted red.
    raw = _fn("collection")
    for label in ("none collected", "not running"):
        element = re.search(rf"<b([^>]*)>{re.escape(label)}</b>", raw)
        assert element, f"the {label!r} pill is gone from the strip"
        assert "class" not in element.group(1), (
            f"the {label!r} pill carries {element.group(1).strip()!r}, tinting it "
            "as a failure -- it is the normal state of a CLI-driven journal"
        )


def test_the_job_status_word_is_rendered_not_collapsed_to_a_colour():
    """`ok` and `nothing` are BOTH healthy and must stay distinguishable.

    A run that fetched no bars is not a success and not a failure -- outside the
    session it is the normal outcome six times in seven. Collapsing the two is
    precisely what let three cron jobs report health while collecting nothing, so
    the status travels as a word. A hue can then add emphasis; it cannot be the
    only carrier, because it cannot say WHICH healthy outcome this was.
    """
    strip = _fn("collection")
    # NOT space-stripped, unlike most pins in this file: the fallback string holds
    # a space, and stripping turns `'never run'` into `'neverrun'`, so the
    # assertion could never match whatever the code said.
    assert "esc(j.last_status||'never run')" in strip, (
        "the status word is gone, so the row carries only a colour"
    )
    # `nothing` must not be tinted as either success or failure.
    tint = re.search(r"const tint=(.*?);", strip.replace("\n", " "), re.S)
    assert tint, "the row's tint expression is gone"
    assert "'nothing'" not in tint.group(1), (
        "`nothing` is being tinted, which makes an empty run look like an "
        "outcome rather than the normal state it is outside the session"
    )


def test_every_runnable_job_gets_a_button_and_a_retired_one_does_not():
    """Step 5e replaces step 4c's read-only pin. That pin is why this one exists.

    The old test forbade `<button` in the strip while `POST /api/jobs/run` did not
    exist, because a button posting to a missing endpoint fails silently in the
    console -- on the panel whose whole purpose is to be trusted. The endpoint
    exists now, so the invariant flips: every REGISTERED job must be runnable from
    here (that is what "no need to run anything from the CLI" means), and a row
    whose job has left the registry must NOT offer a button that cannot work.
    """
    strip = _fn("collection").replace(" ", "").replace("\n", "")
    assert 'class="btnsmjrun"data-job="${esc(j.job)}"' in strip, (
        "the run button is gone, so the strip is read-only again and the jobs can "
        "only be started from a terminal"
    )
    assert "j.retired" in strip, (
        "a retired job would be offered a button that posts a name the registry "
        "no longer knows, which the endpoint answers 400 to"
    )
    # The button is disabled while the job is running, or a second click races the
    # first and gets a 409 for a system that is working.
    assert "busy?'disabled':''" in strip.replace('"', "'"), (
        "the button stays enabled during a run, so a double click reports a "
        "conflict for a job that is simply still going"
    )


def test_only_the_job_that_spends_a_broker_request_asks_for_confirmation():
    """The dialogue is about COST, not caution, and the payload decides.

    `sync` spends one of IBKR's rate-limited Flex requests against a lockout
    budget; the other three hit a public chart endpoint or a calendar feed where an
    extra call costs nothing. A confirm() on all four would train the reader to
    click through the one that matters -- the same argument that keeps `confirm`
    off the calendar's Refresh button.

    Read from `JobRow.spends_request`, which comes from the registry's
    `spends_broker_request`, so the page holds NO list of which jobs touch the
    broker. A second copy of that fact is a copy that drifts, exactly as a page-side
    copy of "USD high-impact" would.
    """
    runner = _fn("bindJobRuns").replace(" ", "").replace("\n", "")
    assert "j.spends_request" in runner, (
        "the page no longer asks the payload which jobs spend a request"
    )
    # The GUARD, not just the word: `if(spends&&!confirm(...))return;` -- a bare
    # search for "confirm(" survived an ablation that disabled it entirely
    # (`if(false&&!window.confirm(`), because the substring was still there.
    assert "if(spends&&!confirm(" in runner, (
        "the confirmation is no longer gated on spends_request and short-circuited "
        "to a return -- a decline must not post"
    )
    assert "return;" in runner, "declining the dialogue no longer aborts the run"
    for name in ("'sync'", '"sync"', "'market'", "'bars_live'"):
        assert name not in runner, (
            f"the page names {name} directly, so it now holds its own copy of "
            "which jobs touch the broker -- read spends_request instead"
        )


def test_the_runner_renders_every_reply_the_endpoint_can_send():
    """409 and 503 are not failures, and rendering them as one is the defect.

    409 means the job is already running: a working system, so the page follows
    the run in flight rather than reporting an error. 503 means the journal is
    locked by another writer and waiting will fix it -- which is precisely what the
    browser could NOT say before the guard existed, because the connection was
    dropped after 16 seconds with no response at all.
    """
    runner = _fn("bindJobRuns")
    assert "409" in runner and "pollRun" in runner, (
        "a 409 no longer follows the run already in flight, so a working system "
        "reports a conflict"
    )
    assert "503" in runner and "locked" in runner, (
        "a 503 is not distinguished, so 'wait a moment' renders as a failure"
    )
    poll = _fn("pollRun")
    assert "'nothing'" in poll.replace('"', "'"), (
        "the poll does not distinguish `nothing` from `ok`, which is the exact "
        "conflation that let three cron jobs look healthy while collecting nothing"
    )
    # Bounded, or a `running` row whose process died spins here forever.
    assert "i<40" in poll.replace(" ", ""), "the poll loop is unbounded"


def test_the_strip_survives_a_payload_without_a_scheduler_block():
    """An older server, or a build_state that raised past those two keys.

    The panel is appended to the dashboard, which every other tab's data shares --
    so an exception here does not blank one card, it blanks the whole view. Two
    guarded reads are cheaper than that.
    """
    strip = _fn("collection").replace(" ", "").replace("\n", "")
    assert "if(!sc||!au)return''" in strip, (
        "the strip no longer guards a missing scheduler/audit block, so a payload "
        "without them throws and takes the entire dashboard down with it"
    )


def test_the_age_wording_matches_the_other_freshness_readout():
    """Two clocks on one page must not describe time differently.

    `quoteNote()` already says "just now" / "3m ago" / "5h ago" for quote age. A
    second vocabulary for heartbeat age would make the reader learn two, and the
    difference would read as significant when it is not.
    """
    ago = _fn("ago")
    quote = _fn("quoteNote")
    for phrase in ("just now", "m ago", "h ago"):
        assert phrase in ago, f"ago() lost the {phrase!r} wording"
        assert phrase in quote, f"quoteNote() no longer says {phrase!r}"
    assert "'never'" in ago.replace('"', "'"), (
        "a null age must read as `never`, not as `NaN ago`"
    )



# --------------------------------------------------------------------------
# POST /api/jobs/run, and the 503 guard that had to land with it
# (SCHEDULER_PLAN.md step 5c).
#
# These use a SCRATCH archive rather than RAW_DIR, and that is not incidental:
# `market` reaches a rate-limited feed and `sync` spends an IBKR request against a
# lockout budget, so every test here targets a job whose work is stubbed or whose
# refusal happens before any work begins.
# --------------------------------------------------------------------------


def _get(base: str, path: str) -> tuple[int, dict]:
    """A GET through a real server, matching `_post`'s shape."""
    import urllib.error  # noqa: PLC0415 - local to this helper
    import urllib.request  # noqa: PLC0415

    try:
        with urllib.request.urlopen(f"{base}{path}", timeout=10) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _post_slow(base: str, path: str, body: dict) -> tuple[int, dict]:
    """`_post` with a client timeout longer than the SERVER's busy timeout.

    Needed for exactly one test. `db.BUSY_TIMEOUT_MS` is 15,000, so sqlite waits
    15 s before raising `database is locked` and only then does the handler answer
    503 -- which is longer than `_post`'s 10 s and made the first version of that
    test fail on the CLIENT's timeout while the server was behaving correctly.
    The wait is the point being measured, so the client has to outlast it.
    """
    import urllib.error  # noqa: PLC0415 - local to this helper
    import urllib.request  # noqa: PLC0415

    request = urllib.request.Request(
        f"{base}{path}", method="POST", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:  # noqa: S310
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_an_unknown_job_is_a_400_that_names_the_real_ones(populated, tmp_path):
    """400, not 500, and the reply lists what IS runnable.

    An endpoint that answers "unknown job" without saying which exist makes the
    caller guess -- and the names are underscore-separated here while MeshClaw's
    are hyphenated (`bars_live` versus `optjournal-bars-live`), which is exactly
    the typo a reader will make.
    """
    with web.serve_ephemeral(db_path=populated, archive_dir=tmp_path) as base:
        status, payload = _post(base, "/api/jobs/run", {"job": "bars-live"})
    assert status == 400
    assert payload["kind"] == "unknown"
    assert "bars_live" in payload["jobs"], (
        "the 400 does not name the jobs that exist, so a hyphen typo is a dead end"
    )


def test_a_job_run_is_recorded_and_its_status_is_readable_afterwards(
    populated, tmp_path, monkeypatch
):
    """The whole round trip: POST returns an id, GET returns that run's row.

    202 rather than 200 on the POST, because the outcome lives in the ledger and
    the page must read it from there -- there is no worker thread until step 6, but
    the contract is the one step 6 needs.
    """
    import dataclasses  # noqa: PLC0415 - local to this test

    from optjournal import jobs as mod  # noqa: PLC0415 - local to this test

    monkeypatch.setattr(mod, "JOBS", tuple(
        dataclasses.replace(job, run=lambda _c, _x: mod.Outcome("ok", "stubbed", 1, 1))
        if job.name == "market" else job
        for job in mod.JOBS
    ))
    with web.serve_ephemeral(db_path=populated, archive_dir=tmp_path) as base:
        status, started = _post(base, "/api/jobs/run", {"job": "market"})
        assert status == 202, f"expected 202, got {status}: {started}"
        assert started["run_id"] > 0
        code, run = _get(base, f"/api/jobs/run?id={started['run_id']}")

    assert code == 200
    assert (run["job"], run["status"], run["detail"]) == ("market", "ok", "stubbed")
    assert run["finished_at"], "the run was never stamped finished"


def test_asking_for_a_run_that_does_not_exist_is_a_404(populated, tmp_path):
    """So a stale id in a poll loop stops rather than reading someone else's row."""
    with web.serve_ephemeral(db_path=populated, archive_dir=tmp_path) as base:
        status, payload = _get(base, "/api/jobs/run?id=999999")
    assert status == 404
    assert payload["kind"] == "missing"


def test_a_non_numeric_run_id_is_refused(populated, tmp_path):
    """`?id=abc` must not become `id=0` and quietly 404 for the wrong reason."""
    with web.serve_ephemeral(db_path=populated, archive_dir=tmp_path) as base:
        status, payload = _get(base, "/api/jobs/run?id=abc")
    assert status == 400
    assert payload["kind"] == "id"


def test_the_run_endpoint_takes_its_target_in_the_body_not_the_path():
    """What keeps `do_POST`'s routing to exact string comparisons.

    That matters structurally rather than stylistically: the `Origin` guard sits
    ahead of the router, and `test_the_origin_check_precedes_the_route_check` pins
    it there by finding the FIRST path comparison. A route like
    `/api/jobs/run/<name>` would need prefix matching, and a prefix match is where
    an endpoint slips past a guard that checks exact paths.
    """
    import inspect  # noqa: PLC0415 - local to this test

    routing = inspect.getsource(web._Handler._route_post)
    assert '"/api/jobs/run"' in routing, "the run route is gone"
    assert "startswith" not in routing and "split" not in routing, (
        "do_POST's routing now does prefix matching, so a path can be routed "
        "without matching a literal the Origin guard's ordering test can see"
    )
    body = inspect.getsource(web._Handler._job_run)
    assert "_body()" in body, "the job target no longer comes from the request body"


def test_a_locked_database_answers_503_rather_than_dropping_the_connection(
    populated, tmp_path
):
    """MEASURED, not assumed, and the measurement is why this guard exists.

    `do_POST` caught nothing and `BaseHTTPRequestHandler` has no error handler, so
    a `sqlite3.OperationalError` escaped the handler and the connection was
    dropped. Against a journal held by `BEGIN EXCLUSIVE`, a real request got:

        POST /api/watchlist  -> RemoteDisconnected: Remote end closed connection
                                without response, after 16.06s
        GET  /api/state      -> 200 in 0.03s

    16 seconds is one `BUSY_TIMEOUT_MS`, and the GET succeeds because WAL lets
    readers through. So the page showed a browser network error naming neither the
    cause nor the fact that waiting would fix it.

    Exercised through a REAL server against a REAL exclusive lock rather than by
    stubbing the error, because the thing under test is what reaches the socket.
    """
    conn = connect(populated)
    conn.execute("BEGIN EXCLUSIVE")
    conn.execute("INSERT OR IGNORE INTO watchlist (symbol, note, added_at)"
                 " VALUES ('LOCKHOLDER', NULL, '2026-08-09')")
    try:
        with web.serve_ephemeral(db_path=populated, archive_dir=tmp_path) as base:
            status, payload = _post_slow(base, "/api/watchlist", {"symbol": "SPY"})
    finally:
        conn.rollback()
        conn.close()

    assert status == 503, (
        f"a locked database answered {status}; before the guard it answered "
        "nothing at all and the connection was dropped after 16s"
    )
    assert payload["kind"] == "busy"
    assert "locked" in payload["message"], (
        "the 503 must name the cause, or it is the same dead end as a dropped "
        "connection with a status code attached"
    )


def test_the_locked_database_guard_covers_every_post_route_not_just_one():
    """Guarded before routing, same as `Origin`, and for the same reason.

    A per-endpoint try/except is a thing each new route has to remember; this
    project has already shipped one endpoint (`/api/market/fetch`) after the guard
    it needed was written, and got it for free precisely because the guard sits
    ahead of the router.
    """
    import inspect  # noqa: PLC0415 - local to this test

    post = inspect.getsource(web._Handler.do_POST)
    assert "sqlite3.OperationalError" in post, (
        "the locked-database guard left do_POST, so a write to a locked journal "
        "drops the connection again"
    )
    assert "_route_post" in post, "do_POST no longer delegates its routing"
    # The routing body must NOT carry its own copy: two guards for one failure is
    # how they drift.
    assert "sqlite3.OperationalError" not in inspect.getsource(
        web._Handler._route_post
    ), "the guard is duplicated inside the router as well as around it"


def test_serve_ephemeral_never_starts_a_scheduler():
    """A SAFETY PROPERTY, not a preference, and it is about this test suite.

    `tests/conftest.py` points `RAW_DIR` at the LIVE `raw/` directory, and six call
    sites pass it to `serve_ephemeral` -- four here, one in `test_rendered.py`, one
    in `sweep.py`. A scheduler started by default would let a `pytest` run fire real
    IBKR fetches against the real archive and the real `.fetch-state.json`, spending
    a rate-limited budget whose penalty is a lockout.

    There is deliberately no parameter to turn it on: a test that wants the loop
    constructs `jobs.Scheduler` directly against a scratch database, which is
    explicit at the call site and cannot be defaulted wrong. So this asserts on the
    SIGNATURE as well as the body -- adding the parameter is the mistake being
    prevented, and it would otherwise pass a body-only check.
    """
    import inspect  # noqa: PLC0415 - local to this test

    signature = inspect.signature(web.serve_ephemeral)
    assert "scheduler" not in signature.parameters, (
        "serve_ephemeral grew a `scheduler` parameter. The suite serves the LIVE "
        "raw/ directory, so a test could then spend real IBKR requests -- build a "
        "jobs.Scheduler against a scratch database instead."
    )
    body = inspect.getsource(web.serve_ephemeral)
    assert "Scheduler(" not in body, (
        "serve_ephemeral constructs a Scheduler, so every test that serves the "
        "live archive now has a reconciler pointed at it"
    )


def test_serve_starts_the_scheduler_by_default_and_stops_it_on_the_way_out():
    """The other direction: `serve` IS the application now.

    Off by default would mean the journal collects nothing unless a human presses a
    button, which is the arrangement this whole plan replaces. And it must be
    STOPPED on exit rather than abandoned: a tick mid-write against a journal the
    caller is about to move is the kind of race that shows up once.
    """
    import inspect  # noqa: PLC0415 - local to this test

    assert web.serve.__kwdefaults__["scheduler"] is True, (
        "the scheduler is off by default, so serve() is a viewer again"
    )
    body = inspect.getsource(web.serve)
    assert "clock.start()" in body and "clock.stop()" in body, (
        "the scheduler is started without being stopped, so shutdown races a tick"
    )
    assert body.index("clock.stop()") > body.index("serve_forever"), (
        "the scheduler is stopped before the server starts serving"
    )


def test_the_demo_never_gets_a_scheduler_whatever_the_flag_says():
    """`optjournal demo` must never write into the real archive, and a scheduler
    pointed at a synthetic one would spend a real IBKR request to fill it.

    Asserted on the CLI's wiring rather than by running the server, because the
    invariant is the conjunction: `--demo` wins over the scheduler flag.
    """
    import inspect  # noqa: PLC0415 - local to this test

    from optjournal.cli import cmd_serve  # noqa: PLC0415 - local to this test

    wiring = inspect.getsource(cmd_serve).replace(" ", "")
    assert "scheduler=bool(args.scheduler)andnotargs.demo" in wiring, (
        "the demo can now start a scheduler, which would fetch real data into a "
        "synthetic journal"
    )
