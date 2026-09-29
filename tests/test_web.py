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

import inspect
import json
import re
import socket
import sqlite3
from pathlib import Path

import pytest
from conftest import RAW_DIR, ROOT, code_only

from optjournal import replay as replay_mod
from optjournal import web
from optjournal.clock import epoch_et
from optjournal.config import (
    DEFAULT_ARCHIVE,
    DEFAULT_DB,
    DEFAULT_DEMO_DB,
    DEFAULT_DEMO_DIR,
)
from optjournal.db import connect, migrate, open_journal
from optjournal.history import build_history
from optjournal.stats import campaigns_for
from optjournal.web import (
    _origin_is_same,
    build_state,
    companion_html,
    page_html,
    serve,
)


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

    from optjournal import journal
    from optjournal.clock import MARKET_TZ
    from optjournal.db import connect
    from optjournal.events import parse_events, store_events

    # MARKET_TZ, not UTC. `market_data` anchors its week in market time, so a
    # fixture dating "today" in UTC seeds an event outside the window for the
    # hours when the two calendars disagree -- every day between UTC midnight and
    # market midnight. The shape then had no sample and the contract guard failed,
    # on a clock rather than on a change. Same timeline as the code under test.
    today = datetime.now(MARKET_TZ).date()
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
    # A journal entry, seeded for the same reason as the events and the job row:
    # the contract guard anchors `JournalEntry` to a real payload row, and every
    # fresh journal has none -- so the shape would be quietly exempted rather than
    # checked. Written against a REAL order id through the module's own writer, so
    # the row is one the endpoint could have produced and it attaches to a
    # lifecycle card the Trades tab actually draws.
    target = conn.execute(
        "SELECT ib_order_id, account_id, COALESCE(underlying_symbol, symbol) AS u,"
        " MIN(trade_date) AS opened FROM trades WHERE ib_order_id IS NOT NULL"
    ).fetchone()
    if target:
        journal.save(
            conn, target["ib_order_id"], account_id=target["account_id"],
            underlying_symbol=target["u"], opened_on=target["opened"],
            values={"plan_target": "take at 50% of credit",
                    "plan_invalidation": "short strike tested",
                    "followed_target": "yes", "exit_trigger": "target",
                    "lessons": "sized right, closed a week early"},
        )
    conn.commit()
    conn.close()
    return build_state(db_path=populated, archive_dir=RAW_DIR, query_id="1591754")


@pytest.fixture
def widest_costs(populated) -> dict:
    """The cost payload under the widest possible scope.

    The Costs tab defaults to options only, so `state["broker_costs"]` carries no
    conversion pair, no fee group and no withholding line -- and the contract guard
    anchors those shapes to real rows, so sampling the default alone would leave
    four of them unanchored and quietly exempt from the drift test.

    A separate fixture rather than a second payload key: the page never asks for
    two scopes at once, so the payload should not carry two. This is the same
    serializer the page gets, just asked a wider question.
    """
    return build_state(
        db_path=populated, archive_dir=RAW_DIR, query_id=None,
        cost_scope=["OPT", "STK", "CASH"],
    )["broker_costs"]


def _js() -> str:
    """The page's inline script, whatever attributes its tag carries.

    Tolerant of attributes because the tag became `<script type="module">` when
    the chart's arithmetic moved to /static/replay.js. That module is NOT scanned
    here and does not need to be: it receives plain arrays and numbers, never the
    payload, so every payload read this contract polices still happens in the
    page. A read moving into the module would show up as a property read on an
    undeclared binding, which is the same failure this guard already raises.

    HTML COMMENTS ARE STRIPPED FIRST, and that is not tidiness. This split used to
    run on the raw page, so the first literal `<script` won -- and a comment in
    <head> that mentions the inline `<script` in prose is enough to win it. One did,
    while documenting the CSP's script-src exemption, and the helper then returned
    the entire head and body AS the script: `app.css`, `mark.svg` and `replay.js`
    all surfaced as undeclared property reads on bindings called `app`, `mark` and
    `replay`. The contract guard failed for a reason that had nothing to do with the
    contract. A helper this much of the file depends on should not be defeatable by
    a sentence.
    """
    page = re.sub(r"<!--.*?-->", "", page_html(), flags=re.S)
    body = page.split("<script", 1)[1]
    script = body.split(">", 1)[1].split("</script>")[0]
    return _IMPORT.sub("", script)


#: The ES module imports, ALL of them. Dropped from the scanned script rather than
#: stripped by `code_only`, because each quoted path parses as a property read on a
#: binding named after the file (`replay`, `watch`) that exists nowhere -- and
#: stripping ALL string literals broke the tests that legitimately assert on them.
#: It was `count=1` while there was one module, which reported `watch` as an
#: undeclared binding the moment the second one landed.
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
    # `JField` is page-side, like `Bucket`: the journal form's own field table,
    # never sent by the server. `JournalWrite` is the `/api/journal` reply, off
    # the state payload like every other write reply here. `LinkWrite` is the
    # `/api/links` reply, for the same reason.
    "JField", "JournalWrite", "LinkWrite",
    # The `/api/odte/refresh` reply, off the state payload like the other writes.
    "OdteRefresh",
    # A used exit trigger needs a written review, and the fixture archive has no
    # journal rows. Exercised directly in test_serialize.
    "TriggerTally",
    "MarketFetch", "WatchWrite", "QuoteReply", "Quote",
    # Reached only through `QuoteReply.ranks`, the `/api/quotes` reply, not the
    # state payload -- so no `/api/state` sample can carry it, exactly like
    # `Quote` and `QuoteReply` above. The branch that added it never saw this:
    # the coherence test skips without the `raw/` archive, which the watchlist
    # worktree lacked, so it first ran here on the real checkout.
    "IvRank",
    # The 0DTE calculator's payload shapes. `OdteBlock.context` is null until a
    # `bars` fetch has landed the S&P and VIX daily closes, and the archive-only
    # fixture has ingested statements but no index bars -- so the nested context
    # never materialises here. Exempt for the same reason as the quote shapes
    # above, and exercised directly against inserted bars in test_serialize.py.
    "OdteContext", "OdteEvent",
    # And its PAGE-SIDE shapes, like `ChartPoint` and `Bucket`: a ladder row, a
    # row's scratch-line marks, a sold level read against the close, and the
    # session's expected move are all built in `static/zdte.js` from two typed
    # numbers, so no `/api/state` sample can carry them. Their keys are pinned by
    # tests/frontend/zdte.test.mjs, which runs the module that produces them.
    "StrikeRow", "ScratchLine", "Scratch", "Move", "SessionEvent",
    # `OdteSession` is server-built but empty for the same reason `OdteContext` is
    # null: scoring needs TWO sessions of index bars and this fixture has none.
    # `RailScore` and `ScoredSession` are page-side, out of `zdte.railScores`, so
    # no `/api/state` sample can carry them either. All three are exercised
    # directly -- the first against inserted bars in test_serialize.py, the other
    # two in tests/frontend/zdte.test.mjs.
    "OdteSession", "RailScore", "ScoredSession",
    # `GET /api/settings/token`. Deliberately off the state payload -- the
    # keyring has been measured at 8.2s with a locked keychain, so presence is
    # fetched by a button rather than on every page load, and no `/api/state`
    # sample can carry it.
    "TokenStatus",
    # `/api/jobs/run`, both verbs. Exempt for the same reason as the rest and one
    # more: its keys are CONDITIONAL on the HTTP status (`jobs` only on 400,
    # `run_id` on 202 and 409, `status` only on the GET), so no single reply
    # carries them all and a sampled one would make four of them look absent.
    # Pinned against the handlers' source below instead.
    "JobReply",
})


def _shape_samples(state: dict, widest: dict) -> dict[str, dict]:
    """One real instance of every sampled shape, from the live payload.

    Keyed per SHAPE, not per binding: this map changes when a new payload
    panel is born (rare), where the old registry changed on every new
    template variable (constant). It is the fixture-side anchor of the
    contract -- the thing that stops the typedefs agreeing with themselves.
    """

    def first(rows):
        return rows[0] if rows else None

    costs = first(state["costs"])
    # The cost payload under the WIDEST scope. The tab defaults to options only,
    # so a conversion pair, a fee group and a withholding line do not appear in
    # `state["broker_costs"]` -- and sampling only that would leave those shapes
    # unanchored, which silently exempts them from the drift test. Built here from
    # the same serializer rather than added to the payload: the page never asks for
    # two scopes at once, so the payload should not carry two.
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
        # all_time rather than the selected month: a single month can hold no
        # decided position and leave the shape null, which would exempt it.
        "StrategyRank": state["all_time"]["best_strategy"],
        # Anchors the nested money shape to a real figure, so the Money
        # typedef cannot drift from what the serializer actually sends.
        "Money": state["stats"]["commissions"],
        "Day": first(state["stats"]["days"]),
        "Position": first(state["positions"]),
        "Allocation": state["allocation"],
        "JournalReview": state["journal"]["review"],
        "Tally": state["journal"]["review"]["plan"]["held"],
        "AllocationRow": first(state["allocation"]["rows"]),
        "Order": first(orders),
        "Leg": first(orders[0]["legs"]) if orders else None,
        "LegMoney": first(orders[0]["legs"])["money"] if orders else None,
        "Episode": first(history["closed"] + history["open"]),
        "History": history,
        "Costs": costs,
        "CostsTotals": costs["totals"] if costs else None,
        "FxRow": first(costs["fx"]) if costs else None,
        "Statement": first(state["statements"]),
        # Always present and always three keys, even on a journal with no confirms
        # -- so it anchors properly rather than needing an exemption. The counts
        # being zero is the steady state, not an absent shape.
        "Provisional": state["provisional"],
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
        # The reader's own writing, anchored to the entry the `state` fixture
        # seeds. Not exempted: every key here is one the modal binds to, and an
        # unsampled shape is a shape the drift guard stops covering.
        "Journal": state["journal"],
        "Trigger": first(state["journal"]["triggers"]),
        "JournalEntry": first(list(state["journal"]["entries"].values())),
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
        # The DB-backed cost report. Every shape is anchored to a real row rather
        # than exempted, and the default scope is options-only -- so the samples
        # that only exist under a WIDER scope (a conversion pair, a fee group) are
        # taken from an account-wide report built alongside. Sampling only the
        # default would leave four shapes unanchored and quietly exempt from the
        # drift test, which is the failure this map exists to prevent.
        "BrokerCosts": state["broker_costs"],
        "CostSelection": state["broker_costs"]["scope"],
        "CostTotals": state["broker_costs"]["totals"],
        "AutoFx": state["broker_costs"]["totals"]["autofx"],
        "FrictionTotals": state["broker_costs"]["totals"]["friction"],
        # The ledger-carrying money shape, anchored to a figure that HAS a ledger.
        "Charge": state["broker_costs"]["totals"]["attributable"],
        "CategoryCost": first(widest["by_category"]),
        "FxPairCost": first(widest["fx"]),
        "FxLeg": first(widest["fx"])["auto"],
        "FxLegManual": first(widest["fx"])["manual"],
        "FeeGroup": first(widest["fees"]),
        "Withholding": first(widest["withholding"]),
        "Audit": state["audit"],
        # The header dateline. Anchored to the real payload rather than exempted,
        # even though it is two keys: the page slices `opened` apart to format a
        # date, so a rename there renders the header's most prominent line wrong
        # rather than merely blank.
        "Logbook": state["logbook"],
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
        "Settings": state["settings"],
    }
    missing = sorted(k for k, v in samples.items() if v is None)
    assert not missing, (
        f"the fixture no longer produces a sample for {missing} -- the drift "
        "test would silently stop covering those shapes, so this fails instead"
    )
    return samples


def test_contract_is_coherent(state, widest_costs):
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

    samples = _shape_samples(state, widest_costs)
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


def test_contract_matches_the_payload_both_ways(state, widest_costs):
    """The typedefs in page.html are held to a real payload in BOTH
    directions: a required key the API stopped sending fails (the contract
    cannot rot optimistic), and a key the API sends that the contract omits
    fails (the server cannot outrun its documentation). Optional keys --
    `[bracketed]` in the typedef -- are exempt from the first direction
    only."""
    shapes, _, _ = _parse_contract(_js())
    problems = []
    for name, sample in _shape_samples(state, widest_costs).items():
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


def test_the_stale_server_guard_names_keys_that_exist(state):
    """The runtime guard must check keys the payload really has.

    `STATS_KEYS_REQUIRED` is the page's own list of `stats` keys whose absence
    means the server process predates the markup. A name that stops existing --
    renamed, or dropped from the serializer -- would leave the guard passing
    unconditionally: it would look like protection while checking nothing, which
    is the same silent-no-op failure `test_every_mutant_pattern_still_matches`
    exists for.

    Both directions matter, so this asserts the names are declared in the Stats
    typedef AND present in a real payload.
    """
    js = _js()
    match = re.search(r"const STATS_KEYS_REQUIRED=\[([^\]]*)\]", js)
    assert match, "the stale-server guard's key list is gone"
    keys = re.findall(r"'([a-z_]+)'", match.group(1))
    assert keys, "the guard checks nothing, so it can never fire"

    shapes, _, _ = _parse_contract(js)
    undeclared = sorted(set(keys) - set(shapes["Stats"]))
    assert not undeclared, (
        f"the guard watches {undeclared}, which the Stats typedef does not "
        "declare -- so it guards a key that may not exist"
    )
    absent = sorted(k for k in keys if k not in state["stats"])
    assert not absent, (
        f"the guard watches {absent}, which a real payload does not contain -- "
        "the banner would fire on every load"
    )

    # And the same both ways for the STATE-level list, which exists because a
    # missing `journal` is worse than a cell reading `undefined`: the form still
    # renders and Save posts to an endpoint the old process does not have, so a
    # whole write-up goes nowhere and nothing says why.
    top = re.search(r"const STATE_KEYS_REQUIRED=\[([^\]]*)\]", js)
    assert top, "the state-level half of the stale-server guard is gone"
    top_keys = re.findall(r"'([a-z_]+)'", top.group(1))
    assert top_keys, "the state-level guard checks nothing, so it can never fire"
    undeclared_top = sorted(set(top_keys) - set(shapes["State"]))
    assert not undeclared_top, (
        f"the guard watches State.{undeclared_top}, which the typedef does not "
        "declare"
    )
    absent_top = sorted(k for k in top_keys if k not in state)
    assert not absent_top, (
        f"the guard watches {absent_top}, absent from a real payload -- the "
        "banner would fire on every load"
    )


def test_the_stale_server_guard_runs_before_anything_renders(state):
    """It must be called where the payload ARRIVES, not from a render path.

    Called from `draw()` it would fire once per redraw and be re-armed by every
    tab click; called after `draw()` the undefined figures would already be on
    screen when the explanation appeared. `load()` right after the assignment to
    `S.state` is the one place it sees each payload exactly once, before a single
    card is built.
    """
    js = _js()
    assign = js.index("S.state=await r.json();")
    check = js.index("staleServerCheck(S.state);")
    drawn = js.index("draw();", assign)
    assert assign < check < drawn, (
        "the guard must run after the payload lands and before the first draw"
    )


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
        "closed_episodes", "open_episodes", "decided_campaigns",
        "green_days", "red_days", "days",
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


def test_the_browsable_range_survives_a_compact_ibkr_date(conn):
    """A journal whose earliest row is stored compact browses from the right month.

    `trades.trade_date` mixes ISO `2025-01-14 14:30:05` with IBKR's compact
    `20250114`. `month_range` used to slice the raw value to seven characters,
    which turns the compact form into `2025011` -- and that is the trap, because
    it does NOT fail the length check. It parses as month ELEVEN, so an account
    whose first fill was in January silently began browsing in November, and the
    calendar walked ten months the account never lived. Measured on the old
    implementation, which returned `2025-11` as its oldest month.

    Both readings now go through `_day_of`, shared with `logbook_data` so the
    header counts days from the same instant the calendar starts at, and the
    stored form cannot change either answer.
    """
    columns = ["broker", "trade_id", "ib_exec_id", "transaction_id", "account_id",
               "trade_date", "asset_category", "symbol", "quantity", "currency",
               "fx_rate_to_base", "raw", "source_file", "first_seen_at"]
    values = ["IBKR", "t1", "e1", "x1", "U1", "20250114", "OPT", "SPY", 1,
              "USD", 1.0, "{}", "f.xml", "2025-01-14T00:00:00Z"]
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        f"INSERT INTO trades ({','.join(columns)})"  # noqa: S608 - fixed names
        f" VALUES ({','.join('?' * len(columns))})",
        values,
    )
    conn.commit()
    from optjournal.stats import first_activity, month_range

    assert first_activity(conn) == "2025-01-14"
    months = month_range(conn)
    assert months, "a compact earliest date left the account with no months"
    assert months[-1] == "2025-01"


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
    # The scoreboard's own unit is the campaign, and it always accounts for
    # itself. Against `closed_episodes` it need NOT agree: a roll closes one
    # contract and opens another, so a month can close a contract whose decision
    # finishes later. `closed_episodes` is never below it, which is the
    # reconciliation the page prints when the two differ.
    assert opened["wins"] + opened["losses"] == opened["decided_campaigns"]
    assert opened["closed_episodes"] >= opened["decided_campaigns"]
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


def test_inflight_realised_explains_the_gap_between_p_and_l_and_the_scoreboard(
    populated,
):
    """The card's note, checked against an independent recount of the archive.

    Net P&L sums contract round trips; the scoreboard counts decided positions. A
    roll settles its near contract for real cash while the decision carries on, so
    the two legitimately differ and `inflight_realized` is what the note uses to
    say by how much. On the real archive that is the GOOG chain: rolled in August,
    still open, its 420C leg already settled.

    Recounted here from the episodes rather than compared against another payload
    figure, so a bug that moved both in step would still fail. Three properties,
    each a way the note could lie:

    * it is a SUBSET of the P&L it qualifies ("of this figure"),
    * it is exactly the closed episodes sitting in an unfinished position,
    * it is zero when every position has finished, rather than merely small.
    """
    conn = connect(populated)
    try:
        report = build_history(conn, asset_category="OPT")
        campaigns = campaigns_for(conn, "OPT", report.episodes)
    finally:
        conn.close()

    expected = 0.0
    for campaign in campaigns:
        episodes = [report.episodes[i] for i in campaign.episode_indices]
        if all(e.is_closed for e in episodes):
            continue
        expected += sum(e.realized_pnl_base for e in episodes if e.is_closed)

    stats = build_state(
        db_path=populated, archive_dir=RAW_DIR, query_id=None
    )["stats"]
    got = stats["inflight_realized"]["base"]

    assert got == pytest.approx(expected)
    assert abs(got) <= abs(stats["net_pnl"]["base"]) + 1e-9, (
        "the note says 'of this figure', so the part cannot exceed the whole"
    )
    if not expected:
        pytest.skip("archive has no unfinished position holding settled cash")
    # Strictness, so the assertions above cannot pass on an all-zero payload: the
    # gap the note exists for is genuinely open on this archive.
    assert stats["closed_episodes"] > stats["decided_campaigns"]


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
    candidates = []
    for episode in spanning:
        owners = [
            lifecycle
            for lifecycle in st["lifecycles"]
            if str(episode.conid) in lifecycle["conids"]
        ]
        if len(owners) == 1 and len(owners[0]["conids"]) == 1:
            candidates.append((episode, owners[0]))
    if not candidates:
        pytest.skip("fixture has no single-contract lifecycle spanning two months")
    ep, lc = candidates[0]
    owning = [lc]
    assert len(owning) == 1, "exactly one lifecycle owns the contract"
    assert lc["status"] == "closed"
    assert len(lc["events"]) >= 2, "the open and the close are both present"
    assert lc["opened_at"][:10] == ep.opened_at[:10]
    assert lc["closed_at"][:10] == ep.closed_at[:10]
    assert lc["realized_pnl"]["base"] == pytest.approx(ep.realized_pnl_base)
    # An open lifecycle keeps the Dashboard's rule: nothing until flat.
    for open_lc in (x for x in st["lifecycles"] if x["status"] == "open"):
        assert open_lc["realized_pnl"] is None


def test_dashboard_headline_counts_decided_positions_for_options():
    """'Total Trades' as a fill count let a month claim trades whose outcome
    belonged to a later month -- open in July, close in August, and July's card
    said '3 trades' while its P&L, wins and losses all correctly read zero. For
    options the headline is DECIDED POSITIONS, the same population the wins,
    losses and averages measure: an episode is per contract, so counting those
    scored a roll as two trades for one decision. Fills and contract round trips
    both survive in the sub-note, named as what they are."""
    js = _js()
    assert "statCard('Trades', s.decided_campaigns," in js
    assert "fill(s), ${s.orders} order(s)" in js, "fills stay visible as activity"
    assert "${s.closed_episodes} contract round trip(s)" in js, (
        "the money's unit stays visible: net P&L is attributed by it, so a "
        "reader adding up the P&L needs to see it"
    )
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
                           # one Quote entry, read per row in the watchlist. `name`
                           # rides here rather than on /api/state because it is
                           # already inside the reply this request pays for, so it
                           # costs no second per-symbol call
                           "price", "at", "previous_close", "currency", "name"),
        # Both verbs of /api/jobs/run. Its keys are conditional on the status --
        # `jobs` only on 400, `run_id` on 202 and 409 -- so a sampled reply would
        # make four of them look absent, which is why it is source-pinned.
        _Handler._job_run: ("ok", "kind", "message", "jobs", "run_id", "job"),
        _Handler._job_status: ("ok", "kind", "message"),
        # Both verbs of /api/settings/token, which answer in ONE shape on purpose:
        # a save's reply is a token status, so the page assigns it exactly where
        # the check button's answer goes. Pinning both against the same key list is
        # what stops one of them drifting out of that arrangement.
        _Handler._token_status: ("ok", "kind", "present", "account", "message"),
        _Handler._token_write: ("ok", "kind", "present", "account", "message"),
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


def test_the_companion_window_is_served_as_a_document_of_its_own():
    """`window.open('/companion')` has to answer with markup, not with a 404 JSON.

    The one route the page opens in a SECOND window, so nothing about it is
    exercised by loading the dashboard: a missing branch in `do_GET` would leave
    the 0DTE tab's Broker Companion button opening a window containing
    `{"error": "not found"}`, and every other test in this file green.

    Checked over a real server rather than by reading `do_GET`, because the failure
    is the response -- its status, its type, and that the body is the companion and
    not the page.
    """
    import urllib.request  # noqa: PLC0415 - local to this test

    with web.serve_ephemeral(db_path=DEFAULT_DEMO_DB, archive_dir=DEFAULT_DEMO_DIR) as base:
        with urllib.request.urlopen(f"{base}/companion", timeout=10) as resp:  # noqa: S310
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/html")
            body = resp.read().decode()

    assert "Broker Companion" in body
    assert "/static/zdte.js" in body, "it must share the calculator's arithmetic"
    # `fetch(` rather than a path, because the prose in this document names
    # /api/state to explain why it does NOT ask for it.
    assert "fetch(" not in body, (
        "the companion takes its three numbers from its own hash; a payload fetch "
        "here would put a brokerage account behind a 330px window that needs none"
    )


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
    attrs = [a for markup in _dressed() for a in re.findall(r'style="([^"]*)"', markup)]
    assert not attrs, (
        f"styling belongs in static/app.css, where the layout tests can see it: "
        f"{attrs}"
    )


#: Words of visible prose one `.note` or `.sub` may carry. A BUDGET, not a style
#: rule: the page is at 32 and the cap is what stops the next explanation being
#: written where it is cheap rather than where it belongs.
PROSE_BUDGET = 40


def test_no_caption_carries_more_prose_than_a_reader_will_read():
    """897 WORDS OF STANDING PROSE, across nine tabs, on every single load.

    Measured in a browser before this was cut, and it is what "the app does not feel
    polished" turned out to mean. The dashboard alone carried 280 — including one
    98-word note, one of 80, and a caption that opened with "Amounts in EUR, the
    account base currency" on a page where every figure already shows a €.

    The cause was mechanical, not editorial. The `i` affordance existed but had no
    helper, so it was written out by hand at four sites; adding a paragraph to a
    `.sub` was one line and adding it to a tip was six. Prose went where it was
    cheap. `infoTip()` closed that gap, and this budget is what keeps it closed —
    without it the next explanation goes back into the caption for the same reason.

    The split the cull applied, which is the rule this enforces the edge of:

    - A fact the reader can SEE stays visible. Two counts that disagree has to be
      named or the figures look wrong.
    - Reference — why they disagree, what a column counts, how a figure is modelled
      — goes behind an `i`. Wanted once, and nobody re-reads it on the ninth visit.
    - A caption that says the same thing on every load says nothing. `ccyNote`
      returns '' in the base case now for exactly that reason.

    Conditional notices are exempt from the spirit but not the letter: an empty state
    or a warning that appears only when something is true is information, and several
    sit in the 20-40 range legitimately. The cap is set above them and below the
    paragraphs, which is where a budget belongs.

    Counted on the TEMPLATE, so it holds for data this demo journal cannot produce —
    the 97-word replay legend rendered only with a replay expanded and was invisible
    to the browser sweep that found the others.
    """
    code = re.sub(r"/\*.*?\*/", "", _js(), flags=re.S)
    over = []
    for found in re.finditer(r'class="(?:note|sub)[^"]*">(.*?)</div>', code, flags=re.S):
        body = found.group(1)
        # An `i`'s contents are not visible prose -- that is the whole point of it.
        body = re.sub(r"\$\{infoTip\(.*?\)\}", "", body, flags=re.S)
        # An interpolation renders as one figure or short phrase, not as prose.
        body = re.sub(r"\$\{.*?\}", " X ", body, flags=re.S)
        text = " ".join(re.sub(r"<[^>]+>", " ", body).split())
        # A table's own header markup matches this shape; it is not a caption.
        if "</th>" in body or "</tr>" in body:
            continue
        if len(text.split()) > PROSE_BUDGET:
            over.append(f"{len(text.split())}w: {text[:90]}")
    assert not over, (
        f"these captions are over the {PROSE_BUDGET}-word budget:\n  "
        + "\n  ".join(over)
        + "\n\nMove the reference half behind `infoTip(...)` and leave the fact the "
        "reader needs on sight. If it is genuinely all needed on sight, raise "
        "PROSE_BUDGET deliberately and say why here."
    )


def test_the_csp_exempts_inline_script_only_and_nothing_needs_more():
    """`'unsafe-inline'` IS THE WHOLE POLICY'S WEAK POINT, so it is scoped and pinned.

    It used to sit on `default-src`, where one token licensed inline script AND
    inline style -- and inline style is the easiest thing for an injected broker
    string to reach and the hardest to spot. Nothing in this project needs it:
    `test_styling_lives_in_the_stylesheet_not_in_the_markup` above forbids
    `style="..."`, `app.css` is an external stylesheet, and there are no `.style.x =`
    assignments in the page. So the exemption now names `script-src` alone and style
    falls back to a strict `default-src 'self'`.

    This test and that one hold the property jointly: the strict `style-src` is only
    safe while nothing has quietly started setting styles from JavaScript, which the
    regex below is what checks. No CSP test existed before, so the header had drifted
    from the two comments describing it -- both quoted `default-src 'self'` and
    omitted the exemption entirely.
    """
    header = (ROOT / "src" / "optjournal" / "web.py").read_text()
    policy = re.search(
        r'"Content-Security-Policy",\s*\n?\s*"([^"]*)"', header
    ) or re.search(r'"Content-Security-Policy",\s*"([^"]*)"', header)
    assert policy, "no Content-Security-Policy header is being sent at all"
    value = policy.group(1)

    assert "default-src 'self'" in value, "the same-origin default is gone"
    assert "script-src 'self' 'unsafe-inline'" in value, (
        f"the inline-script exemption is not scoped to script-src: {value!r}. The "
        f"page's one inline <script> needs it; inline STYLE must not get it too."
    )
    # The directive the exemption must never reappear on.
    default = value.split(";")[0]
    assert "unsafe-inline" not in default, (
        f"`'unsafe-inline'` is back on default-src ({default!r}), which silently "
        f"re-licenses every inline style attribute an injected value could carry"
    )
    assert "unsafe-eval" not in value, "unsafe-eval is never needed here"

    # What makes the strict style-src safe, checked directly rather than assumed.
    js = _code_only(_js())
    styled = re.findall(r"\.style\.[A-Za-z]", js)
    assert not styled, (
        f"the page sets styles from JavaScript ({sorted(set(styled))}), which the "
        f"strict style-src now blocks. Move it to a class, or the policy has to "
        f"loosen again."
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

    SELECTORS ONLY, which is what the name always claimed and the regex did not
    do: it scanned the whole file, declaration values included, and so read
    `url(/static/fonts/Geist-Variable.woff2)` as defining a class `.woff2`. No
    value had ever held a dot followed by a letter until the stylesheet gained a
    font file, so the gap was invisible until then. `_css_rules` already splits
    selector from body, so this reads the one half a class can live in.
    """
    return set(re.findall(r"\.([A-Za-z][A-Za-z0-9_-]*)",
                          " ".join(sel for sel, _ in _css_rules())))


def _dressed() -> list[str]:
    """Every document `app.css` styles.

    TWO of them since the 0DTE calculator shipped: the page, and the Broker
    Companion window it opens. Both directions of the class contract below run
    over this list rather than over the page alone -- the companion reuses the
    calculator's own classes, so checking only the page would report a live rule
    as dead the moment a class was used by the smaller document only.
    """
    return [page_html(), companion_html()]


def _literal_page_classes() -> set[str]:
    """Class tokens the documents state outright, with `${...}` blanked out.

    Blanked rather than parsed: an interpolation is a JS expression whose value
    this cannot know, so a token that only ever arrives through one is checked by
    the other direction instead. What is left is every class written down as
    text, which is where a typo lands.
    """
    tokens: set[str] = set()
    for markup in _dressed():
        for attr in re.finditer(r'class="([^"]*)"', markup):
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
    markup = "\n".join(_dressed())
    unreachable = sorted(
        cls for cls in _css_classes()
        if not re.search(rf"(?<![A-Za-z0-9_-]){re.escape(cls)}(?![A-Za-z0-9_-])", markup)
    )
    assert not unreachable, (
        f"static/app.css defines these and neither document names them, so they "
        f"are dead: {unreachable}"
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

    CONTAINER properties only. `grid-area`, `grid-column` and `grid-row` were in
    this list and had to come out, because they are grid ITEM properties: they are
    set on the CHILDREN of a grid, whose own `display` is irrelevant and is
    usually `block`. Listing them made the check demand `display:grid` on the
    wrong element -- it flagged `.brandhead`, `.tabs`, `.hdr-actions` and
    `.stat.lead`, every one of them a correct child of a correct grid, while
    `.brand` and `.stats` declared the grid a line or two above. A rule that
    reports four false positives is a rule that gets deleted, so the honest fix is
    for it to check only what it can actually know from one selector.

    The item-property case is not unchecked, it is checked by arithmetic instead:
    `test_the_scoreboard_is_a_grid_whose_columns_every_tile_count_divides` reads
    the real column counts and tile counts, which is the thing that can actually
    go wrong with a grid item.
    """
    grid_only = ("grid-template-columns", "grid-template-rows", "grid-template-areas",
                 "grid-auto-flow")
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
    formatter = (
        ROOT / "src" / "optjournal" / "static" / "format.js"
    ).read_text(encoding="utf-8")
    assert "export const esc" in formatter, "no escaping helper defined"

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
    body = code_only(
        (ROOT / "src" / "optjournal" / "web.py").read_text(encoding="utf-8")
    )
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
    # The typed earnings date, refused by its own `kind` so the page can point at
    # the field rather than at the row. A format check only -- this journal cannot
    # know whether a company reports that day -- but the format is checked in both
    # halves: the SPELLING, because `date.fromisoformat` accepts `20260827` and
    # `2026-W35-1` on this interpreter and a column showing two spellings of one
    # date is a column nobody can sort, and the CALENDAR, because a countdown to
    # 2026-13-45 would be arithmetic over a day that does not exist.
    ({"symbol": "SPY", "earnings_on": "27/08/2026"}, "date"),
    ({"symbol": "SPY", "earnings_on": "2026-13-45"}, "date"),
    ({"symbol": "SPY", "earnings_on": "20260827"}, "date"),
    ({"symbol": "SPY", "earnings_on": "next thursday"}, "date"),
    # A price alert is a level above zero, or empty to clear it.
    ({"symbol": "SPY", "alert_above": "abc"}, "alert"),
    ({"symbol": "SPY", "alert_below": "-5"}, "alert"),
    ({"symbol": "SPY", "alert_above": "0"}, "alert"),
    ({"symbol": "SPY", "alert_above": "nan"}, "alert"),
    ({"symbol": "SPY", "alert_above": "inf"}, "alert"),
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


def _watch_row(db: Path, symbol: str = "AMD") -> dict:
    """The stored row for one watched symbol, as a plain dict."""
    conn = connect(db)
    try:
        row = conn.execute(
            "SELECT note, earnings_on FROM watchlist WHERE symbol = ?", (symbol,)
        ).fetchone()
    finally:
        conn.close()
    return {} if row is None else dict(row)


def test_a_typed_field_is_written_when_present_and_left_alone_when_absent(tmp_path):
    """Key-present semantics, in the SEQUENCE that made them necessary.

    Three requests that all look like "add AMD" and mean three different things:

    * `note` and `earnings_on` PRESENT -- write both.
    * both ABSENT (the add form's own body) -- leave both alone. This is the
      documented behaviour of a bare re-add, and it has to key off PRESENCE rather
      than emptiness, which is the whole reason the request shape decides.
    * `note` present and EMPTY -- write NULL. That was impossible before: the
      endpoint mapped "" to None and the upsert's `COALESCE(excluded.note, note)`
      read None as "keep the old value", so a note could be set and never cleared
      while the reply said `ok`.

    Over a SCRATCH journal rather than the `populated` fixture, deliberately: this
    rule needs no statement, and `populated` skips wherever the archive is absent
    (a fresh clone, a worktree, every `optjournal mutate` clone) -- which is exactly
    where a silent write regression would go unmeasured. The sibling assertion on
    `populated` stays as it is; this one runs everywhere.
    """
    db = tmp_path / "j.db"
    with open_journal(db):
        pass  # migrate a journal into existence; the endpoint needs no rows
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        _, added = _post(base, "/api/watchlist", {
            "symbol": "amd", "note": "a note", "earnings_on": "2026-08-27"})
        assert added["symbol"] == "AMD"
        assert _watch_row(db) == {"note": "a note", "earnings_on": "2026-08-27"}

        # The add form sends neither key, so neither is touched.
        _post(base, "/api/watchlist", {"symbol": "AMD"})
        assert _watch_row(db) == {"note": "a note", "earnings_on": "2026-08-27"}, (
            "a bare re-add blanked a typed field: absence must mean 'leave alone'"
        )

        _, cleared = _post(base, "/api/watchlist", {"symbol": "AMD", "note": ""})
        assert cleared["ok"] is True
        assert _watch_row(db)["note"] is None, (
            "an explicit empty note must clear it -- the endpoint answered ok and "
            "kept the old value, which is the hole this closes"
        )
        assert _watch_row(db)["earnings_on"] == "2026-08-27", (
            "clearing one field rewrote another the request never mentioned"
        )

        # And the date clears the same way, since the reader can un-record one.
        _post(base, "/api/watchlist", {"symbol": "AMD", "earnings_on": "   "})
        assert _watch_row(db)["earnings_on"] is None, (
            "whitespace is not a date: a field emptied in a text box has to clear "
            "rather than store spaces the reader cannot see or delete"
        )


def test_a_refused_date_writes_nothing_at_all(tmp_path):
    """A 400 leaves the row as it was, including the fields that WERE valid.

    One request is one edit. Writing the acceptable half of a refused body would
    leave the reader looking at a row that half-took, with an error message about
    the other half -- and no way to tell which half landed.
    """
    db = tmp_path / "j.db"
    with open_journal(db):
        pass
    with web.serve_ephemeral(db_path=db, archive_dir=tmp_path / "raw") as base:
        _post(base, "/api/watchlist", {"symbol": "AMD", "note": "keep me"})
        status, payload = _post(base, "/api/watchlist", {
            "symbol": "AMD", "note": "and this", "earnings_on": "27/08/2026"})
    assert (status, payload["kind"]) == (400, "date")
    assert _watch_row(db) == {"note": "keep me", "earnings_on": None}


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
    from optjournal.replay import _snapshot_leg, _strikes_of

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
    from optjournal.replay import ReplayLeg, _strikes_of

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
    from optjournal.replay import ReplayLeg, _strikes_of

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
    # A journal where nothing has happened has no first day, so there is no day
    # to be the Nth of. Null rather than 1: the header drops the dateline
    # entirely, which is honest, where "log day 1" would date a log that was
    # never opened.
    assert state["logbook"] == {"opened": None, "day": None}
    json.dumps(state)


def test_the_logbook_dates_from_first_activity_counting_inclusively(populated):
    """The header's dateline, against the real archive.

    Two things are worth pinning. The opening day is day ONE, not day zero -- a
    log's first page is page one, and an off-by-one here is visible on the page's
    most prominent small line. And `opened` is normalised to ISO, because the
    stored columns mix `2025-01-14 14:30:05` with IBKR's compact `20250114` and
    the page slices it apart to format the date.
    """
    from datetime import date, timedelta

    from optjournal.serialize import logbook_data

    conn = connect(populated)
    try:
        opened = logbook_data(conn, today=date(2030, 1, 1))["opened"]
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", opened or ""), (
            f"opened is not an ISO day: {opened!r} -- the page splits it on '-' "
            f"to format the dateline, so a compact IBKR date renders as nonsense"
        )
        start = date.fromisoformat(opened)
        # Read on its own opening day, the log is on day 1.
        assert logbook_data(conn, today=start)["day"] == 1
        assert logbook_data(conn, today=start + timedelta(days=1))["day"] == 2
        # And a clock BEHIND the archive cannot produce day 0 or a negative day.
        # Not hypothetical: statements carry exchange-local timestamps, so a
        # reader west of the exchange can hold a fill dated tomorrow.
        assert logbook_data(conn, today=start - timedelta(days=5))["day"] == 1
    finally:
        conn.close()


def test_the_logbook_ignores_both_filters(populated, tmp_path):
    """How long the log has been kept is a fact about the JOURNAL.

    The header displays no filter bar, and a figure that moves with a control its
    own surface does not show leaves the reader nothing to explain the change
    with -- the defect the Annual total row was fixed for. So the dateline must be
    identical under a month selection and under a trade-type scope.
    """
    plain = build_state(db_path=populated, archive_dir=tmp_path, query_id=None)
    scoped = build_state(
        db_path=populated, archive_dir=tmp_path, query_id=None,
        month=plain["month_range"][0], trade_type="odte",
    )
    assert plain["logbook"] == scoped["logbook"], (
        "the header dateline moved with a filter the header does not display"
    )
    assert plain["logbook"]["day"] >= 1


# --- the Costs tab's scope selection -----------------------------------------
#
# Four peer options on the wire, two different kinds of narrowing underneath:
# OPT/STK/CASH are asset categories, 0DTE is a subset of options resolved to
# fills. `_broker_costs` is the seam that reconciles them, so these pin the
# translation rather than the arithmetic (which `test_costs.py` owns).


def _costs(populated, selection=None):
    return build_state(
        db_path=populated, archive_dir=RAW_DIR, query_id=None,
        cost_scope=selection,
    )["broker_costs"]


def test_costs_default_to_the_journals_own_category(populated):
    """So the tab opens agreeing with every other tab.

    The account-wide figure is roughly four times larger on this journal, and a
    tab that opened on it would look like the others were wrong.
    """
    for empty in (None, [], ["", "  "]):
        assert _costs(populated, empty)["scope"]["categories"] == ["OPT"], empty


def test_cost_dates_come_from_selected_journal_rows(populated):
    """The DB report dates describe rows in scope, not statement metadata."""
    costs = _costs(populated, ["OPT", "STK", "CASH"])
    conn = connect(populated)
    try:
        expected = conn.execute(
            "SELECT MIN(trade_date), MAX(trade_date) FROM trades"
            " WHERE asset_category IN ('OPT', 'STK', 'CASH')"
        ).fetchone()
    finally:
        conn.close()
    assert costs["from_date"] == expected[0]
    assert costs["to_date"] == expected[1]

    # Statement analysis keeps the XML reporting period. The fixture's first
    # trade is later, proving the DB path did not copy that metadata.
    statement = build_state(
        db_path=populated, archive_dir=RAW_DIR, query_id=None
    )["costs"][0]
    assert statement["from_date"] != costs["from_date"]


def test_a_repeated_parameter_is_a_multi_select(populated):
    """`?cost=OPT&cost=CASH`, so neither layer invents a delimiter."""
    costs = _costs(populated, ["OPT", "CASH"])
    assert costs["scope"]["categories"] == ["CASH", "OPT"]
    assert costs["totals"]["attributable"]["base"] > 0


def test_selected_categories_add_up(populated):
    """Disjoint categories, so a multi-select is a sum -- the property that makes
    ticking two boxes meaningful."""
    opt = _costs(populated, ["OPT"])["totals"]["attributable"]["base"]
    stk = _costs(populated, ["STK"])["totals"]["attributable"]["base"]
    both = _costs(populated, ["OPT", "STK"])["totals"]["attributable"]["base"]
    assert both == pytest.approx(opt + stk)


def test_a_mixed_selection_keeps_every_billing_currency(populated):
    """Widening the scope adds columns rather than deleting exactness."""
    charged = _costs(populated, ["OPT", "CASH"])["totals"]["attributable"]["charged"]
    assert len(charged) > 1, "a multi-currency scope reported one currency"
    assert _costs(populated, ["OPT"])["totals"]["attributable"]["ccy"] == "USD"


def test_the_case_of_a_selection_does_not_matter(populated):
    """A hand-edited `#cost=opt` is the same selection as `#cost=OPT`."""
    assert (_costs(populated, ["opt", "cash"])["scope"]["categories"]
            == _costs(populated, ["OPT", "CASH"])["scope"]["categories"])


def test_selecting_0dte_alone_implies_options(populated):
    """Asking for a subset of options is asking about options.

    A fill set with no category would also admit a stock fill that happened to
    share an id -- and 0DTE is defined only for contracts with an expiry.
    """
    scope = _costs(populated, ["0DTE"])["scope"]
    assert scope["categories"] == ["OPT"]
    assert scope["subset"] == "0DTE"
    assert scope["is_subset"] is True


def test_0dte_narrows_options_rather_than_widening_them(populated):
    """It is a subset, not a peer, so ticking both cannot exceed Options alone.

    This account has no 0DTE round trips, so the honest answer here is zero --
    `test_costs.py` asserts the proper-narrowing case against a journal that has
    some.
    """
    options = _costs(populated, ["OPT"])["totals"]
    odte = _costs(populated, ["OPT", "0DTE"])["totals"]
    assert odte["attributable"]["base"] <= options["attributable"]["base"]
    assert odte["fills"] <= options["fills"]


def test_account_fees_survive_every_selection(populated):
    """They carry no asset attribution, so no selection can narrow them -- and a
    total that hid them would measure less than the tab claims."""
    figures = {
        str(sel): _costs(populated, sel)["totals"]["unattributable"]["base"]
        for sel in (None, ["OPT"], ["STK"], ["CASH"], ["OPT", "0DTE"])
    }
    assert len(set(figures.values())) == 1, figures
    assert next(iter(figures.values())) > 0


def test_the_estimate_appears_only_when_conversions_are_selected(populated):
    """The markup is a cost of converting, so charging it to a contract scope
    would attribute a currency cost to an option."""
    assert not _costs(populated, ["OPT"])["totals"]["friction"]["is_estimated"]
    with_fx = _costs(populated, ["OPT", "CASH"])["totals"]["friction"]
    assert with_fx["is_estimated"]
    assert (with_fx["total_low_base"] < with_fx["total_mid_base"]
            < with_fx["total_high_base"])


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
    assert set(state["odte"]) == {
        "cohort", "rest", "unknown_dte", "selectable", "context", "history"}
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


def test_the_month_stepper_walks_the_range_through_load():
    """A step must mutate S.month and go through load(), never a bare redraw.

    The stats are aggregated server-side per period, so a step that only redrew
    would show a month the server never filtered for. This was pinned on the
    calendar's own chevrons, which the stepper replaced: they and the old month
    dropdown were two controls writing one state, and the guard was that both
    took the same path. There is one control now and it has to take that path.
    """
    js = _js()
    assert "data-month" in js
    binding = re.search(
        r"button\[data-month\].*?b\.onclick=\(\)=>\{.*?"
        r"const m=b\.dataset\.month\|\|null;\s*"
        r"S\.month=m===\(S\.state\.month_range\|\|\[\]\)\[0\]\?null:m;load\(\);",
        js, re.S)
    assert binding, "a step must set S.month from the button and call load()"
    # A step clears the calendar's day selection: a day from the old month
    # names nothing in the new one, and the chevrons it replaced did the same.
    assert re.search(r"button\[data-month\].*?S\.calday=null;", js, re.S)


def test_the_month_stepper_disables_at_the_ends_of_the_range():
    """At All time there is nowhere back to go, and at the current month
    nowhere forward; a live button that does nothing reads as broken.

    Walks month_range -- the account's life -- and not the fills-only months
    list: with one traded month that list has one stop, and an empty month is a
    real answer rendered as honest zeros. The walk is All time first, then oldest
    to newest, which is what makes a fresh page, whose default is the CURRENT
    month as in the reference app, open with the forward button disabled.
    """
    js = _fn("renderPeriod") + _fn("monthSteps")
    assert "const range=S.state.month_range||S.state.months||[];" in js
    assert "['all'].concat([...range].reverse())" in js, \
        "the walk must start at All time and run oldest to newest"
    assert re.search(r"data-month=\"\$\{prev\?\?''\}\"\s*\$\{prev===null\?'disabled':''\}", js)
    assert re.search(r"data-month=\"\$\{next\?\?''\}\"\s*\$\{next===null\?'disabled':''\}", js)


def test_the_period_controls_appear_only_on_the_tabs_they_drive():
    """The invariant in docs/design-notes.md, in the direction easiest to break.

    "A tab's numbers change only in response to a control that tab displays."
    Moving the month and the trade type into the header made it tempting to show
    them on every tab, the way the reference app does. But Positions, Costs,
    Annual and 0DTE ignore both, so there they would be controls that move
    nothing on screen. One list names the three tabs they drive, and both
    renderers read it rather than each carrying its own.
    """
    js = _js()
    assert "const PERIOD_TABS=['dashboard','calendar','trades'];" in js
    assert "if(!PERIOD_TABS.includes(S.tab)" in _fn("renderPeriod")
    view = _fn("renderViewOptions")
    assert "const onPeriod=PERIOD_TABS.includes(S.tab);" in view
    # The trade type is offered only where it applies; the currency, which
    # restates every money tab, is offered everywhere it has somewhere to go.
    assert re.search(r"onPeriod\?`<div><div class=\"flabel\">Trade type", view)
    assert "+ccy" in view


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
    return (
        ROOT / "src" / "optjournal" / "static" / "app.css"
    ).read_text(encoding="utf-8")


def _fn(name: str) -> str:
    """One render function's source, so a pin cannot be satisfied elsewhere."""
    js = _code_only(_js())
    start = js.index(f"function {name}(")
    nxt = js.find("\nfunction ", start + 1)
    return js[start : nxt if nxt != -1 else len(js)]


def test_header_cluster_right_aligns_and_groups_its_icons():
    """`align-items` is pinned at BOTH levels, each against its own bug.

    `.hdr-actions` is a ROW: the view-options button and the two icon buttons
    side by side, sharing the header with the period, whose 26px figure sets the
    row's height. Under `stretch` the icons would grow to it; under `flex-end`
    they would sink to its baseline. `flex-start` pins them where the title is.
    (As a COLUMN this rule once wanted `flex-end`, for a different reason: the
    icons' explicit width opted out of `stretch` and landed them hard left. That
    layout is gone -- stacked, the groups set the header's height on their own
    and read as unrelated.)

    `#ccywrap` is pinned because `.ccytog` has no width, so under the default
    `stretch` it inflated to its container, leaving the rounded border running
    past the active button with dead space inside it. It used to be right-aligned
    under the header's actions and is now left-aligned in the view-options panel;
    the two values happen to match today, and the test says so rather than
    claiming a difference that no longer exists.
    """
    css = _css().replace(" ", "").replace("\n", "")
    actions = css.split(".hdr-actions{")[1].split("}")[0]
    assert "align-items:flex-start" in actions, (
        "the actions row is back to flex-end, so the icon buttons align with "
        ".ccynote and move whenever a restated total appears"
    )
    assert "flex-direction:column" not in actions, (
        "the actions are stacked again, which is the three-deep pile that set the "
        "header's height and read as three unrelated controls"
    )
    # Inside the view-options panel the toggle is left-aligned with the Trade
    # type control above it, so the value moved from flex-end to flex-start. What
    # the assertion protects did not move: `.ccytog` has no width, and under the
    # default `stretch` it inflates to its column with dead space inside the
    # border. Any explicit alignment prevents that; this one also lines it up.
    assert "align-items:flex-start" in css.split("#ccywrap{")[1].split("}")[0], \
        "the currency toggle will stretch to its column's width again"
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
    assert "chargeMo(s.commissions)" in _fn("dashboard").replace(" ", ""), \
        "the dashboard card does not use the shared helper"
    row = _fn("statsRow").replace(" ", "").replace("\n", "")
    assert "chargeMo(s.commissions)" in row, \
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
    assert "constchargeMo=mo=>mo==null?cash(null):chargeOf(mo.native,mo.ccy,mo.base);" in js
    card = _fn("dashboard").replace(" ", "").replace("\n", "")
    assert "chargeMo(s.commissions)" in card and "chargeMo(s.open_commission)" in card
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

    # The serializer half of the rule, which is what this test is really about:
    # the figure is a Money and the gate reaches it.
    #
    # The page-side assertion that used to live here named `moneyOf(T.journal_taxes)`
    # -- a read in the statement-backed Costs view, which no longer exists. The Costs
    # tab now reads the DB-backed payload, where the same rule is enforced more
    # broadly and structurally: `chargeFig` is the single display path for every
    # `Charge`, it delegates to `moneyOf`, and the payload contract guard checks
    # every key the page reads against the typedefs. So the narrow assertion was
    # retired rather than repointed at an arbitrary new call site.
    js = _code_only(_js()).replace(" ", "").replace("\n", "")
    assert "cash(T.journal_taxes" not in js, "a display site bypasses the rule"
    # `chargeFig` is that single path, and it must go through `moneyOf`.
    assert "moneyOf(ch)" in js, (
        "chargeFig no longer delegates to moneyOf, so a Charge is displayed by a "
        "second rule that can disagree with every other cash figure on the page"
    )


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
    source = (
        ROOT / "src" / "optjournal" / "static" / "format.js"
    ).read_text(encoding="utf-8")
    assert "export function strike" in source, "the strike helper is gone"
    assert "Number.isInteger(amount)" in source
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
    # reaches chargeOf directly rather than through moneyOf. Both paths ask the
    # SAME question -- `isNativeCharge` -- which is the property that stops a
    # change reaching one figure and missing another. moneyOf keeps the sign and
    # chargeOf drops it, which is the only difference between them.
    # There is now exactly ONE entry point. `legProceedsOf` existed only because
    # a leg carried its triple flat; the leaf now carries a Money too, so every
    # display site in the page goes through `moneyOf`.
    assert "legProceedsOf" not in js, "the second entry point is back"
    assert "constmoneyOf=mo=>mo==null?cash(null):isNativeCharge(mo.native,mo.ccy)" in js, \
        "moneyOf no longer asks the shared as-charged rule"
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
    kinds = [row["kind"] for row in replay_mod._annotations(lifecycle, [])]
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
    assert replay_mod._annotations(lifecycle, [])[0]["realized"] is None

    closed = {"events": [
        _event("Short put close", _leg(270, "P", "BUY", "C", 3, 2.60),
               realized_pnl={"base": 684.59, "native": 787.86, "ccy": "USD"}),
    ]}
    got = replay_mod._annotations(closed, [])[0]["realized"]
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
    open_ts = epoch_et("2026-07-24 10:35:01")
    close_ts = epoch_et("2026-08-03 09:55:23")
    marks = [[open_ts, 0.0, 0.52], [close_ts - 3600, 700.0, 0.32],
             [close_ts, 792.0, 0.0]]
    rows = replay_mod._annotations(lifecycle, marks)
    assert (rows[0]["delta_before"], rows[0]["delta_after"]) == (None, 0.52)
    assert (rows[1]["delta_before"], rows[1]["delta_after"]) == (0.32, 0.0)


def test_an_event_without_a_timestamp_is_dropped():
    """It could not be placed on the timeline, and defaulting it to the epoch
    would put it at the far left of every chart as if it happened first.
    """
    lifecycle = {"events": [
        _event("Short put", _leg(270, "P", "SELL", "O", -3, 5.24), at=None),
    ]}
    assert replay_mod._annotations(lifecycle, []) == []


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
    # The trigger clause alone, not the whole class attribute. Pinning the full
    # string made this fail the moment `statCard` grew a fifth parameter for a
    # card-level class (`.stat.lead`), which has nothing to do with tooltips --
    # the invariant here is that `tipped` is driven by `note` and by nothing else.
    assert "${note?'tipped':''}" in card, "no tooltip trigger class"
    assert 'class="stat' in card, "the tile is no longer a .stat"
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
    assert "'neg'+(String(chargeMo(s.commissions)).length>10?'sm':'')" in dash, (
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
    formatter = (
        ROOT / "src" / "optjournal" / "static" / "format.js"
    ).read_text(encoding="utf-8")
    assert '"pos signed"' in formatter and '"neg signed"' in formatter, (
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
    assert "'neg '+(String(chargeMo(s.commissions))" in stats.replace("\n", ""), (
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


def _themes() -> dict[str, dict[str, str]]:
    """Every theme block in the stylesheet, as {selector: {name: hex}}.

    Comments are stripped PER BLOCK, after splitting, and the closing brace is
    matched as `\\n}` rather than the first `}` -- both learned by getting it
    wrong. `--dim2` carries a long explanatory comment mid-block, so stripping
    comments first swallowed the declaration after it, and a `}` inside that
    comment truncated the block. Either way the key silently vanished and the
    contrast check passed by measuring nothing.
    """
    return {
        selector: {
            # Keyed WITHOUT the leading dashes, so a caller asks for "dim2".
            name.lstrip("-"): value
            for name, value in re.findall(
                r"(--[a-z0-9]+)\s*:\s*(#[0-9a-fA-F]{6})",
                re.sub(r"/\*.*?\*/", "", body, flags=re.S),
            )
        }
        for selector, body in re.findall(
            r"^(:root[^{\n]*|\[data-theme=\"[a-z]+\"\])\{(.*?)\n\}",
            _css(), flags=re.S | re.M,
        )
    }


def _lum(hex_colour: str) -> float:
    parts = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    chan = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in parts]
    return 0.2126 * chan[0] + 0.7152 * chan[1] + 0.0722 * chan[2]


def _ratio(fg: str, bg: str) -> float:
    a, b = _lum(fg), _lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def test_muted_text_meets_wcag_aa_in_every_theme_on_every_surface():
    """--dim2 measured 3.31:1 on --bg and 3.02:1 on --panel2, against the 4.5:1
    that 12px body text requires, and it dressed the footer and every
    explanatory caption -- the prose a newcomer reads first.

    Computed rather than pinned to a hex, so re-tuning a palette is free while
    regressing legibility is not -- and run over EVERY theme, because the moment
    a second palette existed this test's `re.search` was silently measuring only
    the first block in the file. A theme is a whole new set of these ratios, so a
    prettier ground that nobody can read must not be able to ship.
    """
    themes = _themes()
    assert len(themes) >= 2, (
        f"expected several theme blocks, found {sorted(themes)} -- the parser is "
        f"probably matching the wrong brace again"
    )
    for selector, palette in themes.items():
        for surface in ("bg", "panel", "panel2"):
            ratio = _ratio(palette["dim2"], palette[surface])
            assert ratio >= 4.5, (
                f"{selector}: --dim2 ({palette['dim2']}) is {ratio:.2f}:1 on "
                f"--{surface} ({palette[surface]}), below the 4.5:1 WCAG AA needs "
                f"for 12px text. Lighten --dim2 for that theme."
            )


def test_text_on_an_accent_fill_is_readable_in_every_theme():
    """A label sitting ON a filled control, which is not the same question as
    text on a surface and had never been asked.

    Asking it found two real failures the moment the palette became measurable:
    --onaccent was 4.31:1 on Leather's --accentlit2 (a defect that predated the
    themes entirely) and 4.18:1 on Oxblood's. The currency toggle's active label
    is what wears that pair.

    The gradient's DARKER stop is the binding case, since the label has to hold
    up across the whole fill rather than at its lightest point.
    """
    for selector, palette in _themes().items():
        for fg, bg in (("accentfg", "accent"), ("onaccent", "accentlit2"),
                       ("edfg", "edbg")):
            ratio = _ratio(palette[fg], palette[bg])
            assert ratio >= 4.5, (
                f"{selector}: --{fg} ({palette[fg]}) is {ratio:.2f}:1 on --{bg} "
                f"({palette[bg]}), below 4.5:1 -- that label sits directly on "
                f"that fill"
            )


def test_the_selected_tab_separates_from_an_unselected_one_in_every_theme():
    """A rail where the current destination does not stand out is a navigation bar
    that has stopped saying where you are.

    `.tab` at rest is TRANSPARENT and `.tab.on` fills with --accent, so the
    separation is the ratio between --accent and the ground the rail sits on,
    --bg. It was --panel2 while the tabs were a horizontal strip of filled
    buttons; the rail draws no fill until hover, because seven stacked panels
    read as seven cards rather than one list.

    Found by SCREENSHOT, not by arithmetic: every text-contrast figure passed
    while Admiralty's selected tab sat at 2.42:1 against its neighbours and read
    as barely selected. Leather managed 3.12 against --panel2, so 2.8 became the
    bar. Against --bg every theme clears it by more (Leather 3.43, Admiralty
    3.71, Ledger 4.00), which is the one thing the transparent rest state bought
    besides the lighter look.

    2.8 rather than any of those exactly, because this is a floor for a
    decorative separation rather than a legibility threshold, and pinning a theme
    to another theme's precise number would make retuning Leather fail
    everything else.
    """
    for selector, palette in _themes().items():
        ratio = _ratio(palette["accent"], palette["bg"])
        assert ratio >= 2.8, (
            f"{selector}: --accent ({palette['accent']}) is only {ratio:.2f}:1 "
            f"against --bg ({palette['bg']}), so a selected rail item barely "
            f"differs from an unselected one. Lighten --accent -- but check "
            f"--accentfg still clears 4.5 on it, the two pull opposite ways"
        )


def test_every_theme_declares_the_same_palette():
    """A theme is a SWAP, not a patch.

    A block that declares only some of the names inherits the rest from `:root`,
    so a half-written theme renders one palette's chrome on another's ground --
    silently, and only on whichever panels happen to use the missing names. That
    is exactly the bug the promotion pass fixed at the literal level, and it
    would walk straight back in through an incomplete block.
    """
    themes = _themes()
    base_selector = next(s for s in themes if s.startswith(":root"))
    base = set(themes[base_selector])
    assert len(base) > 30, f"the base palette looks truncated: {len(base)} names"
    for selector, palette in themes.items():
        if selector == base_selector:
            continue
        missing = base - set(palette)
        assert not missing, (
            f"{selector} does not declare {sorted(missing)}, so those names fall "
            f"through to {base_selector} and this theme renders another theme's "
            f"colours for them"
        )


def test_each_theme_is_visibly_distinct_from_the_default():
    """A THEME CAN BE WIRED PERFECTLY AND STILL SAY NOTHING, which every other
    check here is blind to.

    Measured after the first release: the theme then called "Oxblood" moved ZERO
    of its 48 colours more than 24/255 per channel away from Leather's. Parity
    passed (it declared every name), contrast passed, no literal escaped -- and it
    looked like clicking the chip had done nothing. Admiralty had the same defect
    in the two places a reader looks first: `--logo*` (the mark) and
    `--accentlit*` (the currency pill) were within 22/255 of Leather's brown, so
    the plate and the toggle stayed brown on a blue-black page. Both reported from
    the running app.

    So distinctness is a measured property now. The threshold is per-channel
    distance rather than a perceptual metric on purpose: it is crude, it needs no
    colour-science dependency, and it is enough to separate "a different palette"
    from "the same palette with rounding on it".

    KEY NAMES, not just an average: a theme could shift its ground and leave every
    control alone, which is precisely what happened. The ones checked here are the
    ones a reader identifies a theme by.
    """
    themes = _themes()
    base_selector = next(s for s in themes if s.startswith(":root"))
    base = themes[base_selector]

    def apart(a: str, b: str) -> int:
        return max(abs(int(a[i : i + 2], 16) - int(b[i : i + 2], 16))
                   for i in (1, 3, 5))

    def channel_order(colour: str) -> tuple[int, ...]:
        """Which channel dominates, as a rank. This is the HUE question.

        Absolute distance is the wrong test for a near-black ground: every --bg
        here sits within a few points of zero, so Leather's brown `#0a0806` and
        Admiralty's blue `#060910` are 10/255 apart while being obviously
        different colours. What separates them is WHICH channel leads -- red for
        the brown, blue for the slate. Measured on the ordering, so a dark surface
        is judged by its cast rather than by a distance it cannot reach.
        """
        chans = [int(colour[i : i + 2], 16) for i in (1, 3, 5)]
        return tuple(sorted(range(3), key=lambda i: -chans[i]))

    def spread(colour: str) -> int:
        """How far the leading channel sits above the trailing one.

        The saturation question, needed because DISTANCE ALONE IS NOT ENOUGH and
        the first version of this test proved it: the old Admiralty `--logo1`
        (#d9a05b) is 47/255 from Leather's #b3714a and passed comfortably, while
        the reported complaint was that the logo plate still looked brown. Both
        are brown -- one is a brighter brown. So a landmark must differ in hue or
        in saturation, not merely in brightness.
        """
        chans = [int(colour[i : i + 2], 16) for i in (1, 3, 5)]
        return max(chans) - min(chans)

    # The landmarks a reader identifies a theme by. Each must differ in CAST --
    # which channel leads -- or in how saturated it is. `--logo1` is the plate
    # behind the mark, `--accentlit1` the currency pill, `--chartline` the one
    # line the eye follows on the Performance card. All three were reported as
    # unchanged after the first release.
    landmarks = ("logo1", "accentlit1", "accent", "chartline",
                 "bg", "panel", "panel2")
    for selector, palette in themes.items():
        if selector == base_selector:
            continue
        for name in landmarks:
            mine, theirs = palette[name], base[name]
            recast = channel_order(mine) != channel_order(theirs)
            resaturated = abs(spread(mine) - spread(theirs)) > 24
            assert recast or resaturated, (
                f"{selector}: --{name} ({mine}) has the same colour cast as "
                f"Leather's {theirs} and a similar saturation, so it reads as the "
                f"same colour at a different brightness. This is a landmark a "
                f"reader identifies the theme by -- distance alone is not enough, "
                f"which is how a brown logo plate shipped on a blue-black page"
            )
        # And the palette as a whole has to move, not only the landmarks.
        shared = [n for n in palette if n in base]
        moved = sum(1 for n in shared if apart(base[n], palette[n]) > 24)
        assert moved >= len(shared) // 3, (
            f"{selector}: only {moved} of {len(shared)} colours differ from "
            f"Leather by more than 24/255 -- this reads as Leather with noise on "
            f"it rather than as a separate theme"
        )


def test_no_colour_literal_lives_outside_a_theme_block():
    """The whole theme mechanism is "every colour is a variable", and 35 hex
    literals scattered through the rules is what made a theme swap leave brown
    chrome on a blue page: the logo facets, the edition pill, calendar day
    borders, put/call, the impact dots, and every `rgba(255,255,255,...)` wash --
    which is not a neutral hairline but a dark-theme assumption with no name.

    So the rule is mechanical and this test is what makes it hold.
    """
    css = _css()
    # Everything except the theme blocks themselves.
    rules = re.sub(
        r"^(?::root[^{\n]*|\[data-theme=\"[a-z]+\"\])\{.*?\n\}", "",
        css, flags=re.S | re.M,
    )
    rules = re.sub(r"/\*.*?\*/", "", rules, flags=re.S)
    strays = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([0-9.,\s]*\)", rules)
    assert not strays, (
        f"colour literals outside the theme blocks: {sorted(set(strays))} -- each "
        f"one survives a theme swap unchanged. Give it a name in every theme "
        f"block and use var()."
    )


#: The properties the measurement scale owns. Each is a decision about SIZE or
#: SPACE, which is exactly the class of decision that wants a shared vocabulary --
#: unlike a border width (hairlines are 1px, always), a box-shadow offset (optical,
#: per surface), a width/height (content-driven), or a media-query breakpoint (a
#: device fact). Those keep their literals on purpose.
MEASURED_PROPERTIES = (
    "font-size", "border-radius", "gap", "row-gap", "column-gap",
    "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "font-weight", "letter-spacing",
)


def test_no_measurement_literal_lives_outside_the_token_block():
    """THE COLOUR RULE ABOVE, APPLIED TO SIZE AND SPACE -- and the asymmetry
    between the two is what "this does not look polished" turned out to be.

    Every colour on this page was already a named token with a test forbidding a
    literal. Measurement had neither, and the audit that prompted this found 17
    font sizes, 12 radii, 20 padding values, 15 margins, 15 gaps and 12 letter-
    spacings: 64 literals for a page with about a dozen kinds of thing on it. Nine
    of the type sizes were within half a pixel of another -- 9.5, 10.5, 11.5, 12.5
    -- which is the signature of tuning each element by itself. The values did not
    disagree about anything; they had simply never been asked to agree.

    That is a visual defect and not a tidiness one. A reader perceives rhythm from
    REPETITION, so a page on which no two elements share a measurement offers none
    to perceive: every box is individually defensible and the set reads as
    assembled rather than designed.

    No allowlist, deliberately. The conversion left exactly zero literals in these
    properties, so the check is absolute -- and an absolute check is the only kind
    that survives contact with a hurry. `padding:1px` on two badges was the last
    holdout and became `--s0` (2px), a difference no reader can see, in exchange
    for a rule with no exceptions in it.
    """
    css = _css()
    # The token block itself is where the literals are SUPPOSED to be.
    body = css.split("*{box-sizing:border-box}", 1)
    assert len(body) == 2, "the token block's anchor moved; this test cannot find it"
    rules = re.sub(r"/\*.*?\*/", "", body[1], flags=re.S)

    props = "|".join(re.escape(p) for p in MEASURED_PROPERTIES)
    strays = []
    for found in re.finditer(rf"(?<![-\w])({props})\s*:\s*([^;}}]+)", rules):
        prop, value = found.group(1), found.group(2).strip()
        # A bare unit-ed number, or a raw font-weight keyword number.
        if re.search(r"(?<![\w.-])\d+(?:\.\d+)?(px|em|rem)(?![\w-])", value) or (
            prop == "font-weight" and re.fullmatch(r"\d{3}", value)
        ):
            strays.append(f"{prop}:{value}")
    assert not strays, (
        f"measurement literals outside the token block: {sorted(set(strays))} -- "
        f"each one is a size nothing else on the page shares. Pick the step it "
        f"belongs to (--t*, --s*, --r*, --w-*, --tr-*) and use var()."
    )


def test_every_figure_face_rule_also_asks_for_tabular_figures():
    """MONOSPACE GAVE COLUMN ALIGNMENT FOR FREE. `--fig` does not.

    In a monospace font every glyph is one cell wide, so a column of numbers lines
    up whether or not anyone asked for it. `--fig` is the proportional UI sans, and
    there a `1` is 12.25px against a `0` at 16.63px -- so the same column goes
    ragged unless the rule also says `font-variant-numeric:tabular-nums`, which
    pins every digit to 16.401px.

    Not hypothetical: the swap left SIX rules without it, and two of them are grids
    where the misalignment would have been the first thing a reader saw -- the
    calendar's per-day P&L (`.day .dpl`, a 7-wide grid) and the performance chart's
    axis labels (`.chart .axis`, stacked vertically). `.wdelta`, `.tk`, `.wmend`
    and `.wshow` were the others. Every one of them had been correct while it was
    monospace and silently stopped being correct on the same line that improved it,
    which is the most expensive kind of change there is.

    Punctuation is deliberately NOT pinned: `tabular-nums` leaves the comma and the
    full stop narrow, and that is the entire reason the figures moved off the code
    face. See `--fig` in app.css.
    """
    css = re.sub(r"/\*.*?\*/", "", _css(), flags=re.S)
    ragged = [
        selector.strip()
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        if "var(--fig)" in body
        and "font-family" in body
        and "tabular-nums" not in body
    ]
    assert not ragged, (
        f"these rules set the proportional figure face without asking for tabular "
        f"figures, so their digits are variable-width and any column of them goes "
        f"ragged: {ragged}. Monospace made this free; --fig does not."
    )


def test_the_two_faces_are_declared_once_and_mean_different_things():
    """`--mono` used to be declared THREE times -- once in every theme block --
    which said a typeface is part of a palette. It is not: all three themes carried
    the identical stack, so a palette swap changed nothing about it, and the only
    effect was that adding a second face meant editing three places or forgetting
    to. Both faces now sit in the token block beside the type scale, declared once.

    The two must also stay DISTINCT. Collapsing them is the tidy-up that undoes the
    whole distinction: `--fig` measures, `--mono` quotes, and if they resolve to the
    same stack then an order id and a price look identical again and nothing on the
    page says which is which.
    """
    css = _css()
    for token in ("--mono", "--fig"):
        declarations = re.findall(rf"^\s*{token}\s*:", css, flags=re.M)
        assert len(declarations) == 1, (
            f"{token} is declared {len(declarations)} times; both faces belong in "
            f"the `html` token block once, not in the theme blocks -- a typeface is "
            f"not part of a palette"
        )
    block = css.split("html{", 1)[1].split("\n}", 1)[0]
    mono = re.search(r"--mono:\s*([^;]+);", block)
    fig = re.search(r"--fig:\s*([^;]+);", block)
    assert mono and fig, "both faces must be declared in the token block"
    assert "monospace" in mono.group(1), (
        "--mono is no longer a monospace stack, so the identifiers it dresses "
        "(order ids, tickers, paths) have lost the fixed cell that makes them "
        "scannable character by character"
    )
    assert "monospace" not in fig.group(1), (
        "--fig resolves to a monospace stack, which reintroduces the defect it "
        "exists to fix: a monospace comma is a full digit wide, so it punches a "
        "hole in every thousands-separated figure on the page"
    )
    assert mono.group(1).strip() != fig.group(1).strip(), (
        "the two faces are the same stack, so nothing distinguishes a quantity "
        "from a literal any more"
    )


def test_a_numeric_table_cell_carries_the_figure_face_from_its_own_class():
    """42 markup sites depend on this one rule.

    They used to read `class="n mono"`, naming the face at every call site. `.n`
    already meant "numeric cell" -- it is why the rule right-aligns -- so the face
    belongs with the class, and carrying it here is what let all 42 drop the `mono`
    rather than swap it for a `fig`. If this rule loses the family, every numeric
    column on Positions, Costs and Annual silently falls back to the body font with
    proportional digits.
    """
    css = _css().replace(" ", "").replace("\n", "")
    rule = css.split("td.n,th.n{")[1].split("}")[0]
    assert "font-family:var(--fig)" in rule, (
        "numeric table cells no longer carry the figure face, and the markup no "
        "longer names it either -- so they inherit the body font"
    )
    assert "tabular-nums" in rule, "numeric columns will go ragged"
    assert "text-align:right" in rule, "numeric columns are no longer right-aligned"
    # And the markup must NOT have gone back to naming it per site.
    assert 'class="nmono"' not in page_html().replace(" ", ""), (
        "`class=\"n mono\"` is back, which puts the code face on quantities again"
    )


def test_every_heading_level_the_markup_uses_is_sized_by_the_stylesheet():
    """THE ONE WAY ONTO THE PAGE THE LITERAL CHECK CANNOT SEE: inherit it.

    A stylesheet with no size literals outside the token block still renders an
    off-scale size if an element is left unstyled, because the user agent has an
    opinion about headings. `h3` defaults to 1.17em -- 16.38px here -- plus 1em
    margins top and bottom, and none of that is a literal anywhere in this repo.

    Found twice, which is why it is pinned rather than fixed again. The Watchlist's
    title was a classless `<h3>` doing exactly this; the fix changed that one tag to
    `<h2>` and left the Market tab's two `<h3>`s untouched, so "Economic Calendar"
    went on rendering at 16.38px -- measured on the running page, the only size on
    any of the nine tabs that was not on the scale. Sizing the ELEMENT ends it;
    changing a tag moves it.
    """
    markup = page_html() + (
        ROOT / "src" / "optjournal" / "companion.html"
    ).read_text()
    # Templates build tags in JS too, so match the opening tag anywhere.
    used = {tag for tag in ("h1", "h2", "h3", "h4", "h5", "h6")
            if re.search(rf"<{tag}[\s>]", markup)}
    assert used, "no headings found at all; this test has lost its subject"

    # The rule must be BROAD: the bare element, or the element inside `.card`,
    # which is the wrapper every panel on this page uses. A narrowly scoped rule
    # does not count, and the first version of this test accepted one -- it asked
    # only whether SOME selector ended in the tag, which `.jsec h3` satisfied while
    # applying inside one editor pane. Ablating `.card h3` then left the test green
    # with the Market tab's headings back on the user agent's size, which is the
    # precise bug. Checked by ablation now.
    unsized = []
    for tag in sorted(used):
        sized = any(
            re.fullmatch(rf"(?:\.card\s+)?{tag}", one.strip()) and "font-size" in body
            for selector, body in _css_rules()
            for one in selector.split(",")
        )
        if not sized:
            unsized.append(tag)
    assert not unsized, (
        f"the markup uses {unsized} and no BROAD rule (`{unsized[0]}` or "
        f"`.card {unsized[0]}`) sets a font-size, so an unclassed one wears the "
        f"user agent's em-relative size and 1em margins -- an off-scale size that "
        f"no literal in this repo can be searched for. A rule scoped to one "
        f"container does not count; that is how this bug survived its first fix."
    )


def test_the_token_block_is_not_parsed_as_a_theme():
    """`html{}`, not a second `:root{}`, and the reason is this file's own tests.

    `_themes()` matches `^:root...{...}` and reads every block it finds as a
    palette. A second `:root` block would therefore be collected as a theme that
    declares none of the colour names, and
    `test_every_theme_declares_the_same_palette` would fail on it -- for a block
    that has nothing to do with colour. `html` IS the root element, so the cascade
    is identical and the colour tests stay about colour.

    Pinned because the edit that breaks it is a tidy-up: `:root` reads as more
    idiomatic than `html` and swapping them looks free.
    """
    css = _css()
    assert re.search(r"^html\{", css, flags=re.M), (
        "the measurement tokens are no longer declared on `html`; if they moved to "
        "a second `:root` block, `_themes()` now reads them as an empty palette"
    )
    assert "--t0:" in css.split("html{", 1)[1].split("\n}", 1)[0], (
        "the type scale is not inside the `html` block"
    )
    # The safety net for the above, stated as the thing that actually matters.
    assert all(pal for pal in _themes().values()), (
        f"a theme block parsed as empty, so the palette-parity check is measuring "
        f"nothing: {[sel for sel, pal in _themes().items() if not pal]}"
    )


def _toplevel_rules() -> list[tuple[str, str]]:
    """(selector, body) for rules OUTSIDE any at-rule, comments stripped.

    Nested at-rules are excluded rather than flattened, and BOTH kinds here have a
    legitimate reason to repeat a selector:

    - `@media` is a responsive override. `.stats` setting `grid-template-columns`
      in the base and again at 1180px is the mechanism working, not a collision.
    - `@keyframes` names its own steps. `sheen` and `spin` both have a `to` block
      setting `transform`, which are two unrelated animations rather than one
      overriding the other -- caught as a false positive on the first run of the
      duplicate check, which is why the strip is by at-rule rather than by `@media`.
    """
    css = re.sub(r"/\*.*?\*/", "", _css(), flags=re.S)
    # Any at-rule whose block contains nested rules, of any depth-1 shape.
    css = re.sub(r"@[a-z-]+[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", css, flags=re.S)
    return re.findall(r"([^{}]+)\{([^{}]*)\}", css)


def test_no_two_rules_claim_the_same_selector_and_the_same_property():
    """THE `.jrow` COLLISION, and it shipped green because nothing here looked.

    Two features both named their row `.jrow`: the Collection strip (a six-column
    grid, app.css `the Jobs strip`) and the journal write-up row 550 lines below.
    Both rules were individually valid, so every other check passed -- including
    `test_no_rule_sets_a_layout_property_its_display_mode_cannot_use`, which reads
    one rule at a time and saw a `display:grid` sitting beside its own
    `grid-template-columns`.

    What it could not see is that the LATER rule set `display:flex`. So the strip
    computed flex, its six fixed columns were inert, and the status word landed at
    five different x-positions down the panel -- 314, 356, 375, 386, 409 at 1440px.
    Every row also took the journal rule's `border-top`, including the first, which
    is exactly the defect the comment above the grid claims to have fixed. A
    comment describing a layout the page does not have is worse than no comment.

    The invariant is the general one: if two rules at the same level share a
    selector AND a property, the earlier declaration is dead, and a dead
    declaration is either a bug or a line to delete. Augmenting a selector with
    DIFFERENT properties stays legal -- `.zlad th` does it twice, once for padding
    and once for type -- because nothing is silently lost.
    """
    seen: dict[str, dict[str, str]] = {}
    clashes = []
    for selector, body in _toplevel_rules():
        for one in (s.strip() for s in selector.split(",")):
            if not one:
                continue
            props = {
                m.group(1)
                for m in re.finditer(r"(?:^|;)\s*([-a-z]+)\s*:", body)
            }
            overlap = props & set(seen.get(one, {}))
            if overlap:
                clashes.append(
                    f"`{one}` sets {sorted(overlap)} twice; the first is dead"
                )
            seen.setdefault(one, {}).update(dict.fromkeys(props, body))
    assert not clashes, (
        "two rules claim the same selector and the same property, so the earlier "
        f"declaration never renders: {clashes}. This is the shape of the `.jrow` "
        f"bug -- if the two are different features, rename one."
    )


def test_the_stylesheet_has_no_text_loose_outside_a_rule():
    """A COMMENT THAT DOES NOT CLOSE TAKES THE REST OF THE FILE WITH IT, quietly.

    Found by making it: a paragraph was added to the scoreboard's explanation and
    landed one line BELOW the `*/` instead of above it. The browser then hit prose
    where a selector belonged, discarded declarations until it resynchronised, and
    `.stats` lost its `grid-template-columns` -- so the dashboard rendered as a
    single column at 1440px. No test failed. The stylesheet still parsed as a
    string, still contained every rule as text, and every assertion that searches
    it with `in` still passed, because the bytes were all present.

    This file is 1500 lines of prose and rules interleaved, which is a deliberate
    choice that earns its keep -- and it makes this failure mode routine rather
    than exotic, so it wants a check of its own.
    """
    css = _css()
    assert css.count("/*") == css.count("*/"), (
        f"unbalanced CSS comments: {css.count('/*')} openers, {css.count('*/')} "
        f"closers -- everything after the unclosed one is being parsed as prose"
    )
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    # Whatever sits between one rule's `}` and the next rule's `{` is a selector,
    # and a selector cannot contain a full stop followed by a space, or a semicolon.
    for chunk in re.split(r"\}", stripped):
        head = chunk.split("{", 1)[0]
        if "{" not in chunk and not head.strip():
            continue
        assert ";" not in head, (
            f"a declaration is sitting outside any rule, which means a comment "
            f"above it did not close: {head.strip()[:120]!r}"
        )
        assert not re.search(r"\.\s|\bthe\b|\bbecause\b", head), (
            f"prose is sitting where a selector belongs, so a comment above it did "
            f"not close: {head.strip()[:120]!r}"
        )


def test_no_colour_literal_lives_in_the_page_either():
    """THE STYLESHEET IS NOT THE ONLY PLACE A COLOUR CAN HIDE, and checking only
    `app.css` is why three visible things sat out the first theme release.

    Reported from the running app, not found by a test: the performance chart
    never changed colour, and its dots were ringed in `#0a0806` -- Leather's own
    BACKGROUND, hardcoded into an SVG `stroke` attribute. `note()` set
    `style.background` and `style.color` from a literal table, and an inline style
    outranks every rule, so no theme could have repainted the banner even with the
    right variable in place.

    Both are invisible to the CSS check by construction: an SVG presentation
    attribute and a `style.foo =` assignment are colours the stylesheet cannot
    see. So the page is scanned too, and the fix in each case was to move the
    decision into a class.

    `url(#gpos)` and `fill="none"` are not colours and stay; the pattern only
    matches hex and rgb().
    """
    page = _code_only(page_html())
    strays = re.findall(r"#[0-9a-fA-F]{6}\b|rgba?\([0-9.,\s]+\)", page)
    assert not strays, (
        f"colour literals in page.html: {sorted(set(strays))} -- a hex in an SVG "
        f"attribute or an inline style cannot be themed, and an inline style "
        f"cannot even be overridden from app.css. Move it to a class."
    )


def test_every_control_has_a_visible_keyboard_focus_ring():
    """There was none: .tab and .icobtn both computed outline-style:none, so
    tabbing through the page left no way to see where you were, and <select>
    wore Chrome's default blue -- the only off-palette colour on the page.

    Declared once for `:focus-visible` rather than per control, so a button
    added later is reachable by default instead of by remembering.
    """
    css = _css().replace(" ", "").replace("\n", "")
    # --accent, formerly --leather1: the palette names say ROLE now that a theme
    # can repaint them, and a ring hardcoded to one theme's brown would be
    # invisible against another theme's ground.
    assert ":focus-visible{outline:2pxsolidvar(--accent)" in css, (
        "the global focus ring is gone, so keyboard users cannot see focus"
    )
    # :focus-visible, not :focus -- otherwise a mouse click leaves a ring that
    # reads as a stuck selection.
    assert ".tab:focus-visible,.icobtn:focus-visible{outline-offset:-2px}" in css, (
        "the inset offset is gone; these two sit flush to a panel edge, where "
        "an outset ring is clipped"
    )


#: Every `.stats` grid, as {modifier class or "" : the tile counts it can be
#: given}. The dashboard's is a SET because the reader chooses how many tiles to
#: show; the others are fixed. The divisibility check reads every count listed,
#: and the dashboard's set is pinned against the page's own `TILE_STEP` below.
STATS_GRIDS = {"": (4, 8, 12, 16, 20), "c3": (3,), "c2": (4,)}

#: Columns per modifier at each breakpoint, widest first. Read off the stylesheet
#: by the test rather than trusted, so a retune cannot drift from this table.
#: A modifier absent from a query keeps its wider count through it, which is why
#: these tuples differ in length: `c3` is deliberately not named at 1180px, since
#: three tiles in two columns is an orphan. Every tuple must still END at 1.
STATS_COLUMNS = {
    "": (4, 2, 1),
    "c3": (3, 1),
    "c2": (2, 1),
}


def test_the_scoreboard_is_a_grid_whose_columns_every_tile_count_divides():
    """THE ARITHMETIC THAT ONCE FORBADE A GRID, now satisfied instead of dodged.

    History, because the obvious edit here is a regression. `.stats` was
    `repeat(5,1fr)`, became `display:flex;flex-wrap:wrap;--sw:18%`, and the flex
    was CORRECT for its inputs: the dashboard emitted nine cards, nine divides
    evenly into none of 5/3/2, and every fixed grid orphaned a cell in the wide
    view. `flex:1 1 <basis>` let the last row absorb the remainder and end flush.

    What it could not fix is that wrapped flex rows size INDEPENDENTLY. Measured at
    1440px, the nine came out as five tiles of 260px above four of 329px, and not
    one column edge lined up down the panel. A flush right edge, bought with the
    interior alignment -- which is most of what read as unpolished on this tab.

    So the premise went instead. First Net P&L spanned the first row as a lead
    tile, leaving eight; then the header took over the headline and the tiles
    became peers the reader orders and chooses. What keeps the grid gapless now
    is the COUNT: a multiple of four, every one of which divides 4, 2 and 1, so the
    columns align at every breakpoint AND no cell is empty, which the flex row
    could not do at the same time.

    Checked as arithmetic over the real stylesheet, not as a spelling: the column
    counts are parsed out of the CSS, so retuning a breakpoint is free and
    introducing an orphan is not.
    """
    css = _css().replace(" ", "").replace("\n", "")
    assert ".stats{display:grid" in css, (
        "the scoreboard is a flex row again, so its wrapped rows will size "
        "independently and the columns will not line up"
    )
    assert ".stat.lead" not in css, (
        "a lead tile spans the first row again, so the reader's order has "
        "nowhere to start and the header's period figure has a duplicate"
    )

    for mod, cols in _stats_column_counts().items():
        name = f".stats{'.' + mod if mod else ''}"
        assert cols == STATS_COLUMNS[mod], (
            f"`{name}` now runs {cols} columns across the breakpoints, not "
            f"{STATS_COLUMNS[mod]}"
        )
        for tiles in STATS_GRIDS[mod]:
            for count in cols:
                assert tiles % count == 0, (
                    f"`{name}` can hold {tiles} tiles in {count} columns, which "
                    f"leaves {tiles % count} empty cell(s) -- the orphan the flex "
                    f"row existed to avoid"
                )
        assert cols[-1] == 1, (
            f"`{name}` never reaches one column, so on a phone its tiles stay "
            f"{cols[-1]} across and the figures inside them wrap"
        )


def _stats_column_counts() -> dict[str, tuple[int, ...]]:
    """Columns per `.stats` modifier, widest breakpoint first, read off the CSS.

    A bare `repeat(n,1fr)` is n columns and `1fr` is one. Media queries are taken
    in source order, which is how the stylesheet is written (widest first), and
    each one must name every modifier explicitly -- `.stats.c3` is specificity
    (0,2,0) and outranks a bare `.stats` whatever the query, so a rule that
    forgets to list it leaves a three-column strip three columns on a phone. That
    requirement is what makes this parse well defined.
    """
    css = _css()
    counts: dict[str, list[int]] = {mod: [] for mod in STATS_GRIDS}

    def columns(decl: str) -> int:
        found = re.search(r"repeat\((\d+),1fr\)", decl.replace(" ", ""))
        return int(found.group(1)) if found else 1

    # Base rules first, then each media block in order.
    blocks = [css.split("@media")[0]] + [
        "@media" + part for part in css.split("@media")[1:]
    ]
    for block in blocks:
        for mod in counts:
            selector = rf"\.stats{re.escape('.' + mod) if mod else ''}\b"
            # The selector may appear in a comma list; take the rule it belongs to.
            for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}",
                                        re.sub(r"/\*.*?\*/", "", block, flags=re.S)):
                if "grid-template-columns" not in body:
                    continue
                if any(re.fullmatch(selector, one.strip())
                       for one in sel.split(",")):
                    counts[mod].append(columns(body))
                    break
    return {mod: tuple(seen) for mod, seen in counts.items()}


def _tile_registry() -> tuple[list[str], list[str], int]:
    """(every tile key in TILES order, the renderer keys in dashboard(), TILE_STEP)."""
    js = _js()
    table = js.split("const TILES=[", 1)[1].split("];", 1)[0]
    keys = re.findall(r"\['([a-z_]+)','", table)
    renderers = re.findall(r"^\s{4}([a-z_]+):\(\)=>", _fn("dashboard"), re.M)
    step = int(re.search(r"const TILE_STEP=(\d+);", js).group(1))
    return keys, renderers, step


def test_every_tile_the_chooser_offers_has_a_renderer_and_none_is_orphaned():
    """TILES lists what Settings offers; `dashboard()` holds what can render.

    They are two tables because the chooser has no payload to render against, and
    two tables drift: a key offered with no renderer throws on the dashboard the
    moment a reader ticks it, and a renderer nothing offers is a tile no reader
    can ever see. Both directions, same as the payload guard.
    """
    keys, renderers, _ = _tile_registry()
    assert len(keys) == len(set(keys)), f"a tile key is listed twice: {keys}"
    assert sorted(keys) == sorted(renderers), (
        f"offered without a renderer: {sorted(set(keys) - set(renderers))}; "
        f"renderable but never offered: {sorted(set(renderers) - set(keys))}"
    )


def test_the_tile_counts_the_page_allows_are_the_ones_the_grid_divides():
    """The page's own step, the default and the full set, against STATS_GRIDS.

    `TILE_STEP` is what the chooser enforces and what the server validates, and
    every count it permits has to be a count the grid's columns divide. Read out
    of the page rather than restated, so changing the step without rechecking the
    grid fails here instead of shipping a hole. The default must be one of those
    counts, and so must showing everything, since "tick them all" is the first
    thing a reader tries.
    """
    keys, _, step = _tile_registry()
    allowed = tuple(range(step, len(keys) + 1, step))
    assert allowed == STATS_GRIDS[""], (
        f"the page permits {allowed} visible tiles but the grid is checked for "
        f"{STATS_GRIDS['']} -- update STATS_GRIDS and recheck the divisibility above"
    )
    default = int(re.search(r"const TILE_DEFAULT=TILES\.slice\(0,(\d+)\)", _js()).group(1))
    assert default in allowed, f"the default shows {default} tiles, not a multiple of {step}"
    assert len(keys) in allowed, (
        f"all {len(keys)} tiles shown leaves a hole; the full set must be a multiple "
        f"of {step} too"
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
    # NO break-even rule. It was a labelled dashed line at zero, removed by
    # request -- and the sign information it carried is stated twice over by the
    # things already on the card: the fill is clipped at zero and painted in the
    # loss colour below it, and a dot below zero takes that colour too. Asserted
    # as an absence so it cannot drift back in alongside those.
    assert "break even" not in chart, (
        "the break-even rule is back on the performance chart; the clipped fill "
        "and the per-dot sign colour already say which side of zero the series is"
    )
    assert 'class="zero"' not in chart, "the zero rule is back"


def test_the_performance_chart_scales_uniformly():
    """A non-uniform SVG scale stretches every GLYPH, and that is what made this
    axis look blurry.

    The chart was authored at `viewBox="0 0 1000 230"` with
    `preserveAspectRatio="none"` and `width:100%;height:230px`. The card is about
    1400px wide, so the box was scaled 1.40x horizontally and 1.00x vertically:
    measured, every digit came out 40% wider than tall, and strokes landed on
    fractional pixels. Not a font problem, a geometry one -- which is why no
    amount of font tuning would have fixed it.

    The replay panel next door always used `xMidYMid meet`, which is why its
    labels were crisp while these were not.
    """
    chart = _fn("chart")
    assert 'preserveAspectRatio="none"' not in chart, (
        "the performance chart is scaling non-uniformly again, which stretches "
        "its type -- the axis will look blurry at any card width but 1000px"
    )
    css = _css().replace(" ", "").replace("\n", "")
    assert "height:auto" in css.split(".chart{")[1].split("}")[0], (
        "`.chart` pins a height again; with width:100% that forces a non-uniform "
        "scale unless the viewBox happens to match the card exactly"
    )


def test_the_performance_axis_labels_round_numbers_in_display_currency():
    """Two decisions, and the ORDER of them is what makes it correct.

    The ticks were `[hi, (hi+lo)/2, lo]` -- the data's own padded extremes -- so
    the axis read "€1,626 / €726 / −€174": numbers nobody chose, moving on every
    fill, and never including zero. `niceTicks` (unit-tested in
    tests/frontend/replay.test.mjs) picks round multiples instead.

    AND THE ROUNDING HAPPENS IN DISPLAY SPACE. Choosing round BASE values and
    labelling them through `cash` would have rounded the wrong quantity: at 1.13
    USD per EUR a tidy €500 tick renders "$568.63", so under a non-base toggle
    the axis would have looked exactly as arbitrary as before. So the domain is
    converted, ticks are chosen there, and `unconv` maps them back to position.
    """
    chart = _fn("chart")
    assert "niceTicks(conv(lo),conv(hi))" in chart.replace(" ", ""), (
        "the axis no longer picks round ticks from the CONVERTED domain, so a "
        "round tick under one currency is an arbitrary one under another"
    )
    assert "unconv(" in chart, (
        "the display-space ticks are not mapped back to base, so they are "
        "positioned on the wrong scale"
    )
    js = _code_only(_js()).replace(" ", "")
    assert "constunconv=" in js, "the inverse of conv is gone"


def test_the_chart_axis_dates_are_short_and_unambiguous():
    """`2026-07-24` is ten characters, four of them punctuation and four a year
    the card's own title already establishes.

    Day-then-month rather than DD/MM/YY: a purely numeric date reads two ways
    (07/08 is August 7th to half the world), and the header dateline and month
    picker already spell the month, so this reuses the page's vocabulary instead
    of introducing a fourth date format.

    The year returns only when the span crosses one -- noise on a two-week chart,
    load-bearing on one running from December into January.
    """
    source = (
        ROOT / "src" / "optjournal" / "static" / "format.js"
    ).read_text(encoding="utf-8").replace(" ", "")
    assert "exportfunctiondayLabel" in source, "the short date formatter is gone"
    chart = _fn("chart").replace(" ", "")
    assert "dayLabel(pt.x,spansYears)" in chart, (
        "the axis is not using the short date formatter"
    )
    assert "spansYears=" in chart, (
        "the year is now unconditional -- either always noise or always missing, "
        "and one of those makes a December-to-January axis lie about its own order"
    )


def test_only_a_success_banner_dismisses_itself():
    """A success has been read the instant it appears; a failure has not.

    "market ok -- 74 fetched, 74 stored" used to sit above the calendar until the
    next note replaced it or the page reloaded, claiming the reader's attention
    forever for something already absorbed. Reported from the running app.

    THE ASYMMETRY IS THE POINT AND MUST NOT BE FLATTENED. An error that
    disappears on a timer is an error nobody saw -- the reader who looked away is
    exactly the reader who needed it -- so `bad` and `warn` stay until something
    replaces them.
    """
    note = _fn("note")
    assert "if(k!=='ok')return" in note.replace(" ", ""), (
        "every kind now self-dismisses, so a failure can vanish before it is "
        "read -- only `ok` may be on a timer"
    )
    # The timer is cleared on EVERY call, before the branch: an ok followed by a
    # bad inside the window would otherwise let the first note's timer fire and
    # hide the error mid-read.
    body = note.replace(" ", "").replace("\n", "")
    assert body.index("clearTimeout") < body.index("if(k!=='ok')return"), (
        "the pending timer is not cleared before the kind is checked, so an `ok` "
        "followed by a `bad` lets the stale timer hide the error"
    )
    css = _css().replace(" ", "").replace("\n", "")
    # The fade needs the element to STAY displayed while opacity animates.
    assert "#msg.show.fading{opacity:0}" in css, (
        "the fade-out state is gone; dropping `.show` instead sets display:none "
        "in the same frame and the transition never runs"
    )


def test_an_empty_loss_population_reads_as_a_fact_not_a_missing_number():
    """`—` on Avg Loss with zero losing trades reads as "failed to load". There
    have been no losses, which is information; the card should say so.

    Only when the population is genuinely empty AND something has closed, so a
    real average loss still renders as a number and a journal with nothing
    closed still shows the em-dash it should.
    """
    body = _fn("dashboard")
    assert "s.losses===0&&s.decided_campaigns>0" in body.replace(" ", ""), (
        "the empty-population case is gone, so Avg Loss shows a bare em-dash "
        "again when there are no losses"
    )
    assert "none yet" in body, "the replacement text is gone"
    css = _css().replace(" ", "").replace("\n", "")
    assert ".stat.v.nil{" in css, (
        "the nil styling is gone, so prose renders at a figure's size and reads "
        "as a value"
    )


def test_the_kicker_carries_no_hardcoded_line():
    """The header's most prominent small slot must say something only THIS
    journal can say, and two static strings failed that in turn.

    `Cuaderno de Bitácora` restated the title directly beneath it. `Strikes ·
    Fills · Round trips` then named the journal's contents -- true, but identical
    for every reader on every load, and already said by the tab strip two lines
    down. It is now a dateline computed from the payload (`serialize.logbook_data`
    plus the open book), so the assertion is that the slot ships EMPTY: any text
    baked in here is either a placeholder that flashes before the real line, or a
    regression to a fixed string.
    """
    # The rendered element, not the whole file: the comment beside it names both
    # rejected strings on purpose, to say why each was rejected.
    match = re.search(r'<div class="kicker" id="kicker">([^<]*)</div>', page_html())
    assert match, "the header kicker slot is gone"
    assert not match.group(1).strip(), (
        f"the kicker ships with hardcoded text {match.group(1)!r}; it is filled "
        f"from the payload, and baked-in text either flashes before the real "
        f"line or is a fixed string nobody reads twice"
    )
    body = code_only(page_html()).replace(" ", "")
    assert "renderKicker()" in body, (
        "nothing fills the kicker slot, so the header's top line renders blank"
    )


def test_the_kicker_names_underlyings_rather_than_counting_contracts():
    """A count here would contradict the Positions tab.

    An episode is per CONTRACT, so a strangle is two of them against one card on
    Positions -- the disagreement `strategies.open_position_count` exists to end.
    The header sidesteps it by naming UNDERLYINGS: two legs on GOOG are one GOOG
    however they are grouped. This pins that it reads `underlying_symbol` from
    the same snapshot rows that tab renders, and that it de-duplicates them.
    """
    body = code_only(page_html())
    line = re.search(r"function logbookLine\(\)\{(.*?)\n\}", body, re.S)
    assert line, "logbookLine is gone"
    src = line.group(1)
    assert "underlying_symbol" in src, (
        "the kicker no longer names underlyings, so a two-leg strangle can read "
        "as two positions and disagree with the Positions tab"
    )
    assert "new Set(" in src, (
        "the names are no longer de-duplicated, so both legs of a strangle print "
        "the same symbol twice"
    )
    assert "state.positions" in src.replace("st.", "state."), (
        "the names come from somewhere other than the snapshot rows the "
        "Positions tab renders, so the two surfaces can drift"
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


def test_no_handler_is_bound_to_a_styling_class():
    """A CLASS IS A STYLING HOOK; AN ATTRIBUTE IS A BEHAVIOUR HOOK. Mixing them
    means a rename in the stylesheet can silently delete a feature.

    This is not a style preference, it is a bill that was paid. `bindJobRuns` read
    `document.querySelectorAll('button.jrun')` while the CSS class was renamed to
    `.jobrun` -- necessary, because `.jrow` was two different features sharing a
    name. The stylesheet, the markup and every layout assertion moved together and
    the whole suite stayed green, because nothing asserted on the SELECTOR. The
    Collection strip rendered perfectly and not one of its five run buttons did
    anything at all.

    Every other binder in this page already keys on a data attribute --
    `[data-replay]`, `[data-wsel]`, `[data-zstrike]`, `[data-calday]` and twenty
    more -- so this was the single exception, and it was the single thing the rename
    broke. Which is the argument: the convention was already right, it just was not
    enforced anywhere.

    Element and structural selectors are fine (`#body input`, `.seg button[data-
    type]` reaches its attribute). What is banned is a BARE class carrying the
    identity of the thing being wired.
    """
    js = _code_only(_js())
    offenders = []
    for call in re.finditer(r"querySelector(?:All)?\(\s*(['\"])(.*?)\1", js):
        selector = call.group(2)
        # Does any part of the selector reach a data attribute or an id?
        if "[data-" in selector or "#" in selector:
            continue
        # A bare class (possibly tag-qualified) with no attribute qualifier.
        if re.search(r"(?:^|[\s,>])[a-z]*\.[A-Za-z][\w-]*\s*$", selector):
            offenders.append(selector)
    assert not offenders, (
        f"these handlers are bound to a styling class, so renaming it in app.css "
        f"silently unbinds them: {offenders}. Bind on a `data-` attribute instead "
        f"-- that is what the other two dozen binders in this file do, and it is "
        f"why the `.jrun` -> `.jobrun` rename broke only this one."
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
    assert 'class="btnsmjobrun"data-job="${esc(j.job)}"' in strip, (
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
# The Watchlist panel: a list beside a detail pane.
#
# These are SOURCE assertions, and deliberately so: this panel's behaviour lives
# in page.html, which node cannot import and this project has no browser test
# runner for. Each one below was verified in a real browser first (demo journal,
# 1600x1200, three watched rows) and then pinned here at the narrowest point that
# would have caught the failure -- which is the same trade `test_a_render_preserves_
# what_the_user_was_typing` records, and the reason README's "the greps are the
# defence, not the smell" paragraph exists. What the browser showed: the hash
# carried the open row across a reload, Enter on a focused symbol cell selected it,
# every sort reversed on a second click with the barren row last in both
# directions, the note survived a redraw mid-sentence with its caret, and no cell
# rendered `undefined` or `NaN`.
# --------------------------------------------------------------------------


def test_the_row_and_the_panel_print_one_price():
    """Both surfaces derive the shown price through the SAME helper.

    This is the rule a measured defect produced: showing the fetched quote (773.26)
    beside a change computed from stored bars (-0.16%) put two DIFFERENT sessions in
    one row, because the bars ended Thursday and the quote was Friday's. A detail
    pane reading `w.last` while the row it was opened from reads `qs[sym].price`
    would be the same defect across two surfaces instead of two columns -- and
    nothing else in this suite would notice: `previous_close` appears here only as a
    payload key name, never as an assertion about how the change is derived.

    So the derivation is `shownPrice` in static/watch.js, where node tests execute
    every branch of it, and this pins that both call sites go through it.
    """
    for surface in ("watchTable", "watchDetail"):
        body = _fn(surface)
        assert "shownPrice(" in body, (
            f"{surface} no longer calls shownPrice, so the row and the panel can "
            "print a price from one session beside a change from another"
        )
    # And the page must not have grown its own copy of the fallback beside it.
    js = _code_only(_js())
    assert "previous_close" not in js, (
        "the 1d basis is being derived in the page again; it belongs in watch.js, "
        "where the quote-present, quote-without-a-previous-close and no-quote "
        "branches are each covered by an executed test"
    )
    # Both also render the price through one helper, so the stale marker and its
    # title cannot differ between the two.
    # The row prints the price, once, through the one helper; the drawer under it
    # does not repeat it, so the two cannot print the same price differently.
    assert _fn("watchTable").count("wprice(") == 1
    assert "wprice(" not in _fn("watchDetail")


def test_exactly_one_row_is_marked_current_for_a_selection():
    """The open row is marked twice over: a tint for the eye, `aria-current` for a
    screen reader, and both driven by one comparison against the chosen symbol.

    A tint alone says nothing to a screen reader, and `aria-current` alone is
    invisible -- so the test that matters is that they cannot disagree. One
    expression decides both.
    """
    body = _fn("watchTable")
    marked = body.replace(" ", "").replace("\n", "")
    assert "conston=w.symbol===open" in marked, (
        "the open row is no longer decided by one comparison, so the class, the "
        "ARIA state and the drawer can drift apart"
    )
    assert 'class="${on?\'wsel\':\'\'}"' in marked, "the open row lost its tint"
    assert "aria-expanded=\"${on?'true':'false'}\"" in marked, (
        "the row lost aria-expanded, so a screen reader cannot tell which row the "
        "drawer under it belongs to"
    )
    assert "${on?`<trclass=\"wdrawer\">" in marked, (
        "the drawer is no longer rendered from the same comparison as the tint"
    )


def test_the_selected_symbol_round_trips_through_the_hash():
    """`#wsym=DELL` is written, read back, and healed against the payload.

    Healed BEFORE syncHash, following `S.replay` and `S.calday`: this one has an
    edge neither of those has, because the reader can DELETE the row that is open.
    Stop watching DELL and the payload reloads without it, so an unhealed key would
    leave the address bar naming a symbol the journal no longer holds while the pane
    quietly showed a different one.
    """
    js = _code_only(_js())
    assert "hs.get('wsym')" in js, "the open row is not read back from the hash"
    assert "hs.set('wsym',S.wsym)" in js.replace(" ", ""), (
        "the open row is not written to the hash, so a reload loses it"
    )
    # Upper-cased on the way in, because that is how the endpoint stores a symbol
    # and how the payload sends it back.
    assert "(hs.get('wsym')||'').toUpperCase()" in js.replace(" ", "")
    draw = _fn("draw").replace(" ", "").replace("\n", "")
    assert "S.wsym=null" in draw, "wsym is not healed against the payload"
    assert draw.index("S.wsym=null") < draw.index("syncHash("), (
        "the heal must run before the hash is written, or the address bar carries a "
        "symbol the page cannot open"
    )
    # A selection is a redraw, never a refetch: every figure is already in hand.
    assert "wsym" not in _code_only(_js()).split("window.onhashchange")[1], (
        "opening a row must not trigger a payload refetch"
    )
    assert "draw()" in _fn("selectWatch")
    assert "load()" not in _fn("selectWatch")


def test_the_symbol_cell_is_a_real_control():
    """A `<button>`, not a click handler on a `<td>`.

    That is what brings the accessible name, the role, Enter and Space and the
    page's global `:focus-visible` ring without inventing any of them -- and a
    master-detail list whose master is mouse-only fails this page's own standard
    (`statCard` carries `tabindex` because "a tooltip reachable only by mouse is
    still hidden from anyone driving the page from the keyboard").

    The whole row carries the same `data-wsel` so a click anywhere selects, which
    means one handler serves both paths and neither can rot separately.
    """
    body = _fn("watchTable").replace("\n", " ")
    assert '<button class="wselb"' in body, (
        "the symbol cell is no longer a button, so the list is unreachable from "
        "the keyboard"
    )
    # The ROW carries the symbol and the one handler; the button's click bubbles to
    # it, so a button carrying data-wsel too would toggle the drawer twice.
    assert re.search(r'<tr class="[^"]*" data-wsel="\$\{esc\(w\.symbol\)\}"', body)
    # Its accessible name is the symbol, and the company name once one has arrived.
    assert "esc(w.symbol)}</b>" in body.replace(" ", "").replace("<b>", "<b>")
    # The full name must ride in the title, since the cell ellipses it. It reaches
    # there through `wselwhy` rather than inline: the same title also carries the held
    # marker's meaning, because that glyph is `aria-hidden` and a second nested `title`
    # would be a tooltip whose winner depends on where the pointer stopped. So the
    # pin is in two parts, and the fallback is the part that matters here.
    assert 'title="${esc(wselwhy(w,name))}"' in body, (
        "the symbol button's title is no longer wselwhy's sentence, so either the "
        "company name or the held fact has left the accessible name"
    )
    assert "constwhat=name||w.symbol" in _code_only(_js()).replace(" ", ""), (
        "wselwhy no longer falls back to the company name, so the title on an unheld "
        "row says nothing the ellipsed cell does not already show"
    )
    bind = _fn("bindWatchlist").replace(" ", "").replace("\n", "")
    assert "querySelectorAll('tr[data-wsel]')" in bind and (
        "el.onclick=()=>selectWatch(el.dataset.wsel)" in bind), (
        "one handler on the row must serve the mouse and the button's keyboard click"
    )
    # Re-clicking the open row closes its drawer: the table opens with none open,
    # and it must be able to return there.
    assert "S.wsym=symbol===S.wsym?null:symbol" in _fn("selectWatch").replace(" ", "")


def test_the_symbol_cell_carries_the_ellipsis_trio():
    """All three properties, because any one missing disables the other two.

    `min-width:0` (a flex/grid item refuses to shrink below min-content without
    it), `white-space:nowrap` (text-overflow only applies to a single line) and
    `text-overflow:ellipsis` with `overflow:hidden`. The idiom is `.mkdot`'s, and
    the reason is that "CrowdStrike Holdings, Inc." beside a symbol exceeds any
    share of a split card -- without the trio the pane widens instead.
    """
    css = _css().replace(" ", "").replace("\n", "")
    rule = css.split(".wselb{")[1].split("}")[0]
    for prop in ("min-width:0", "white-space:nowrap",
                 "overflow:hidden", "text-overflow:ellipsis"):
        assert prop in rule, (
            f".wselb lost {prop}, which silently disables the rest of the "
            f"truncation: the cell will widen the pane instead of ellipsing"
        )


def test_the_sort_reverses_on_a_second_click():
    """A click on the SORTED column flips its direction; a click on a new one
    starts descending for the derived columns and ascending for the symbol.

    A sort that only ever ascends passes any test that checks order once, which is
    why the reversal is pinned rather than the order. Verified in a browser over
    every column: `rvr` gave NVDA, SPY then SPY, NVDA on the second click, and
    `aria-sort` followed both times.
    """
    handler = _fn("bindWatchlist").replace(" ", "").replace("\n", "")
    assert "key===was[0]?key+':'+(was[1]==='desc'?'asc':'desc')" in handler, (
        "clicking the sorted column no longer reverses it"
    )
    assert "key+':'+(key==='symbol'?'asc':'desc')" in handler, (
        "a new column must open descending for a derived figure (the interesting "
        "end is the top) and ascending for the symbol (alphabetical)"
    )
    # The state that carries it, and its default: symbol-ascending, which is
    # `serialize.watchlist_data`'s own ORDER BY so the page and the CLI agree
    # about which row is first.
    js = _code_only(_js()).replace(" ", "").replace("\n", "")
    assert "wsort:'symbol:asc'" in js
    # aria-sort tracks the same two halves, or the glyph and the announcement
    # disagree.
    table = _fn("watchTable").replace(" ", "").replace("\n", "")
    assert "aria-sort=\"${col===key?(asc?'ascending':'descending'):'none'}\"" in table
    assert "col===key?(asc?'↑':'↓'):'⇅'" in table, (
        "the inactive double arrow is gone, so an unsorted column reads as "
        "unsortable"
    )


def test_nulls_sort_last_in_both_directions():
    """An absent figure is not a small one.

    A symbol holding no B-Xtrender has not stored enough sessions to have a
    reading, so it must never top an ascending sort, where it would read as the most
    extreme value in the column. The null branch therefore returns its verdict
    BEFORE the direction multiplier is applied -- that ordering is the whole rule,
    and folding the nulls into the comparison is how they end up first in one
    direction.
    """
    body = _fn("watchSort").replace(" ", "").replace("\n", "")
    assert "if(va==null||vb==null){" in body, "the null case is no longer separate"
    assert "returnva==null?1:-1" in body, (
        "a missing value must sort after a measured one whichever way the column "
        "is pointing"
    )
    nulls = body.index("returnva==null?1:-1")
    direction = body.index("*(asc?1:-1)")
    assert nulls < direction, (
        "the direction multiplier now reaches the null verdict, which puts "
        "unmeasured rows first in one of the two directions"
    )
    # Ties break on the symbol, so the block of nulls has a stable readable order
    # rather than the payload's arrival order.
    assert body.count("wa.symbol<wb.symbol?-1:1") == 2


def test_every_implied_vol_on_the_tab_is_attributed_to_whoever_measured_it():
    """This rule INVERTED when the source arrived, and the inversion is the point.

    What stood here before was an absence grep: the tab must never render the letters
    IV, because implied vol is not reachable for a symbol this journal does not hold.
    That premise was true of an implied vol this journal would COMPUTE, and it is
    still true -- `iv.py` computes none. It was never true of one this journal is
    TOLD. CBOE publishes iv30 and the high and low of its own iv30 over the trailing
    year, so the tab now carries a real IV rank, and the rule that replaces the
    absence is the one that made the absence necessary in the first place:

        A FIGURE THIS JOURNAL DID NOT MEASURE MUST SAY WHOSE IT IS.

    That is `market_events.impact`'s rule, stored verbatim as "the FEED's judgement,
    not the journal's" and printed with that sentence beside it. So the assertions
    are all positive now. An absence grep would pass over a ring with no caption at
    all; naming the source is what cannot be faked.
    """
    watch = " ".join(_fn(name) for name in
                     ("watchTable", "watchDetail", "watchFilters", "wring"))
    labels = _code_only(watch)

    # The provenance sentence, spent at all three surfaces that show the figure: the
    # sortable header, the tile's caption, and the filter group's caption. One of the
    # three going missing is a rank on screen with nobody's name on it.
    assert labels.count("IVR_SOURCE") >= 3, (
        f"IVR_SOURCE is spent {labels.count('IVR_SOURCE')} times across the header, "
        f"the tile and the filter caption; every surface that prints CBOE's rank has "
        f"to print whose rank it is"
    )
    # And that sentence actually names them, rather than being an empty slot.
    source = re.search(r'const IVR_SOURCE="([^"]*)"', _js())
    assert source, "IVR_SOURCE is gone, so nothing on the tab attributes the rank"
    for word in ("CBOE", "trailing year", "delayed"):
        assert word in source.group(1), (
            f"the attribution no longer says {word!r}: a reader cannot tell whose "
            f"figure this is, over what window, or how fresh"
        )

    # The bounds travel WITH the rank, which is what makes it reproducible. The same
    # 76.7 inside a two-point year is a different fact from one inside a
    # seventy-point year, and the rank alone can be neither checked nor doubted.
    detail = _fn("watchDetail")
    for part in ("ivr.iv30", "ivr.low", "ivr.high", "ivr.as_of"):
        assert part in detail, (
            f"the rank's caption dropped {part}, so the figure no longer states the "
            f"range it is a position inside or how old it is"
        )

    # The two ranks stay distinguishable on screen. Realised and implied are
    # different measurements, and the tab shows both -- an implied rank high while
    # the realised rank is low is the market charging more than the stock delivered,
    # which is only readable if the labels never blur.
    assert "realised vol" in labels or "realised vol" in detail, (
        "the realised figure lost its label, so two ranks share one vocabulary"
    )
    for part in ("rv_rank_low", "rv_rank_high", "rv_rank_windows"):
        assert part in detail, (
            f"the realised rank dropped {part}: it moved into the realised vol "
            f"tile's caption, it did not stop needing its own bounds"
        )


def test_the_attribution_sentence_survives_the_rewrite():
    """The footer sentences, and what each one is for.

    The first DISTINGUISHES the tab's two volatilities, and its wording had to change
    when the second one arrived. It used to end "implied vol needs an option chain
    this journal cannot reach", which was the honest sentence while the neighbouring
    rank was realised. With an IVR column two cells left it became a footer denying
    what the table above it shows -- caught by reading the rendered page, not by any
    assertion here, which is why this one now pins the CONTRAST rather than the
    denial.

    The second is the only instruction that turns this tab's dashes into numbers, so
    it names the symbols that are waiting.
    """
    body = _fn("watchlist") + _fn("watchTable")
    assert "realised vol is what the stock DID" in body
    assert "IVR is what the" in body and "market CHARGES" in body, (
        "the footer no longer contrasts the two volatilities, so a reader has no "
        "sentence telling them why the tab carries both"
    )
    assert "cannot reach" not in body, (
        "the footer is denying that implied vol is reachable while an IVR column is "
        "on screen two cells away"
    )
    assert "no stored history yet for ${\n      esc(thin.join(', '))}: run" in body, (
        "the remedy no longer names the thin symbols, so a reader cannot tell "
        "which rows a `bars` run would fill in"
    )
    assert "optjournal bars" in body
    # And the typed column says it is typed, which is slice 4's clause.
    assert "earnings dates are ones you recorded" in body


def test_the_typed_field_is_preserved_across_a_render_and_not_across_subjects():
    """`loadQuotes()` calls `draw()` on its own, so a redraw lands mid-typing.

    This guarded a note editor until the note panel went. The editor's textarea went
    with it and so did the `textarea` half of the selector -- a selector matching
    nothing is a mechanism no test can reach. Both halves are back, because the
    journal form brought eleven textareas and a select, and neither failure the
    guard was written for ever went away:

    Preserved across a render of the SAME row: a quote landing mid-sentence otherwise
    eats the text, the focus and the caret, exactly as it once ate the add field's.

    NOT preserved across subjects, which is the sharper bug. An id is unique per
    RENDER, not per subject, so `#wearn` is the earnings field whatever row is open:
    selecting another symbol restored the previous symbol's typed date under the new
    symbol's heading, and Save would have written it there. Verified in a browser
    both ways.
    """
    helper = _code_only(_js())
    keep = helper[helper.index("function preserveInputs"):]
    assert "#body input" in keep, (
        "preserveInputs no longer scans the panel's inputs, so the earnings date "
        "loses what was typed on every redraw"
    )
    assert "textarea" in keep and "select" in keep, (
        "the journal form's fields are textareas and a select, so a redraw "
        "mid-sentence discards writing this database cannot rebuild"
    )
    # The other half of the rule that once removed `textarea`: a selector may only
    # name what the page actually renders, or it is a mechanism no test can reach.
    assert "<textarea" in _fn("jfield") and "<select" in _fn("jfield"), (
        "the selector names textarea and select but the page renders neither -- "
        "either restore the fields or narrow the selector back"
    )
    assert "subject:el.dataset.subject" in keep.replace(" ", ""), (
        "the subject is not captured, so a date typed for one symbol can be "
        "restored under another"
    )
    # Every journal field carries the anchor it belongs to, for the same reason the
    # earnings date carries its symbol: opening another decision's editor with text
    # unsaved in this one would otherwise restore this decision's sentence under
    # that one's heading, and Save would file it there.
    assert 'data-subject="${esc(anchor)}"' in _fn("jfield"), (
        "the journal fields do not name the decision they belong to, so text typed "
        "for one write-up can be restored into another's"
    )
    # And the field it now protects actually carries a subject to be checked against.
    assert 'id="wearn" data-subject="${esc(w.symbol)}"' in _fn("watchDetail"), (
        "the earnings field lost its id or its subject, so preserveInputs either "
        "skips it or cannot tell which symbol the text belongs to"
    )
    restore = helper[helper.index("function restoreInputs"):]
    assert "!==was.subject)return" in restore.replace(" ", ""), (
        "a field whose subject changed under it must be dropped, not reconciled"
    )
    # It writes through the same endpoint, and an emptied box CLEARS -- which is what
    # web._watchlist_write's key-present semantics were built for. The field is the
    # earnings date now that the note editor has gone; `note` is still a writable
    # column, reachable from `optjournal watch --note`, so the SEMANTICS did not move
    # with the panel that used to exercise them.
    assert 'data-wfield="earnings_on"' in _fn("watchDetail")
    assert "body[wf.dataset.wfield]=el.value" in _fn("bindWatchlist").replace(" ", ""), (
        "the field's value must be sent unmodified, or an emptied box cannot clear "
        "a recorded date"
    )


def test_the_gate_counts_on_screen_match_the_python_constants():
    """A dash names the count it is waiting for, and 120 must be the real gate.

    The page holds its own copy of both gates because a count alone cannot explain
    a dash -- "43" means nothing without "of 120" -- and neither gate is on the wire
    (they are constants of the arithmetic, not facts about a symbol). A copy needs a
    test, or a retune in `trend.py` leaves the screen explaining a dash with a
    threshold that no longer applies, which is exactly the drift `PARAMS_CAPTION`
    and `BANDS_SOURCE` exist to prevent one level down.
    """
    from optjournal import trend, vol  # noqa: PLC0415 - local to this test

    js = _code_only(_js())
    assert f"const BX_SETTLED={trend.MIN_SETTLED};" in js, (
        f"the page explains a B-Xtrender dash against a gate that is not "
        f"trend.MIN_SETTLED ({trend.MIN_SETTLED})"
    )
    assert f"const RVR_WINDOWS={vol.RANK_MIN_WINDOWS};" in js, (
        f"the page explains a rank dash against a gate that is not "
        f"vol.RANK_MIN_WINDOWS ({vol.RANK_MIN_WINDOWS})"
    )
    # And the tile's caption is the leaf's own generated sentence, so a retune of
    # the periods cannot leave the screen claiming the old ones.
    assert trend.PARAMS_CAPTION in js, (
        "the B-Xtrender tile's caption no longer matches trend.PARAMS_CAPTION, so "
        "the periods on screen are not the periods that produced the figure"
    )
    # Each dash actually spends them, or the constants are decoration.
    assert "BX_SETTLED} sessions stored" in _fn("watchTable")
    assert "BX_SETTLED} ISO weeks stored" in _fn("watchDetail")
    # `wrankwhy` is an arrow const rather than a `function`, so it is read out of
    # the whole script: it is one sentence shared by the cell's title and the
    # tile's caption, which is the point of it existing at all.
    assert "RVR_WINDOWS} windows measured" in js


def test_the_held_marker_is_rendered_and_says_what_it_means():
    """`Watch.held` shipped on the wire with NO reader, and this is that gap closed.

    The defect shape is specific and worth naming, because it is invisible to every
    other check here: a payload key can be emitted by `serialize` (serialize.py:971),
    declared on the `Watch` typedef, pass the contract test that pairs those two, and
    still be read by nothing at all. The contract test asks whether the page DECLARES
    what the payload sends. It cannot ask whether the page SPENDS it. So the marker the
    tab needs most -- the one fact a quote screen cannot show, that you have positions
    open on this name -- was declared and never drawn, and the plan recorded it as
    built.

    Three assertions, because the marker fails in three separate ways. Unread: the
    row does not branch on `held`. Unexplained: the glyph is `aria-hidden`, so if the
    title does not carry the sentence then a screen reader gets a symbol and no fact,
    and a hover gets the company name only. Untheming: a hex here wears Leather's
    brass on Admiralty and Ledger, which is the defect the performance chart shipped
    once already with `#0a0806` hardcoded into a stroke.
    """
    row = _fn("watchTable")
    assert "w.held" in row, (
        "watchTable no longer branches on Watch.held, so the payload key is back to "
        "having no reader and the held marker is not drawn on any row"
    )
    assert 'class="whold"' in row, \
        "the held marker's class is gone, so nothing in the stylesheet can reach it"

    # The sentence, and the fact that it is spent. `wselwhy` is an arrow const, so it
    # is read out of the whole script like `wrankwhy` above.
    js = _code_only(_js())
    assert "you hold" in js and "option position(s) on it" in js, (
        "the held marker's title no longer says what the mark means, and the glyph is "
        "aria-hidden -- so the row states a fact only in pixels"
    )
    assert "title=\"${esc(wselwhy(" in row, (
        "the symbol button's title is no longer wselwhy's sentence, so either the "
        "held fact is unreachable or it is in a second nested title attribute"
    )

    # Themed, not hardcoded. `--accent` is defined by :root and re-defined by both
    # other themes (app.css:53, :136, :203), which is what makes the marker travel.
    marker = re.search(r"\.whold\{([^}]*)\}", _css())
    assert marker, ".whold lost its rule, so the marker renders at body colour and size"
    assert "var(--accent)" in marker.group(1), (
        f"the held marker's colour is not a theme variable: {marker.group(1)!r} -- a "
        f"hex here is Leather's brass worn on Admiralty and Ledger"
    )


# --------------------------------------------------------------------------
# The filter row and the two gauges.
#
# Same trade as the section above, and one addition worth naming: the gauges'
# ARITHMETIC is not here at all. It is in static/watch.js under node tests, because
# an inverted knob or an arc scaled by the radius renders a well-formed picture that
# contains no `undefined` and no `NaN` -- nothing in this file, and nothing in the
# sweep, can see it. What is pinned here is the part Python can see and would
# otherwise drift: that the numbers on screen are the numbers the Python constants
# produced, and that a control which hides every row says so.
#
# Verified in a real browser first (demo journal, 1600x2400, three watched rows):
# company-name search narrowed to one row while the box kept its caret through four
# redraws, each bucket and each band narrowed and named itself when it emptied the
# table, the earnings chip enabled the moment a date was recorded and excluded the
# 14d row while keeping the 80d and the dateless ones, and Clear filters restored all
# three rows with the previously open row still open. One defect came out of that
# session and is fixed with its own assertion below.
# --------------------------------------------------------------------------


def test_the_meter_scale_is_fixed_at_the_indicators_own_bounds():
    """The literal -50 and +50 reach `meterKnob`, so the meter cannot auto-scale.

    B-Xtrender is an RSI minus 50, so [-50, +50] is a real statable bound rather than
    a preference -- and three years of TSLA used only [-35.6, +41.6] of it. Scaling to
    the visible rows instead would make one knob position mean a different reading on
    every refresh, and two symbols compared side by side would be read off two
    different scales.

    The bounds and the box travel as ONE object, which is the other half: the numbers
    printed at the ends of the scale and the knob's position between them are read from
    the same four fields, so a picture that disagrees with its own labels cannot be
    written. `meterKnob` clamps rather than extrapolating (a node test), so a value
    past the bound is pinned at the end instead of drawn off the track.
    """
    js = _code_only(_js()).replace(" ", "")
    assert "constWMETER={x:6,w:156,lo:-50,hi:50}" in js, (
        "the meter's domain is no longer the indicator's own [-50, +50] stated as "
        "literals beside its box"
    )
    meter = _fn("wmeter")
    assert "meterKnob(w.bx_daily,WMETER)" in meter.replace(" ", ""), (
        "the knob is not placed by the tested helper against the fixed box"
    )
    # The scale's ends are PRINTED, from the same object, and nothing near the meter
    # measures the data to find them.
    assert "WMETER.lo" in meter and "WMETER.hi" in meter, (
        "the scale ends are no longer read from the box the knob is placed against"
    )
    for measured in ("Math.min", "Math.max", "reduce("):
        assert measured not in meter, (
            f"{measured} in the meter means the scale is being derived from the rows "
            f"on screen, which is the auto-scaling this test exists to prevent"
        )


def test_the_band_caption_matches_the_constants_that_produced_it():
    """The bucket chips' numbers and their provenance sentence are `trend.py`'s.

    B-Xtrender publishes no oversold or overbought level at all -- no hline, no level
    inputs, only zero crossed with rising-or-falling -- so any band on screen is
    IMPORTED, and an imported threshold with no provenance is one its reader cannot
    audit. `BANDS_SOURCE` is the sentence that names the import and, in the same
    breath, refuses the convention's valuation words: "oversold" and "overbought" are
    claims about what a share is worth, and this is a statement about the shape of
    recent closes.

    Both halves are bound to Python, so a retune of the band cannot leave the row
    offering the old one with nothing on screen saying so.
    """
    from optjournal import trend  # noqa: PLC0415 - local to this test

    js = _code_only(_js())
    assert f"const BX_LO={int(trend.BX_OVERSOLD)};" in js, (
        f"the low band chip is not cut at trend.BX_OVERSOLD ({trend.BX_OVERSOLD})"
    )
    assert f"const BX_HI={int(trend.BX_OVERBOUGHT)};" in js, (
        f"the high band chip is not cut at trend.BX_OVERBOUGHT "
        f"({trend.BX_OVERBOUGHT})"
    )
    assert trend.BANDS_SOURCE in js, (
        "the band caption is no longer trend.BANDS_SOURCE, so the page can quote a "
        "provenance the constants no longer have"
    )
    # And each is actually SPENT: the labels are generated from the cut points, and
    # the sentence is rendered in the row's caption.
    labels = _code_only(_js()).replace(" ", "")
    assert "low:'below'+num(BX_LO,0)" in labels and "high:'above+'+num(BX_HI,0)" in labels, (
        "the chip labels are typed rather than generated, so one can read 'below -20' "
        "after the band has moved"
    )
    assert "BX_BANDS" in _fn("watchFilters"), "the provenance sentence is not rendered"
    # The band belongs to ONE arm, and the row says which: +/-20 cuts the outer ~10%
    # of the daily short arm and about a third of the long one, so a caption that did
    # not name the arm would be describing a bucket that holds a different share of
    # the sessions depending on which series you read it against.
    assert "daily short arm" in _fn("watchFilters")


def test_the_indicator_parameters_are_on_screen():
    """A figure headed BXTRENDER with no periods stated cannot be reproduced.

    B-Xtrender at 5/20/15 and B-Xtrender at other settings are different numbers, so
    the tile's caption is `trend.PARAMS_CAPTION` -- generated in Python from the
    constants themselves, which is what stops a retune leaving the screen claiming
    the old periods. The same gap `impact_source` closes for the calendar feed.
    """
    from optjournal import trend  # noqa: PLC0415 - local to this test

    assert trend.PARAMS_CAPTION in _code_only(_js())
    assert "title=\"${esc(PARAMS_CAPTION)}\"" in _fn("watchDetail").replace(" ", ""), (
        "the daily tile no longer captions itself with the generated sentence"
    )
    # The column header spends it too, so a reader scanning the table can reach the
    # periods without opening a row.
    assert "PARAMS_CAPTION" in _fn("watchTable")


def test_the_ivr_chips_and_the_ring_tick_read_one_constant():
    """The picture and the filter cannot disagree about where the cut point is.

    One page constant, bound to `iv.IVR_HIGH`, spent three times: the two chip labels
    and the ring's threshold tick. If the tick were drawn from its own literal, a
    retune would move the chips and leave the mark where it was -- a gauge whose
    reference line contradicts the control beside it, which is worse than having no
    mark at all.

    THIRTY IS THE READER'S LINE, and the caption has to say that rather than imply
    research put it there. tastytrade, whose formula this rank uses, publish 50 as the
    level premium selling leans on, 80 as extreme and 20 as depressed. 30 is none of
    those: it is a wider net, chosen by the person running the screen. A chip may cut
    wherever its reader wants; what it may not do is borrow someone else's authority
    for the choice. So the assertion is on both halves -- the number, and the sentence
    disclaiming whose number it is.
    """
    from optjournal import iv  # noqa: PLC0415 - local to this test

    js = _code_only(_js())
    assert f"const IVR_HIGH={int(iv.IVR_HIGH)};" in js, (
        f"the IVR chips are not cut at iv.IVR_HIGH ({iv.IVR_HIGH})"
    )
    bands = re.search(r'const IVR_BANDS="([^"]*)"', _js())
    assert bands, "IVR_BANDS is gone, so the cut point is on screen unexplained"
    for word in ("your own", "tastytrade"):
        assert word in bands.group(1), (
            f"the band caption no longer says {word!r}: 30 has to read as the "
            f"reader's own screening line and not as anyone's published level"
        )
    flat = js.replace(" ", "")
    assert ("upper:'IVR>'+num(IVR_HIGH,0)" in flat
            and "lower:'IVR<='+num(IVR_HIGH,0)" in flat), (
        "the chip labels no longer come from the constant, so they can name a cut "
        "point the filter does not use"
    )
    # The tick, from the SAME constant, as a fraction of a turn rather than a
    # percentage -- `ringPoint` takes a turn, and 30 would be thirty laps.
    tick = _fn("wring").replace(" ", "")
    assert tick.count("ringPoint(IVR_HIGH/100,") == 2, (
        "both ends of the tick must come from the same fraction as the chips, or the "
        "mark and the control point at two different numbers"
    )


def test_the_ring_caption_names_its_window_and_its_bounds():
    """A 0-to-100 arc in an options journal reads as IV rank to anyone who has one.

    So the tile states, in words, every part of what it measured: the window (a
    trailing year, in the label), the figure, the year's low and its high, and how
    many windows it ranked against. The bounds matter most -- a rank hides an outlier
    inside a rate, and printing both ends is what makes 67 checkable.

    NO VERDICT WORD. "Elevated" is a claim about a level against some normal, and
    nothing here establishes a normal; the slot prints the measurement instead.
    """
    ring = _fn("watchDetail")
    assert "ivr · iv rank, 1y" in ring, (
        "the tile no longer names what it ranks or over what window"
    )
    for part in ("ivr.iv30", "ivr.low", "ivr.high"):
        assert part in ring, f"the caption dropped {part}"
    assert "this year's low" in ring and "to its high" in ring
    # The LEVEL beside the rank, which is the pair that makes a rank legible: 78.7 on
    # an implied vol of 33.6% and 78.7 on one of 90% are the same position and
    # completely different trades.
    assert "implied vol is" in ring, (
        "the caption no longer prints the implied vol the rank is a position of, so a "
        "high rank on a modest level is indistinguishable from one on a violent level"
    )
    # Absent, it is a dash whose title says WHICH absence -- not fetched, not carried,
    # or failed -- because those have three different remedies.
    assert "wdash(ivrWhy(w.symbol))" in ring.replace(" ", "")
    # NO VERDICT WORD, even now that one would be attributable. tastytrade's "premium
    # selling favored" belongs to their 50, and this tab cuts at the reader's 30: a
    # verdict imported across a different threshold is advice the number does not
    # support. The measurement prints instead.
    for verdict in ("Elevated", "elevated", "favored", "favoured"):
        assert verdict not in ring, (
            f"the ring is passing a verdict ({verdict}): the tab cuts at 30 and that "
            f"word is calibrated on 50, so it would recommend on the strength of a "
            f"threshold nobody applied"
        )


def test_the_gradient_stops_carry_no_colour_attribute():
    """A presentation attribute cannot hold a `var()`, so a colour in one is a colour
    no theme can repaint.

    This is the defect the performance chart shipped with: its line and its dot halo
    were SVG attributes holding hexes, one of them Leather's own background, so on
    Admiralty the dots were ringed in a brown the page no longer contained. The
    stylesheet check cannot see an attribute, which is why that chart sat out the
    first theme release entirely.

    So the meter's gradient stops carry CLASSES and the colours live in app.css, where
    `test_no_colour_literal_lives_outside_a_theme_block` and the theme-parity check
    both reach them.
    """
    meter = _fn("wmeter")
    assert "<linearGradient" in meter, "the meter lost its gradient"
    stops = re.findall(r"<stop[^>]*>", meter)
    assert len(stops) == 3, f"expected three stops, found {stops}"
    for stop in stops:
        assert "stop-color" not in stop, (
            f"a stop carries a colour attribute ({stop}), which survives every theme "
            f"swap unchanged"
        )
        assert "class=" in stop, "a stop with no class cannot be coloured at all"
    css = _css().replace(" ", "").replace("\n", "")
    for name, var in (("lo", "--badfill1"), ("mid", "--bg2"), ("hi", "--okfill1")):
        assert f".wmeterstop.{name}{{stop-color:var({var})}}" in css, (
            f"the {name} stop has no themed colour in app.css"
        )
    # The knob likewise: it takes the page's own sign palette through currentColor
    # rather than naming a colour of its own.
    assert "fill:currentColor" in css.split(".wmknob{")[1].split("}")[0]
    assert "'pos'" in meter and "'neg'" in meter, (
        "the knob no longer carries a sign class, so it cannot be tinted at all"
    )


def test_an_unknown_earnings_date_is_not_excluded():
    """The earnings filter excludes only dates you RECORDED.

    Sparse nulls are the normal state of this column, not an edge: `earnings_on` is
    typed, so most rows carry none for a long time. Dropping them would present the
    filtered list as "nothing here reports within 28 days" when the truth is "nothing
    here reports within 28 days that you have told me about" -- a claim this journal
    has no source for, since nothing it can reach publishes an earnings date.

    The rule is `earningsSoon` in watch.js, where node tests run the mixed set (14
    days, 80 days, null, and the -1 past case) rather than reading it. It is a
    function and not an inline comparison for one measured reason: `null >= 0` is TRUE
    in JavaScript, so the obvious spelling hides every dateless row the moment the
    filter goes on, silently and only while the chip is pressed. Verified in a browser
    over a mixed set: NVDA at 14d dropped out, SPY at 80d and the dateless ZZZDEMO
    stayed.
    """
    rows = _fn("watchRows").replace(" ", "")
    assert "earningsSoon(w.earnings_in_days,WEARN_SOON)" in rows, (
        "the earnings filter is comparing days inline again; `null >= 0` is true in "
        "JavaScript, so that spelling hides every row with no date recorded"
    )
    assert "constsoon=w=>earningsSoon(w.earnings_in_days,WEARN_SOON)" in rows
    assert "!(S.wearn==='out'&&soon(w))" in rows, (
        "hiding must EXCLUDE the near ones rather than keep them, and only while the "
        "toggle is pressed"
    )
    assert "!(S.wearn==='in'&&!soon(w))" in rows, "the keep-only toggle is gone"
    # The chip says what it does, because both readings of it are defensible and they
    # differ by the whole table.
    chip = _fn("watchFilters")
    assert "Hiding keeps a row with no date" in chip, (
        "the toggle's tip no longer states that a row with no date is kept"
    )
    # And it is disabled, with its own reason, when no row carries a date at all --
    # rather than offered as a control that would hide nothing.
    flat = chip.replace(" ", "").replace("\n", "")
    assert flat.count("!dated)") == 2 and "${off?'disabled':''}" in flat, (
        "the chip is offered even when no date exists anywhere, where pressing it "
        "cannot change the list"
    )
    assert "no watched symbol carries an earnings date yet" in chip


def test_a_filter_matching_nothing_says_which_control_is_hiding_the_rows():
    """A non-empty watchlist with an empty filtered set has no first row.

    Which makes it a STATE, not an edge to be discovered at runtime: the likely
    accidental implementation is a blank `<tbody>` beside a detail pane reading from
    `undefined`, and `sweep.check_no_junk_bindings` fails on exactly that. Both panes
    are specified, and both name the CONTROL -- a reader who typed in the search box
    and then pressed a bucket chip cannot see which of the two emptied the table.

    One sentence, one function, spent by both panes, so they cannot describe two
    different situations.
    """
    said = _fn("wnorows")
    assert "watched symbol(s) are hidden by" in said
    active = _fn("wactive")
    for control in ("the search box", "the BXTRENDER filter", "the IV rank filter",
                    "the EARNINGS filter"):
        assert control in active, f"an empty result cannot name {control}"
    # The search TERM is quoted back, because a typo is the likeliest cause.
    assert 'the search box ("${needle}")' in active
    # The table's colspan row, with the way out beside it.
    table = _fn("watchTable").replace(" ", "").replace("\n", "")
    assert 'colspan="7"' in table, "the empty row does not span the seven columns"
    assert "esc(wnorows())" in table and "data-wclear" in table
    assert "${body||none}" in table, (
        "the empty row is not rendered in place of an empty body, so the table shows "
        "a bare header and the reader is left guessing"
    )
    # With the drawer, an empty filtered set simply has no row to open; the table's
    # sentence is the whole state. The open symbol survives it, so clearing the
    # filter brings the drawer back.
    tab = _fn("watchlist").replace(" ", "").replace("\n", "")
    assert "S.wsym&&rows.some(w=>w.symbol===S.wsym)?S.wsym:''" in tab, (
        "the drawer can render from a row the filter is hiding"
    )
    assert "S.wsym=null" not in _fn("watchRows"), "the filter must not clear the choice"
    # Clearing empties the FIELD as well as the state. Caught in a browser: without
    # it, `preserveInputs` restored "nvidnvidzzz" over the freshly rendered empty box,
    # so the table showed every row while the search box described a filter that was
    # no longer applied.
    bind = _fn("bindWatchlist").replace(" ", "").replace("\n", "")
    assert "if(sb)sb.value='';S.wsearch=''" in bind, (
        "Clear filters leaves the reader's text in the search box, which then "
        "describes a filter that is not applied"
    )


def test_the_search_input_carries_an_id():
    """Or `preserveInputs` drops what the reader typed on the next redraw.

    The helper is keyed by `id` because that is what survives an innerHTML
    replacement, and this field redraws the whole tab on every keystroke -- so without
    an id the box would lose its text, its focus and its caret on the first character.
    That is exactly what happened to `#wadd`, which is why the helper exists at all.

    And it carries NO `data-subject`: a search box is not editing one symbol, so its
    text must survive a change of selection rather than being dropped with it.
    """
    filters = _fn("watchFilters")
    assert 'id="wsearch"' in filters, "the search box has no id, so a redraw eats it"
    box = filters[filters.index('id="wsearch"'):filters.index('id="wsearch"') + 400]
    assert "data-subject" not in box.split("</div>")[0], (
        "a data-subject on the search box would drop the reader's query whenever "
        "another row was opened"
    )
    # Its value is rendered FROM state, so the box and the filter agree after any
    # redraw, and the handler writes state on input.
    assert 'value="${esc(S.wsearch||\'\')}"' in filters
    assert "sb.oninput" in _fn("bindWatchlist"), "typing does not narrow the list"
    # The placeholder says whether company search is available yet, because the name
    # only exists after Refresh has run -- offering "symbol or company" before that
    # would silently match nothing.
    assert "Symbol or company" in filters and "company names arrive with" in filters
    assert "named" in filters, "the placeholder is not derived from what arrived"


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


def _an_anchor(db_path) -> tuple[str, str]:
    """A real order id from the archive, and the account that placed it.

    Real rather than invented, because `/api/journal` derives the account, the
    underlying and the open date from the fills the anchor names -- so an invented
    id is refused, which is behaviour with its own test below.
    """
    conn = connect(db_path)
    row = conn.execute(
        "SELECT ib_order_id, account_id FROM trades"
        " WHERE ib_order_id IS NOT NULL ORDER BY ib_order_id LIMIT 1"
    ).fetchone()
    conn.close()
    return str(row["ib_order_id"]), str(row["account_id"])


def test_the_journal_endpoint_round_trips_an_entry(populated):
    """Write a plan, then a review, then read the state back.

    One test for the sequence, because the sequence is the behaviour: the two are
    written weeks apart from different forms, and the second must not blank the
    first. A whole-row POST is how a review erases the plan it is reviewing.
    """
    anchor, _account = _an_anchor(populated)
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        status, wrote = _post(base, "/api/journal", {
            "anchor": anchor,
            "plan_target": "take at 50% of credit",
            "plan_invalidation": "short strike tested",
        })
        assert (status, wrote["ok"]) == (200, True)
        assert wrote["entry"]["plan_target"] == "take at 50% of credit"

        _, reviewed = _post(base, "/api/journal", {
            "anchor": anchor, "followed_target": "yes",
            "exit_trigger": "target", "lessons": "closed a week early",
        })
        entry = reviewed["entry"]
        assert entry["plan_target"] == "take at 50% of credit", (
            "the close review blanked the plan it was reviewing"
        )
        assert (entry["followed_target"], entry["exit_trigger"]) == ("yes", "target")

        _, state = _get(base, "/api/state")
    assert state["journal"]["entries"][anchor]["lessons"] == "closed a week early"


def test_the_journal_endpoint_derives_the_decisions_identity_from_the_fills(populated):
    """The account, the underlying and the open date are BROKER facts.

    So the form does not send them and cannot send them wrong. The entry is keyed
    on the account IBKR says placed the order, which is also what makes the key
    resolvable from a card that carries only an anchor.
    """
    anchor, account = _an_anchor(populated)
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        _, wrote = _post(base, "/api/journal", {
            "anchor": anchor, "entry_note": "n",
            # Sent and ignored: these are not writable fields, and a request that
            # could set them could file an entry under an account it has no
            # business naming.
        })
    assert wrote["entry"]["account_id"] == account
    assert wrote["entry"]["underlying"], "no underlying was resolved from the fills"
    assert wrote["entry"]["opened_on"], "no open date was resolved from the fills"


def test_an_entry_against_an_order_this_journal_never_saw_is_refused(populated):
    """404, not a stored row.

    A row keyed on a typo would be invisible from every surface afterwards: no
    card carries that anchor, so nothing would ever render it back, and the reader
    would believe they had written something down.
    """
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        status, payload = _post(base, "/api/journal",
                                {"anchor": "9999999999", "lessons": "x"})
    assert status == 404
    assert payload["kind"] == "anchor"


def test_a_journal_write_with_no_anchor_is_refused(populated):
    """The snapshot-only position: a decision with no fills to attach writing to.

    Named as such in the message, because "no anchor" is not a thing a reader did
    wrong -- it is a position this journal holds only as a snapshot.
    """
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        status, payload = _post(base, "/api/journal", {"lessons": "x"})
    assert (status, payload["kind"]) == (400, "anchor")


@pytest.mark.parametrize(("field", "value"), [
    ("followed_target", "true"),
    ("followed_invalidation", "1"),
    ("exit_trigger", "felt_wrong"),
])
def test_the_journal_endpoint_refuses_a_value_outside_its_enumeration(
    populated, field, value
):
    """Coerced to null instead, the adherence and trigger counts would be wrong in
    the reassuring direction -- and those counts are the whole reason the two
    fields are enumerations rather than free text."""
    anchor, _account = _an_anchor(populated)
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        status, payload = _post(base, "/api/journal",
                                {"anchor": anchor, field: value})
    assert (status, payload["kind"]) == (400, "journal")


def test_the_journal_endpoint_refuses_a_field_the_table_does_not_have(populated):
    """A typo in the page's form must fail loudly.

    Ignored instead, the request answers `ok` and the text is never seen again --
    which for this table means it is gone, since nothing can re-derive it.
    """
    anchor, _account = _an_anchor(populated)
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        status, payload = _post(base, "/api/journal",
                                {"anchor": anchor, "plan": "take at 50%"})
    assert (status, payload["kind"]) == (400, "journal")


def test_emptying_an_entry_through_the_endpoint_removes_it_from_the_state(populated):
    """Clearing every field deletes, and the page must see that.

    A stored row of nulls would keep a "written up" badge on a card whose writing
    the reader just deleted.
    """
    anchor, _account = _an_anchor(populated)
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        _post(base, "/api/journal", {"anchor": anchor, "lessons": "x"})
        _, emptied = _post(base, "/api/journal", {"anchor": anchor, "lessons": ""})
        assert emptied["entry"] is None
        _, state = _get(base, "/api/state")
    assert anchor not in state["journal"]["entries"]


def test_an_over_long_entry_is_refused_rather_than_read_as_empty(populated):
    """The one place a size cap could DESTROY writing rather than reject it.

    `_body` answers `{}` for a body over its limit, which `journal.save` would
    read as an entry emptied of every field and delete. So the length is checked
    before the body is read, and the reply says nothing was saved -- for a table
    whose rows cannot be re-derived from anything.
    """
    anchor, _account = _an_anchor(populated)
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        _post(base, "/api/journal", {"anchor": anchor, "lessons": "worth keeping"})
        status, payload = _post(base, "/api/journal", {
            "anchor": anchor, "lessons": "x" * (web.JOURNAL_BODY_LIMIT + 1),
        })
        assert (status, payload["kind"]) == (413, "too-long")
        _, state = _get(base, "/api/state")
    assert state["journal"]["entries"][anchor]["lessons"] == "worth keeping", (
        "the refused write deleted the entry it was too long to replace"
    )


def test_the_lifecycle_cards_carry_the_anchor_the_journal_is_keyed_on(populated):
    """Without it the page has a journal it cannot attach to anything.

    Asserted against the state rather than against `position_groups` directly,
    because the payload is what the page reads and a key lost in serialization
    would pass a unit test on the grouping.
    """
    state = web.build_state(db_path=populated, archive_dir=RAW_DIR, query_id=None)
    lifecycles = state["lifecycles"]
    assert lifecycles, "the archive should form lifecycles"
    anchored = [lc for lc in lifecycles if lc["anchor"]]
    assert anchored, "no lifecycle carries an anchor, so nothing can be journalled"
    conn = connect(populated)
    known = {
        str(r["ib_order_id"]) for r in conn.execute(
            "SELECT DISTINCT ib_order_id FROM trades WHERE ib_order_id IS NOT NULL")
    }
    conn.close()
    unknown = sorted(lc["anchor"] for lc in anchored if lc["anchor"] not in known)
    assert not unknown, (
        f"these anchors name no fill in the journal: {unknown}. The endpoint "
        "resolves an entry's account from the fills, so it would refuse them"
    )


def test_the_journal_form_offers_exactly_the_fields_the_journal_stores():
    """The page's field table against `journal.FIELDS`, both directions.

    The two are separate lists in separate languages, and the endpoint refuses a
    name the journal does not declare -- so a field only the page knows about is a
    box whose text is rejected on Save, and a field only the journal knows about is
    a column no reader can ever fill.

    Names, not order: the page groups them into two moments and the table declares
    them in schema order, which is a presentation choice rather than drift.
    """
    from optjournal.journal import FIELDS

    js = _code_only(_js())
    table = js[js.index("const JFIELDS=["):js.index("];", js.index("const JFIELDS=["))]
    named = set(re.findall(r"name:'([a-z_]+)'", table))
    assert named == set(FIELDS), (
        f"the form and the table disagree. Only in the page: "
        f"{sorted(named - set(FIELDS))}; only in journal.FIELDS: "
        f"{sorted(set(FIELDS) - named)}"
    )


def test_every_journal_field_says_which_moment_it_belongs_to():
    """Two sections, and membership is DECLARED rather than sliced.

    The first version split the list by index (`slice(0,3)`), so inserting a field
    silently moved the boundary and a question meant for the close would appear at
    entry -- asking a reader to answer, before the trade, whether they followed a
    plan they had not written yet.
    """
    js = _code_only(_js())
    table = js[js.index("const JFIELDS=["):js.index("];", js.index("const JFIELDS=["))]
    rows = re.findall(r"name:'([a-z_]+)',at:'(entry|close)'", table)
    assert len(rows) == table.count("name:'"), (
        "a field in the table declares no moment, so it renders in neither section"
    )
    at_entry = {name for name, when in rows if when == "entry"}
    assert at_entry == {"plan_target", "plan_invalidation", "entry_note"}, (
        f"the entry section is {sorted(at_entry)}. Only what is known BEFORE the "
        "outcome belongs there -- that is what makes it worth reading afterwards"
    )


def test_the_form_reads_its_vocabulary_from_the_payload():
    """The trigger labels are not spelled twice.

    A label written in the page as well as in `journal.TRIGGERS` is a label that
    will disagree, and a VALUE written twice is an option whose write the server
    refuses -- with the reader's text in it. So the page renders `state.journal`'s
    own list, and the only vocabulary it hard-codes is the adherence fallback for a
    payload too old to carry one.
    """
    js = _code_only(_js())
    for label in ("Hit the profit target", "Time-based", "Tested side",
                  "Assigned or expired"):
        assert label not in js, (
            f"{label!r} is spelled in the page as well as in journal.TRIGGERS"
        )
    assert "JTRIGGERS()" in _fn("jfield"), "the options are not read from the payload"


def test_a_decision_with_no_anchor_says_so_instead_of_offering_a_button():
    """A snapshot-only position has no order to file writing under.

    A button that posts and fails would be worse than none: the reader would have
    typed a plan first. So the card explains, in the same place the button would
    be, that the archive holds no opening fills for this position.
    """
    src = _fn("journalRow")
    assert "if(!a)" in src.replace(" ", ""), "no guard for a card without an anchor"
    assert "only a snapshot" in src, (
        "the card offers no reason, so the missing button reads as a bug"
    )
    opens = src.index("data-jopen")
    assert src.index("only a snapshot") < opens, (
        "the guard must return before the button is rendered"
    )


def test_a_failed_journal_save_keeps_the_text_on_screen():
    """The one write on this page whose input cannot be recovered.

    A watchlist note refused is a note retyped from what is still on screen; a
    journal entry refused after the form closed is writing gone. So neither the
    network failure nor the server refusal closes the editor or reloads, and both
    say the text is still there -- while success closes it, because the act is
    finished and the badge now says what landed.
    """
    src = _fn("bindJournal")
    fail = src.index("Could not save")
    refused = src.index("was refused")
    closed = src.index("S.jrnl=null")
    assert fail < refused < closed, (
        "the editor is closed before the failure branches, so a refused save "
        "discards the writing it refused"
    )
    assert src.count("still on screen") == 2, (
        "one of the two failure paths does not tell the reader their text survived"
    )
    assert "await load()" in src[closed:], (
        "the reload must follow the success, or the badge never updates"
    )


def test_every_journal_field_is_one_element_with_an_id():
    """`preserveInputs` is keyed by `id`, so a field without one is unprotected.

    The adherence questions were three radios sharing a `name` first, which reads
    better and cannot be keyed: an unsaved pick reverted on the next redraw,
    silently, and Save then wrote the answer the reader had replaced. Text was
    protected and a one-word answer was not -- and the one-word answer is the half
    that lands in the adherence count.

    So every field is a textarea or a select with an id, one mechanism covers the
    whole form, and `bindJournal` reads them all the same way.
    """
    src = _fn("jfield")
    assert 'type="radio"' not in src, (
        "a radio has no id for preserveInputs to key on, so an unsaved pick is "
        "lost on the next redraw"
    )
    assert src.count('id="${esc(id)}"') == 2, (
        "every branch must give its field an id: one for the select, one for the "
        "textarea"
    )
    reader = _fn("bindJournal")
    assert "$('#j-'+jf.name)" in reader, (
        "the save reads fields by something other than their id, so the two halves "
        "of the form can disagree about what a field is called"
    )


def test_the_web_api_cannot_turn_dev_mode_on(populated, monkeypatch):
    """The `is_admin` lesson, applied: the untrusted surface cannot set the
    privileged flag.

    This server has no authentication, so a page open in another tab can POST
    here. `dev` gates developer-only surfaces, so if `/api/settings` accepted it
    that page could flip it. `_settings_write` names `query_id` and `scoring` by
    hand and writes nothing else, so `{dev:true}` is simply not a known setting --
    and the state it renders stays `dev:false`.

    Asserted through a real server, and both ways: the write is refused AND the
    payload is unchanged, so a future edit that started honouring `dev` here would
    fail the second half even if it returned 200.
    """
    monkeypatch.delenv("OPTJOURNAL_DEV", raising=False)
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        status, payload = _post(base, "/api/settings", {"dev": True})
        assert status == 400 and payload["kind"] == "empty", (
            "the API treated dev as a known setting; it must not be settable here"
        )
        _, state = _get(base, "/api/state")
    assert state["dev"] is False, "a web POST turned dev mode on"


def test_the_state_carries_dev_off_by_default_and_on_when_the_env_says_so(
    populated, monkeypatch
):
    """`build_state` resolves the flag per request, so the env is enough to flip
    it without a file -- and it is off when nothing says otherwise."""
    monkeypatch.delenv("OPTJOURNAL_DEV", raising=False)
    off = web.build_state(db_path=populated, archive_dir=RAW_DIR, query_id=None)
    assert off["dev"] is False

    monkeypatch.setenv("OPTJOURNAL_DEV", "1")
    on = web.build_state(db_path=populated, archive_dir=RAW_DIR, query_id=None)
    assert on["dev"] is True


def test_the_footer_marks_dev_mode_only_when_it_is_on():
    """A visible, always-on marker -- so a dev never wonders, and never ships a
    screenshot not knowing. Off, the footer reads exactly as before.

    The marker is gated on the payload's own `dev`, not on anything the page
    decides, so it cannot disagree with the flag the server resolved.
    """
    js = _code_only(_js())
    foot = js[js.index("$('#foot')"):js.index("$('#foot')") + 260]
    assert "st.dev?" in foot and "dev mode" in foot, (
        "the footer does not render a dev marker off the payload's dev flag"
    )
    # And it is conditional: a friend's footer carries no marker.
    assert "':''" in foot, "the dev marker is not gated, so it always shows"


def test_dev_mode_opens_the_diagnostics_block_rather_than_hiding_it():
    """Dev mode REVEALS -- it opens the Advanced diagnostics by default. It must
    not gate the whole block away, because a friend still wants to check whether a
    statement ingested."""
    panel = _fn("settingsPanel")
    assert 'class="adv mt-5"${st.dev?\' open\':\'\'}' in panel, (
        "the Advanced block is not opened by dev mode (or is hidden by it)"
    )
    # The block itself is unconditional: its content shows for everyone.
    assert "Advanced — journal details and archive" in panel


# --------------------------------------------------------------------------
# POST /api/settings/token -- the settings page storing a Flex token.
#
# The credential path, so these tests are written to a different standard than
# the rest of this file: every one of them either proves the value goes where it
# is supposed to, or proves it does NOT go somewhere it must not. A test suite
# that only checked the happy path here would be green while the token sat in a
# log file.
#
# `keyring.set_password` is patched in every test that reaches it. Not for speed:
# the suite must never write to the developer's own credential store, which is
# the one piece of state on this machine that `tmp_path` cannot isolate.
# --------------------------------------------------------------------------

def _no_keyring_writes(monkeypatch) -> list[tuple[str, str, str]]:
    """Capture keyring writes instead of performing them. Returns the log."""
    import keyring  # noqa: PLC0415 - local to the credential tests

    wrote: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        keyring, "set_password",
        lambda service, account, token: wrote.append((service, account, token)),
    )
    return wrote


@pytest.mark.parametrize(("body", "expected"), [
    ({}, "no token in the request"),
    ({"token": ""}, "no token in the request"),
    ({"token": "   \n  "}, "no token in the request"),
    ({"token": "1" * 129}, "characters"),
])
def test_the_token_endpoint_refuses_a_request_without_storing_anything(
    populated, monkeypatch, body, expected,
):
    """A refusal must leave the PREVIOUS token alone.

    The failure this rules out is the ugly one: a blank Save press overwriting a
    working token with an empty string, so the journal stops syncing because
    somebody clicked the wrong button. Hence `present: null` rather than `false` --
    nothing was written, so this reply knows nothing about what is stored, and
    saying `false` would send the reader off to re-enter a credential that is
    still there and still fine.
    """
    wrote = _no_keyring_writes(monkeypatch)
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        status, payload = _post(base, "/api/settings/token", body)
    assert status == 400
    assert payload["ok"] is False
    assert payload["kind"] == "token"
    assert payload["present"] is None
    assert expected in payload["message"]
    assert wrote == [], "a refused request still wrote to the keyring"


def test_the_token_endpoint_stores_a_pasted_token_stripped(populated, monkeypatch):
    """The happy path, and the whitespace is the interesting half.

    A token arrives PASTED -- out of Client Portal, into a browser field -- and a
    paste carries a trailing newline often enough that IBKR rejecting the result
    as invalid would read, to the person who just pasted it correctly, as the
    token being wrong. Stripped once, in `flex.write_token`, so the terminal
    prompt gets the same treatment.
    """
    import getpass  # noqa: PLC0415 - local to this test

    wrote = _no_keyring_writes(monkeypatch)
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        status, payload = _post(base, "/api/settings/token",
                                {"token": "  123456789012345\n"})
    assert status == 200
    assert payload["ok"] is True
    assert payload["present"] is True
    assert payload["account"] == getpass.getuser()
    assert wrote == [("ibkr-flex-token", getpass.getuser(), "123456789012345")]


def test_storing_a_token_never_echoes_it_back(populated, monkeypatch):
    """The reply is read by a browser and kept in devtools history.

    Nothing about a save needs the value, so the reply must not carry it -- not in
    a confirmation message, not as a masked prefix, not anywhere. Asserted over the
    whole serialised body rather than key by key, because the next key added here
    would not be covered by a per-key check.
    """
    _no_keyring_writes(monkeypatch)
    secret = "987654321098765"
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        _status, payload = _post(base, "/api/settings/token", {"token": secret})
    assert secret not in json.dumps(payload)


def test_storing_a_token_never_logs_it(populated, monkeypatch, caplog):
    """A credential in `logs/optjournal.log` is a credential on disk in the clear.

    This journal logs generously and the log is long-lived -- the real one carries
    months of scheduler history -- so the one thing the token endpoint must never
    do is mention its input. The account name it may log, and does: that is what
    tells a reader which keyring entry to look at.
    """
    _no_keyring_writes(monkeypatch)
    secret = "555000111222333"
    with caplog.at_level("DEBUG"):
        with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
            status, _payload = _post(base, "/api/settings/token",
                                     {"token": secret})
    assert status == 200
    assert secret not in caplog.text


def test_a_keyring_that_will_not_answer_reports_it_rather_than_hanging(
    populated, monkeypatch,
):
    """The measured failure: a keychain waiting on an unlock blocks the call.

    Unbounded, the request stays open with no reply and the Save button spins
    until the tab is closed. So the write gets the same deadline as the read, and
    the timeout says NOTHING WAS STORED -- with `present: null`, because a call
    that never returned cannot report what is in the keyring.

    The deadline is shortened here rather than the sleep lengthened: the point is
    that the handler gives up, not how long four seconds takes.
    """
    import time  # noqa: PLC0415 - local to this test

    import keyring  # noqa: PLC0415

    monkeypatch.setattr(web, "KEYRING_TIMEOUT_S", 0.05)
    monkeypatch.setattr(
        keyring, "set_password",
        lambda service, account, token: time.sleep(5),
    )
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        status, payload = _post(base, "/api/settings/token", {"token": "12345"})
    assert status == 503
    assert payload["ok"] is False
    assert payload["kind"] == "keyring"
    assert payload["present"] is None
    assert "Nothing was stored" in payload["message"]


def test_the_settings_panel_offers_a_token_field_that_does_not_render_a_value():
    """The page half, pinned where the copy cannot quietly diverge from it.

    Three properties, each answering an objection that kept this out of the page
    for a while: the input is a PASSWORD field so a shoulder or a screenshot does
    not carry it, `autocomplete` is off so the browser does not offer it back in
    another form, and the markup interpolates NO value -- a round trip through the
    page would mean the server had sent the token, which no endpoint does.
    """
    panel = _fn("settingsPanel")
    assert 'id="tok"' in panel, "the settings panel offers no token field"
    assert 'id="toksave"' in panel, "the token field has no save button"
    assert 'type="password"' in panel, "the token field is not masked"
    assert 'autocomplete="off"' in panel
    # The `value=` attribute is what a rendered credential would need. The query
    # id row legitimately has one, so this is scoped to the token input's tag.
    tag = panel[panel.index('id="tok"'):]
    assert "value=" not in tag[:tag.index(">")], (
        "the token input renders a value, which would mean the payload carries one"
    )


def test_the_token_save_handler_clears_the_field_and_reports_the_server():
    """What happens after the POST, which is where a credential lingers.

    The field is cleared ON SUCCESS ONLY -- keeping it on failure is deliberate,
    so fixing a paste that picked up one stray character does not mean another
    trip to Client Portal -- and the failure message is the SERVER's, because the
    server holds the length rule and a copy here would be a second rule to keep in
    step.
    """
    js = _code_only(_js())
    handler = js[js.index("toksave.onclick"):js.index("const tokcheck")]
    assert "'/api/settings/token'" in handler, "the save button posts elsewhere"
    assert "method:'POST'" in handler
    assert "el.value=''" in handler, "the token stays in the form field"
    assert "tok.message" in handler, "the page invents its own failure message"
    # Cleared after the ok check, not before it: the ordering IS the property.
    assert handler.index("if(!tok.ok)") < handler.index("el.value=''")


def test_a_sync_from_the_page_writes_the_ledger_row_the_scheduler_reads(
    tmp_path, monkeypatch,
):
    """The endpoint half of the outage: work done, nothing recorded.

    `POST /api/sync` fetched, ingested and reported -- and wrote no `job_runs` row,
    so `consecutive_failures` stayed where a backed-off scheduler had left it. The
    journal was syncing on demand and the schedule stayed dead, which is the
    hardest version of this bug to notice: everything a person touches works.

    `sync_journal` is patched out rather than reached: the real one spends an IBKR
    request, and what is under test is the bookkeeping around it.
    """
    from conftest import connect_migrated  # noqa: PLC0415 - local to this test

    from optjournal.jobs import FAILURE_BACKOFF  # noqa: PLC0415
    from optjournal.web import _do_sync  # noqa: PLC0415 - private by design

    db = tmp_path / "j.db"
    conn = connect_migrated(db)
    conn.execute("INSERT OR REPLACE INTO job_state (job, last_status,"
                 " consecutive_failures) VALUES ('sync', 'failed', ?)",
                 (FAILURE_BACKOFF,))
    conn.commit()
    conn.close()

    monkeypatch.setattr(web, "sync_journal", lambda **kw: {
        "changed": True, "summary": "3 new trade(s), 1 new cash row(s)",
        "new_trades": 3, "new_cash": 1,
    })
    reply = _do_sync(db_path=db, archive_dir=RAW_DIR, query_id="1591754",
                     assets=("OPT",))
    assert reply["new_trades"] == 3, "the page's own reply changed shape"

    after = connect_migrated(db)
    assert after.execute(
        "SELECT consecutive_failures FROM job_state WHERE job='sync'"
    ).fetchone()[0] == 0, (
        "a sync from the page did not clear the scheduler's backoff"
    )
    row = after.execute(
        "SELECT job, status, fired_for FROM job_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert (row["job"], row["status"], row["fired_for"]) == ("sync", "ok", None)


def test_a_refused_sync_from_the_page_is_recorded_too(tmp_path, monkeypatch):
    """A cooldown and a rejected token are outcomes, not silences.

    Recording only the successes would leave the ledger describing a job that
    apparently never fails -- and `job_runs.detail` is where the reason has to be,
    because it is what the backoff warning now reads back.
    """
    from conftest import connect_migrated  # noqa: PLC0415

    from optjournal.flex import TokenRejected  # noqa: PLC0415
    from optjournal.web import _do_sync  # noqa: PLC0415

    db = tmp_path / "j.db"
    connect_migrated(db).close()

    def rejected(**kwargs):
        raise TokenRejected("IBKR says your Flex token is expired")

    monkeypatch.setattr(web, "sync_journal", rejected)
    reply = _do_sync(db_path=db, archive_dir=RAW_DIR, query_id="1591754",
                     assets=("OPT",))
    assert reply["kind"] == "config" and reply["ok"] is False

    row = connect_migrated(db).execute(
        "SELECT status, detail FROM job_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "failed"
    assert "credentials:" in row["detail"]


def test_both_hand_run_sync_paths_record_what_they_did():
    """The page and the CLI, pinned together, because they failed together.

    Two entry points to one piece of work, and both were invisible to the ledger.
    A pin on each is cheap insurance that a future edit to one does not quietly
    reintroduce the asymmetry that made a two-week outage look like a working
    journal.
    """
    import inspect  # noqa: PLC0415

    from optjournal.cli import cmd_sync  # noqa: PLC0415
    from optjournal.web import _do_sync  # noqa: PLC0415

    for fn in (_do_sync, cmd_sync):
        assert "record_manual_sync" in inspect.getsource(fn), (
            f"{fn.__name__} no longer records its run, so a sync through it "
            f"leaves the scheduler's backoff counter untouched"
        )


# --------------------------------------------------------------------------
# Same-session fills on the page: the estimate has to be visible.
# --------------------------------------------------------------------------

def test_the_payload_reports_how_much_of_the_journal_is_provisional(tmp_path):
    """Two counts, because they are two different claims.

    `fills` is what makes a base-currency TOTAL approximate; `unsettled` is what has
    no realised P&L yet. A journal with a EUR confirm on a EUR account has the second
    without the first, so collapsing them into one number would either overstate the
    uncertainty or hide it.
    """
    from conftest import connect_migrated  # noqa: PLC0415

    db = tmp_path / "j.db"
    conn = connect_migrated(db)
    conn.execute(
        "INSERT INTO statements (broker, source_file, sha256, account_id, from_date,"
        " to_date, when_generated, base_currency, asset_filter, ingested_at)"
        " VALUES ('ibkr','confirm-20260924.xml','d','U1','20260924','20260924',"
        " '20260924;1112','EUR','ALL','2026-09-24T12:00:00+00:00')"
    )
    for trade_id, kind, estimated in (("1", "confirm", 1), ("2", "confirm", 0),
                                      ("3", "activity", 0)):
        conn.execute(
            "INSERT INTO trades (broker, trade_id, account_id, trade_date,"
            " asset_category, symbol, quantity, currency, fx_rate_to_base, raw,"
            " source_file, first_seen_at, source_kind, fx_rate_estimated)"
            " VALUES ('ibkr',?,'U1','20260924','OPT','GOOG',1,'USD',0.88,'{}',"
            " 'confirm-20260924.xml','2026-09-24T12:00:00+00:00',?,?)",
            (trade_id, kind, estimated),
        )
    conn.commit()
    conn.close()

    state = build_state(db_path=db, archive_dir=RAW_DIR, query_id=None)
    pv = state["provisional"]
    assert pv["fills"] == 1, "the estimated-rate count is wrong"
    assert pv["unsettled"] == 2, "a base-currency confirm was not counted unsettled"
    assert pv["newest"] == "20260924"


def test_a_settled_journal_reports_nothing_provisional(populated):
    """The steady state, and the banner must be silent in it.

    Every row in the fixture came from an Activity Statement, so a page that warned
    about estimates here would be crying wolf on a journal with none -- which is how
    a warning stops being read.
    """
    state = build_state(db_path=populated, archive_dir=RAW_DIR, query_id=None)
    assert state["provisional"] == {"fills": 0, "unsettled": 0, "newest": None}


def test_the_stats_caption_warns_only_when_something_is_provisional():
    """The estimate reaches a headline card, so the card has to admit it.

    `docs/design-notes.md` quarantines modelled numbers precisely so none can reach
    one. Same-session fills are the sanctioned exception, and the condition of the
    exception is this banner -- so it is pinned: silent at zero, and naming the
    estimate when there is one.
    """
    banner = _fn("provisionalBanner")
    assert "if(!pv.unsettled) return ''" in banner, (
        "the banner is not silent on a journal with no provisional fills"
    )
    assert "pv.fills" in banner, "the banner ignores the estimated-rate count"
    assert "estimated" in banner and "FX" in banner, (
        "the banner does not say that the base-currency figures are estimated"
    )
    # And it is actually rendered into the stats caption, not merely defined.
    assert "provisionalBanner()" in _code_only(_js()).replace(banner, ""), (
        "provisionalBanner is defined but never called"
    )


def test_the_order_views_carry_provisionality_up_from_the_fills(tmp_path):
    """One estimated fill makes the order's totals estimated.

    The view columns are SUMs, so a single unconverted row taints the total it lands
    in -- MAX over the group is what lets the page mark the order rather than
    silently presenting a mixed figure as settled.
    """
    from conftest import connect_migrated  # noqa: PLC0415

    conn = connect_migrated(tmp_path / "j.db")
    conn.execute(
        "INSERT INTO statements (broker, source_file, sha256, account_id, from_date,"
        " to_date, when_generated, base_currency, asset_filter, ingested_at)"
        " VALUES ('ibkr','f.xml','d','U1','20260924','20260924','g','EUR','ALL','t')"
    )
    # One order, two fills on one contract: one settled, one same-session.
    for trade_id, kind, estimated in (("1", "activity", 0), ("2", "confirm", 1)):
        conn.execute(
            "INSERT INTO trades (broker, trade_id, ib_order_id, conid, account_id,"
            " trade_date, date_time, asset_category, symbol, quantity, trade_price,"
            " currency, fx_rate_to_base, raw, source_file, first_seen_at,"
            " source_kind, fx_rate_estimated)"
            " VALUES ('ibkr',?, 'O1','C1','U1','20260924','20260924;10','OPT','GOOG',"
            " 1, 4.9, 'USD', 0.88, '{}', 'f.xml', 't', ?, ?)",
            (trade_id, kind, estimated),
        )
    conn.commit()

    leg = conn.execute("SELECT * FROM trade_legs").fetchone()
    order = conn.execute("SELECT * FROM trade_orders").fetchone()
    assert leg["fx_rate_estimated"] == 1, "an estimated fill did not taint its leg"
    assert leg["settled"] == 0, "a leg holding a same-session fill reads as settled"
    assert order["fx_rate_estimated"] == 1
    assert order["settled"] == 0


def test_the_settings_endpoint_stores_and_clears_the_confirm_query(populated):
    """Clearing it is how the intraday poll is turned off, so a blank must save.

    Unlike the token field, which refuses an empty value: an absent confirm query is
    a supported configuration and the only way to say "statement only" from the page.
    """
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        status, saved = _post(base, "/api/settings",
                              {"confirm_query_id": "1621016"})
        assert (status, saved["ok"]) == (200, True)
        assert saved["stored"]["confirm_query_id"] == "1621016"

        status, bad = _post(base, "/api/settings", {"confirm_query_id": "not-an-id"})
        assert (status, bad["kind"]) == (400, "confirm_query_id")

        status, cleared = _post(base, "/api/settings", {"confirm_query_id": ""})
        assert (status, cleared["ok"]) == (200, True)
        assert cleared["stored"].get("confirm_query_id") is None, (
            "an empty confirm query id was not stored as absent, so the poll "
            "cannot be turned off from the page"
        )


def test_the_server_and_the_page_agree_on_the_dashboard_tiles():
    """Two copies of one vocabulary, pinned together like `sweep.TABS`.

    The page needs the labels and the server needs the keys to refuse, and
    neither can import the other. Drift in one direction stores a tile the page
    cannot render (it falls back to the default, and the reader's choice silently
    vanishes); in the other, the page offers a tile the server refuses to save.
    The step and the default are pinned too, because the server stores the
    default as absence and must recognise exactly the arrangement the page means.
    """
    keys, _, step = _tile_registry()
    assert tuple(keys) == web.DASHBOARD_TILES, (
        "page.html's TILES and web.DASHBOARD_TILES disagree -- change both"
    )
    assert step == web.TILE_STEP
    default = int(re.search(r"const TILE_DEFAULT=TILES\.slice\(0,(\d+)\)", _js()).group(1))
    assert web.DASHBOARD_TILES[:default] == web.TILE_DEFAULT


@pytest.mark.parametrize("tiles, says", [
    ("net_pnl", "list"),
    (["net_pnl", 3, "trades", "wins"], "list"),
    (["net_pnl", "trades", "wins", "sharpe"], "unknown tile(s): sharpe"),
    (["net_pnl", "net_pnl", "trades", "wins"], "twice"),
    (["net_pnl", "trades", "wins"], "3 tiles leaves an empty cell"),
])
def test_a_tile_list_the_grid_cannot_hold_is_refused_with_its_reason(tiles, says):
    """Each rule is one the page depends on, and the reason is what it shows."""
    problem = web._tiles_problem(tiles)
    assert problem and says in problem, f"{tiles!r} -> {problem!r}"


def test_a_tile_list_the_grid_can_hold_is_accepted():
    assert web._tiles_problem(list(web.DASHBOARD_TILES)) is None, "every tile"
    assert web._tiles_problem(["inflight", "red_days", "wins", "trades"]) is None


def test_the_settings_endpoint_stores_tiles_and_stores_the_default_as_absence(populated):
    """A chosen arrangement round-trips; the default and a reset both store nothing.

    Absence for the default, as for `scoring`: a stored copy of today's default
    would freeze it for this reader when the default later changes. A refused list
    must leave the stored one exactly as it was -- a 400 that half-applied would be
    a grid with a hole in it on the next load.
    """
    four = ["win_rate", "net_pnl", "profit_factor", "avg_pnl"]
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        status, saved = _post(base, "/api/settings", {"tiles": four})
        assert (status, saved["ok"]) == (200, True)
        assert saved["stored"]["tiles"] == four
        assert _get(base, "/api/state")[1]["settings"]["tiles"] == four

        status, bad = _post(base, "/api/settings", {"tiles": four[:3]})
        assert (status, bad["kind"]) == (400, "tiles")
        assert _get(base, "/api/state")[1]["settings"]["tiles"] == four, (
            "a refused tile list changed what was stored"
        )

        status, same = _post(base, "/api/settings", {"tiles": list(web.TILE_DEFAULT)})
        assert (status, same["ok"]) == (200, True)
        assert "tiles" not in same["stored"], "the default was stored as a copy"

        _post(base, "/api/settings", {"tiles": four})
        status, reset = _post(base, "/api/settings", {"tiles": None})
        assert (status, reset["ok"]) == (200, True)
        assert "tiles" not in reset["stored"], "a reset left a stored arrangement"


def test_the_provisional_banner_dashes_ibkrs_date():
    """IBKR sends `20260924`; every other date on this page is dashed.

    A caption mixing the two spellings reads like a leaked internal field, which is
    exactly the impression a provisional-data warning must not give.
    """
    banner = _fn("provisionalBanner")
    assert "raw.slice(0,4)" in banner and "raw.slice(4,6)" in banner, (
        "the banner renders IBKR's compact date without reformatting it"
    )


# --------------------------------------------------------------------------
# The 0DTE calculator's wiring
#
# Its controls are the page's only two-way surface outside the watchlist and the
# journal form: four typed fields, a sort, a toggle, two resets, two pop-outs, and
# a draggable strike in every ladder row. None of that is reachable by the checks
# that read rendered markup, because a handler attached to a selector that matches
# nothing renders perfectly and does nothing -- the same silent no-op an unstyled
# class is, one layer down. So the two halves are pinned against each other.
# --------------------------------------------------------------------------


#: Every hook the calculator's markup emits, and what it is for. The binder is
#: checked against this list in both directions, so a renamed attribute breaks a
#: test rather than a control.
_ZDTE_HOOKS = {
    "data-zread": "the four typed fields",
    "data-zstrike": "the draggable strike cell",
    "data-zreset": "back to the feed's reading",
    "data-zsort": "the strike column's direction",
    "data-zall": "collapse or reveal the outer rails",
    "data-zpop": "the Broker Companion",
    "data-zretry": "check the feed again after a stale reading",
}


@pytest.mark.parametrize("hook", sorted(_ZDTE_HOOKS), ids=lambda h: h)
def test_every_calculator_hook_is_both_rendered_and_bound(hook):
    """A control needs an attribute in `odte()` and a handler in `bindZdte()`."""
    view = _fn("odte")
    binder = _fn("bindZdte")
    assert hook in view, (
        f"{hook} ({_ZDTE_HOOKS[hook]}) is bound but never rendered, so the "
        "handler attaches to nothing"
    )
    assert hook in binder, (
        f"{hook} ({_ZDTE_HOOKS[hook]}) is rendered but never bound, so the "
        "control is inert"
    )


def test_the_calculator_binds_no_hook_it_does_not_render():
    """The other direction, over whatever the binder actually reaches for."""
    bound = set(re.findall(r"data-(z[a-z]+)", _fn("bindZdte")))
    declared = {hook.removeprefix("data-") for hook in _ZDTE_HOOKS}
    assert bound <= declared, (
        f"bindZdte reaches for {sorted(bound - declared)}, which is not in this "
        "test's hook table -- add it there and to the view, or drop the handler"
    )


def test_typing_a_reading_redraws_and_the_caret_survives():
    """The ladder is recomputed per keystroke, which is only usable if the field
    keeps its text and its cursor: `#body` is replaced wholesale on every draw.

    `preserveInputs` does the carrying, so every field needs an id -- and the two
    readings need `setSelectionRange`, because masking out a stray character
    shortens the value and the caret would otherwise jump to the end.
    """
    view = _fn("odte")
    for field in ('id="${id}"', 'id="z${side}"'):
        assert field in view, f"a calculator field has no id ({field})"
    binder = _fn("bindZdte")
    assert "el.oninput" in binder, "typing does not recompute the ladder"
    assert "sanitizeLevel" in binder, "the field accepts characters it cannot parse"
    assert "setSelectionRange" in binder, "the caret jumps on a masked keystroke"


def test_a_programmatic_fill_writes_the_node_as_well_as_the_state():
    """The trap `zfill` exists for, pinned so it cannot be quietly undone.

    `restoreInputs` writes the PRE-render text back into every field it finds by
    id, unconditionally. A handler that set only `S.zcall` would therefore render
    the dropped strike into the markup and have it overwritten a line later by the
    stale value still in the old input -- so dragging a strike into a pad would
    look like nothing happened. Setting the live node too is what keeps the two
    agreeing at the only moment the preserver looks.
    """
    fill = _fn("zfill")
    assert "el.value=" in fill.replace(" ", ""), (
        "zfill no longer writes the field, so restoreInputs will revert every "
        "drag, double-click and reset"
    )
    assert "draw()" in fill


def test_the_companion_window_is_reused_and_kept_in_step():
    """One window, and it follows what is typed here.

    Opening a second window per click would leave two disagreeing readouts over
    the broker; opening it once and never posting into it would leave one readout
    that silently stops matching the ladder it came from.
    """
    opener = _fn("openCompanion")
    assert "S.zpop&&!S.zpop.closed" in opener.replace(" ", ""), (
        "a second click opens a second window"
    )
    assert "bitacora-companion" in opener, "the window is not named, so it cannot be reused"
    assert "noopener" not in opener, (
        "noopener would drop the handle companionPost needs"
    )
    post = _fn("companionPost")
    assert "location.origin" in post, "the message must be addressed to this origin"
    assert "companionPost();" in _fn("draw"), (
        "the floating pads stop following what is typed on the tab"
    )


def test_the_typed_readings_ride_in_the_hash_and_heal_back_out():
    """A reload lands on the ladder you left, and a reading that agrees with the
    feed stops claiming to be an override.

    Both halves matter. Without the write, the four fields are lost on every
    reload and a link carries none of them; without the heal, retyping the feed's
    own close leaves `#spx=` in the address bar for the rest of the day, and a
    link shared tomorrow pins yesterday's number as an override of a close that
    has since moved.
    """
    sync = _fn("syncHash")
    for key in ("spx", "vix", "call", "put"):
        assert f"hs.set('{key}'" in sync, f"the hash does not carry {key}"
    assert "zfeed(oc.spx_prev_close)" in _fn("draw"), (
        "a typed reading identical to the feed's no longer heals out of the URL"
    )
    apply_hash = _fn("applyHash")
    assert "sanitizeLevel" in apply_hash, (
        "a hand-edited hash could put a non-number in the field"
    )


def test_the_calculator_explains_itself_on_hover_rather_than_on_the_page():
    """Its two explanations live in tips, and the figures stay on the page.

    They were paragraphs under the readings and under the ladder: ~160 words of
    correct, once-useful prose that a seller rereads every session and then has to
    look past. The reasoning is now behind two `i` triggers -- the idiom the
    Statistics heading already uses -- and what stays visible is the expected move
    itself, which is a reading rather than an argument.

    Pinned because the pressure runs one way: the next thing worth saying about the
    ladder is easiest to add as another sentence under it.
    """
    view = _fn("odte")
    assert view.count('class="tip wide"') == 2, (
        "the calculator's two explanations are no longer both in hovers"
    )
    # Exactly one `.note` survives, and it is the empty state: an instruction for a
    # journal whose index bars have not landed, which has no figure to hide behind
    # and nothing on the tab works without.
    notes = view.count('class="note"')
    assert notes == 1 and "Not available yet" in view, (
        f"{notes} prose blocks on the tab -- explanation belongs in one of the two "
        "tips; only the 'run optjournal bars' instruction stays on the page"
    )
    # Keyboard-reachable, or the explanation exists only for a mouse.
    assert view.count('class="info tipped"\n      tabindex="0"') + view.count(
        'class="info tipped" tabindex="0"') == 2, (
        "an info trigger is not focusable, so it cannot be opened from a keyboard"
    )
    # The reading itself is NOT in the tip.
    assert "expected move</span>" in view and "zfig" in view, (
        "the expected move has to stay on the page; only its reasoning hides"
    )


def test_the_calendar_strip_shows_us_releases_only():
    """This tab sells SPX, so a Swiss rate decision is not news on it.

    The journal's feed is worldwide -- 23 entries on the day this was written, most
    of them irrelevant to an SPX seller -- and all of them as chips buried the two
    readings under six rows. The Market tab is where the whole day lives, and the
    strip names it.
    """
    view = _fn("odte")
    assert "sessionEvents(oc.events_today)" in view, (
        "the strip no longer asks the module which releases belong on this tab, so "
        "the whole world is back on it"
    )
    assert "more US" in view, "the rest of the day is not accounted for"
    # The rule itself, and the Fed-speaker merge with it, live in the module where
    # node runs them -- see tests/frontend/zdte.test.mjs.
    module = (ROOT / "src" / "optjournal" / "static" / "zdte.js").read_text(encoding="utf-8")
    assert 'SESSION_COUNTRY = "USD"' in module, "the country filter is gone"


def test_every_font_the_stylesheet_names_is_shipped_served_and_licensed(populated):
    """Each `@font-face` source is a file under static/, of a type the server
    will send, with the Open Font License beside it.

    Three failures, each silent. A missing file falls back to the system stack
    with no error on screen, so the page just looks slightly different on every
    machine again. A missing STATIC_TYPES entry is the same fallback, via a 404.
    And OFL 1.1 permits redistributing Geist only with its license travelling
    alongside, so shipping the files without it is the one way this change could
    stop being allowed to ship at all.
    """
    css = _css()
    sources = re.findall(r"@font-face\{[^}]*src:url\(([^)]+)\)", css.replace("\n", ""))
    assert len(sources) == 2, f"expected the two faces' files, found {sources}"
    static = Path(web.__file__).parent / "static"
    for src in sources:
        assert src.startswith("/static/"), f"{src} is not same-origin static"
        path = static / src[len("/static/"):]
        assert path.is_file(), f"{src} is named by app.css but not shipped"
        assert path.suffix in web.STATIC_TYPES, f"{src} would be answered with a 404"
        assert (path.parent / "OFL.txt").is_file(), (
            f"{src} ships without the Open Font License beside it"
        )
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        import urllib.request  # noqa: PLC0415 - local, like _post's
        for src in sources:
            with urllib.request.urlopen(base + src, timeout=10) as res:  # noqa: S310
                assert res.headers["Content-Type"] == "font/woff2"


def test_the_body_and_the_tooltip_name_the_same_face():
    """The tooltip spells its family out rather than inheriting it -- its trigger
    can be a monospace glyph -- so its stack has to track the body's by hand, and
    a change to one that skips the other puts every tooltip in a different face
    from the text around it. Both lead with Geist, and so does `--fig`, which is
    the same face with tabular figures.
    """
    css = _css().replace("\n", "")
    body = re.search(r"html,body\{[^}]*font:14px/1\.5 ([^;}]+)", css).group(1).strip()
    tip = re.search(r"\.tip\{[^}]*font-family:([^;}]+)", css).group(1).strip()
    fig = re.search(r"--fig:([^;]+);", css).group(1).strip()
    assert body == tip == fig, f"body {body!r}, tooltip {tip!r} and --fig {fig!r} differ"
    assert body.startswith('"Geist",')
    assert re.search(r"--mono:([^;]+);", css).group(1).strip().startswith('"Geist Mono",')


def test_the_header_figure_cannot_break_between_its_sign_and_its_number():
    """The `+` is `.signed::before`, a separate box the line can break after, and at
    a 1200px viewport it did: a lone `+` above `€3,695.08`. Invisible at the width
    the page is usually built at, so pinned rather than remembered."""
    css = _css().replace(" ", "").replace("\n", "")
    assert "white-space:nowrap" in css.split(".pfig{", 1)[1].split("}", 1)[0]
    # And the header gives the period its own row when one row cannot hold it --
    # only when there IS a period, so the six tabs without one grow no dead row.
    assert ".brand:has(.period:not(:empty)){" in css


def test_the_strategy_ranking_sums_the_same_money_as_the_scoreboard(state):
    """The ranking reads the lifecycles; the tiles read month_stats. Both claim to
    count decided positions' realised P&L, so over all time the decided cards must
    sum to what Avg P&L per Trade implies -- or the Best Strategy tile is ranking a
    different population from the tiles beside it. Recomputed, not pinned.
    """
    at = state["all_time"]
    if at["scoring"] != "position" or not at["decided_campaigns"]:
        pytest.skip("needs decided positions under position scoring")
    decided = [lc for lc in state["lifecycles"]
               if lc["status"] == "closed" and lc["realized_pnl"]]
    assert sum(lc["realized_pnl"]["base"] for lc in decided) == pytest.approx(
        at["avg_pnl"]["base"] * at["decided_campaigns"])
    best = at["best_strategy"]
    assert best and best["pnl"]["base"] >= (at["worst_strategy"] or best)["pnl"]["base"]
    # And the largest outcomes bound the averages they were chosen from.
    if at["largest_win"]:
        assert at["largest_win"]["base"] >= at["avg_win"]["base"]
    if at["largest_loss"]:
        assert at["largest_loss"]["base"] <= at["avg_loss"]["base"]


def test_each_stats_block_ranks_strategies_over_its_own_period():
    """`stats` is the selected month and `all_time` is everything; ranking both over
    all time would put a lifetime answer on a one-month dashboard."""
    src = inspect.getsource(web.build_state).replace(" ", "")
    assert 'for block, period in (("stats", selected), ("all_time", None)):'.replace(" ", "") in src
    assert "strategy_ranking(state[\"lifecycles\"],period)" in src


def _media_rules(width: int) -> list[tuple[str, str]]:
    """(selector, body) for every rule inside `@media(max-width:<width>px)`."""
    css = re.sub(r"/\*.*?\*/", "", _css(), flags=re.S)
    rules: list[tuple[str, str]] = []
    for m in re.finditer(rf"@media\s*\(max-width:\s*{width}px\)\s*\{{", css):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        rules += re.findall(r"([^{}]+)\{([^{}]*)\}", css[m.end():i - 1])
    return [(sel.strip(), body) for sel, body in rules]


def test_the_content_column_can_shrink_below_its_widest_child():
    """A `1fr` track has an `auto` minimum, so one wide table widened `.wrap`
    past the viewport and the whole page scrolled sideways. `minmax(0,1fr)` is
    what moves the overflow down to the table that owns it.

    Ablated by restoring `auto 1fr`: this fails.
    """
    shell = [b for s, b in _css_rules() if s.strip() == ".shell"]
    assert shell, "no .shell rule"
    for body in shell:
        cols = re.search(r"grid-template-columns:([^;}]+)", body)
        if cols:
            tracks = re.sub(r"minmax\([^)]*\)", "", cols.group(1))
            assert "1fr" not in tracks, f".shell has a bare 1fr track: {cols.group(1)}"


def test_a_tooltip_does_not_inherit_nowrap_from_its_pill():
    """A `.tip` inside a `.pill` inherited `white-space:nowrap`, ran as one
    line, and widened the page even while hidden.

    Ablated by dropping the declaration: this fails.
    """
    base = [b for s, b in _css_rules() if s.strip() == ".tip"]
    assert any(re.search(r"white-space:\s*normal", b) for b in base)


def test_below_1180px_tables_scroll_in_place_and_tips_keep_their_heading():
    """Tables get their own scroller rather than the card, because a scrolling
    card clips the tips that hang out of it. The tips then anchor to the card,
    and `top:auto` keeps them at their static position under the heading
    instead of at the card's foot.

    Ablated by removing each rule in turn: each assertion fails on its own.
    """
    rules = _media_rules(1180)
    tables = [b for s, b in rules if s.startswith("table")]
    assert any("display:block" in b and "overflow-x:auto" in b for b in tables)
    assert any(s == ".card" and "position:relative" in b for s, b in rules)
    tips = [b for s, b in rules if ".tip" in s and "top:auto" in b]
    assert tips, "no tip keeps its static top below 1180px"


def test_below_760px_the_rail_becomes_a_strip():
    """The rail stays a fixed-width column otherwise, and a phone loses a
    third of its width to it.

    Ablated by removing the rule: this fails.
    """
    rules = _media_rules(760)
    assert any(s == ".shell" and "minmax(0,1fr)" in b for s, b in rules)
    assert any(s == ".rail" and "flex-direction:row" in b for s, b in rules)


def _two_cards_on_one_underlying(state) -> tuple[str, str]:
    by_under: dict[str, list[str]] = {}
    for lc in state["lifecycles"]:
        if lc["anchor"]:
            by_under.setdefault(lc["underlying"], []).append(lc["anchor"])
    pairs = [sorted(a, key=lambda o: (len(o), o))[:2]
             for a in by_under.values() if len(a) > 1]
    if not pairs:
        pytest.skip("no underlying with two separate positions in this archive")
    low, high = pairs[0]
    return low, high


def test_linking_two_cards_by_hand_makes_them_one_and_unlinking_undoes_it(populated):
    """The whole round trip against the real fills: two cards become one filed
    under the lower anchor, carrying the pair so the page can offer the undo,
    and the undo restores both cards."""
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        _, before = _get(base, "/api/state")
        low, high = _two_cards_on_one_underlying(before)
        status, wrote = _post(base, "/api/links", {"anchor": high, "joins": low})
        assert (status, wrote["ok"], wrote["pair"]) == (200, True, [low, high])

        _, after = _get(base, "/api/state")
        anchors = {lc["anchor"]: lc for lc in after["lifecycles"]}
        assert high not in anchors, "the later card is still drawn on its own"
        assert anchors[low]["links"] == [[low, high]]
        assert len(after["lifecycles"]) == len(before["lifecycles"]) - 1

        _post(base, "/api/links", {"anchor": high, "joins": low, "unlink": True})
        _, undone = _get(base, "/api/state")
    assert len(undone["lifecycles"]) == len(before["lifecycles"])


def test_a_link_that_would_hide_a_write_up_is_refused(populated):
    """The merged card files under the lower anchor, so writing on the higher one
    would stop showing anywhere. Refused, and nothing is stored."""
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        _, state = _get(base, "/api/state")
        low, high = _two_cards_on_one_underlying(state)
        _post(base, "/api/journal", {"anchor": high, "lessons": "keep me"})
        status, refused = _post(base, "/api/links", {"anchor": high, "joins": low})
        _, after = _get(base, "/api/state")
    assert (status, refused["ok"]) == (409, False)
    assert high in {lc["anchor"] for lc in after["lifecycles"]}


def test_a_link_across_underlyings_is_refused(populated):
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        _, state = _get(base, "/api/state")
        firsts: dict[str, str] = {}
        for lc in state["lifecycles"]:
            if lc["anchor"]:
                firsts.setdefault(lc["underlying"], lc["anchor"])
        if len(firsts) < 2:
            pytest.skip("one underlying only in this archive")
        a, b = list(firsts.values())[:2]
        status, refused = _post(base, "/api/links", {"anchor": a, "joins": b})
    assert (status, refused["ok"]) == (400, False)


def test_the_page_default_is_the_current_month_and_absent_stays_all_time(populated):
    """`month=current` is what the page sends with no month chosen, and the
    server resolves it to the month today falls in, newest of `month_range`.
    No month at all stays all-time, so the CLI keeps its reading.

    Ablated by dropping the `current` branch: the first assertion fails.
    """
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        _, current = _get(base, "/api/state?month=current")
        _, absent = _get(base, "/api/state")
    assert current["selected_month"] == current["month_range"][0]
    assert absent["selected_month"] is None


def test_a_money_figure_keeps_its_minus():
    """`moneyOf` routed every Money through `chargeOf`, whose `Math.abs` is right
    for a commission and wrong for P&L: a losing month rendered red with no minus,
    because `.signed` draws only the `+`. Found on the live journal's first
    losing month. The unsigned form is `chargeMo`, for commission alone.

    Ablated by restoring the `chargeOf` route: this fails.
    """
    js = _js()
    body = re.search(r"const moneyOf=(.*?);\n", js, re.S)
    assert body, "moneyOf is gone"
    assert "chargeOf" not in body.group(1) and "abs" not in body.group(1)
    assert re.search(r"\.pos\.signed::before\{content:\"\+\"\}", _css())
    assert ".neg.signed::before" not in _css(), "the minus would print twice"


def test_the_odte_refresh_fetches_now_and_answers_a_fresh_reading(populated, monkeypatch):
    """Opening the tab fetches the S&P and VIX and answers the reading as of now.
    The network is stubbed: a prior session for the S&P and a live VIX bar, both
    landing at the moment of the request, which is exactly what makes them fresh.
    """
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415 - local to this test

    from optjournal import serialize  # noqa: PLC0415 - local to this test
    from optjournal.marketdata import Bar  # noqa: PLC0415 - local to this test

    now = datetime.now(UTC)
    week = [int((now - timedelta(days=d)).timestamp()) for d in range(7, 0, -1)]

    def fake(symbol, **_):
        level = 7700.0 if symbol == "^GSPC" else 15.0
        return [Bar(ts=ts, open=level, high=level, low=level, close=level + i,
                    volume=0) for i, ts in enumerate(week)]

    monkeypatch.setattr(serialize, "fetch_bars", fake)
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        status, reply = _post(base, "/api/odte/refresh", {})
    assert (status, reply["ok"], reply["error"]) == (200, True, None)
    assert reply["context"]["fresh"] is True, reply["context"]["stale_reason"]


def test_a_failed_odte_refresh_says_so_and_the_reading_is_not_fresh(populated, monkeypatch):
    """Offline, as on the Saturday that shipped Thursday's close: the reply names
    the failure, and a reading older than the settle is not called fresh."""
    from optjournal import serialize  # noqa: PLC0415 - local to this test
    from optjournal.marketdata import Bar, BarFetchError  # noqa: PLC0415

    def offline(symbol, **_):
        raise BarFetchError(f"{symbol} 1d: URLError: nodename nor servname")

    conn = connect(populated)
    for sym, level in (("^GSPC", 7704.13), ("^VIX", 15.11)):
        serialize.upsert_bars(conn, conid=sym, symbol=sym, bar_size="1d",
                              source="yahoo",
                              bars=[Bar(ts=1790000000, open=level, high=level,
                                        low=level, close=level, volume=0)])
    conn.execute("UPDATE price_bars SET fetched_at = '2026-01-01T00:00:00+00:00'")
    conn.commit()
    conn.close()
    monkeypatch.setattr(serialize, "fetch_bars", offline)
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        status, reply = _post(base, "/api/odte/refresh", {})
    assert (status, reply["ok"]) == (200, False)
    assert "URLError" in reply["error"]
    assert reply["context"]["fresh"] is False


def test_a_price_alert_round_trips_and_an_empty_box_clears_it(populated):
    """Set above and below, read them back, then clear one side only: key-present
    semantics, the same as the earnings date."""
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        _post(base, "/api/watchlist",
              {"symbol": "SPY", "alert_above": "800", "alert_below": "650.5"})
        _, state = _get(base, "/api/state")
        spy = next(w for w in state["watchlist"] if w["symbol"] == "SPY")
        assert (spy["alert_above"], spy["alert_below"]) == (800.0, 650.5)
        _post(base, "/api/watchlist", {"symbol": "SPY", "alert_above": ""})
        _, state = _get(base, "/api/state")
    spy = next(w for w in state["watchlist"] if w["symbol"] == "SPY")
    assert (spy["alert_above"], spy["alert_below"]) == (None, 650.5)


def test_the_row_histogram_is_five_sessions_ending_at_the_daily_reading(populated):
    """Oldest first, so the newest bar is the one the Daily column prints."""
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        _post(base, "/api/watchlist", {"symbol": "SPY"})
        _, state = _get(base, "/api/state")
    for w in state["watchlist"]:
        assert len(w["bx_recent"]) == 5
        assert w["bx_recent"][-1] == w["bx_daily"]


def test_the_row_filters_and_the_bell_read_the_shared_helpers():
    """The price tier and the alert verdict are `watch.js`'s, node-tested; the page
    must not grow its own comparison beside them, and both read the price the row
    shows."""
    rows = _fn("watchRows").replace(" ", "")
    assert "priceTier(shownPrice(w,qs[w.symbol]).price)" in rows
    table = _fn("watchTable").replace(" ", "")
    assert "alertState(px.price,w.alert_above,w.alert_below)" in table


def test_adding_a_symbol_fetches_its_history_at_once(populated, monkeypatch):
    """A new watch arrives with its figures, not dashes until the next job: the
    add fetches the daily history under the key the job would use."""
    from optjournal import bars  # noqa: PLC0415 - local to this test
    from optjournal.marketdata import Bar  # noqa: PLC0415 - local to this test

    asked: list[tuple[str, str]] = []

    def fake(symbol, *, bar_size, start, end, **_):
        asked.append((symbol, bar_size))
        return [Bar(ts=end - 86400 * i, open=10.0, high=10.0, low=10.0,
                    close=10.0 + i % 3, volume=1) for i in range(200, 0, -1)]

    monkeypatch.setattr(bars, "fetch_bars", fake)
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        status, reply = _post(base, "/api/watchlist", {"symbol": "zzzq"})
        _, state = _get(base, "/api/state")
    assert (status, reply["fetch_error"]) == (200, None)
    assert asked == [("ZZZQ", "1d")]
    row = next(w for w in state["watchlist"] if w["symbol"] == "ZZZQ")
    assert row["closes"] > 100 and row["bx_daily"] is not None


def test_saving_a_field_does_not_refetch(populated, monkeypatch):
    """Only a bare add is a new watch; saving an alert must not spend a request."""
    from optjournal import bars  # noqa: PLC0415 - local to this test
    calls: list[str] = []
    monkeypatch.setattr(bars, "fetch_bars", lambda s, **k: calls.append(s) or [])
    with web.serve_ephemeral(db_path=populated, archive_dir=RAW_DIR) as base:
        _post(base, "/api/watchlist", {"symbol": "SPY", "alert_above": "900"})
    assert calls == []
