# optjournal

An options trading journal backed by IBKR's Flex Web Service. Fetches
activity statements, archives them, folds them into SQLite, and serves an
IAG-style local dashboard with cost, history, annual and 0DTE views.

Personal local tool: single user, loopback only, data lives beside the code.

## Quickstart

```bash
# one-time: store the Flex token in the OS keyring
security add-generic-password -s ibkr-flex-token -a "$USER" -w '<token>'

uv run optjournal sync 1591754        # fetch + ingest + report what is new
uv run optjournal serve --query-id 1591754   # UI on http://127.0.0.1:8765

uv run optjournal demo                # synthetic data in demo/ (never raw/)
uv run optjournal serve --demo        # browse it
```

`optjournal --help` lists the rest: `fetch`, `ingest`, `orders`,
`positions`, `history`, `costs`, `statements`, `prune`, `sweep`. Every
reporting command takes `--json`.

## Architecture

```
flex.py ──▶ archive (raw/*.xml) ──▶ ingest.py ──▶ SQLite (db.py)
                                                      │
                       ┌──────────────┬───────────────┤
                       ▼              ▼               ▼
                  history.py      stats.py       analysis.py ◀── raw XML
                  (episodes)   (periods, scopes)  (cost report)
                       └──────────────┼───────────────┘
                                      ▼
                     serialize.py (JSON payload contract; wraps analysis's
                                   per-currency ledgers into Money)
                     render.py    (terminal reports)
                          ▲
                     money.py (Money: held by every layer above, depends on none)
                                      │
                            ┌─────────┴─────────┐
                            ▼                   ▼
                         cli.py            web.py + page.html
```

| module | owns |
|---|---|
| `config.py` | filesystem defaults (`raw/`, `journal.db`, `demo/`) |
| `flex.py` | IBKR Flex fetch: token, retries, lockout budget, cooldown |
| `fills.py` | `NormalisedFill`: one executed fill in broker-neutral terms. A leaf, like `money.py` -- the seam between a broker's statement and the database |
| `sources.py` | `StatementSource` Protocol and the `SOURCES` registry: reads a broker's statement into `NormalisedFill`s. `IbkrSource` is the only implementation today and the one place that knows py_ibkr's attribute names |
| `archive.py` | statement store: content-hash dedupe, prune |
| `ingest.py` | fills → SQLite, idempotent upserts (stores every asset category; scoping is query-time). Trades arrive via `sources`, so the writer is broker-agnostic |
| `db.py` | connection, schema migration, `open_journal()` |
| `locks.py` | cross-process `flock`, because the cron and the server are two processes and a `threading.Lock` only serialises two browser tabs. A leaf |
| `history.py` | fills → round-trip episodes (status, 0DTE, holding period) |
| `money.py` | `Money`: an amount, the currency it was charged in, and the base translation. A leaf — imports nothing, so any layer can hold one. See [The Money model](#the-money-model) |
| `stats.py` | period stats (month/year/all-time), `TradeScope` filters, cohorts. **Never reads `blackscholes.py`** — see [Modelled numbers](#modelled-numbers) |
| `marketdata.py` | price-bar fetch and parse for one contract over one window. A leaf: no DB, no journal shapes |
| `vol.py` | realised volatility from closes, and the move it implies. A leaf, and deliberately NOT `blackscholes` — see [Modelled numbers](#modelled-numbers) |
| `events.py` | economic calendar: fetch, parse and store this week's releases. A leaf. One feed, no Protocol — see [Adding a calendar feed](#a-new-calendar-feed) |
| `bars.py` | the journal-shaped half of price bars — which contract over which window (from episodes), the idempotent write, the series a chart reads, and the expected-move band |
| `blackscholes.py` | option pricing and the implied vol backed out of a market price. A leaf: pure float maths, `math.erf` for the normal CDF, so no numpy or scipy |
| `analysis.py` | cost/friction report from the raw statement (whole account); a leaf — imports nothing internal |
| `strategies.py` | orders folded into the strategies they were placed as (a strangle sold as two same-second orders is one group), then linked into position lifecycles via episode trade ids |
| `serialize.py` | the JSON payload the page renders and `--json` emits; wraps `analysis`'s per-currency ledgers into `Money` |
| `render.py` | human-readable terminal reports. Bound to `serialize`'s shapes by `tests/test_render.py`: it once read flat money keys the `Money` conversion had removed, and `orders`/`history` died on `float(dict)` behind a green suite |
| `cli.py` | argparse wiring only: every command opens the database via `db.open_journal` and emits through `_emit(data, text, json)`, so `--json` comes for free |
| `web.py` | loopback HTTP server; `ServeConfig` injected per server |
| `page.html` | the entire frontend: no build step, no external resources |
| `static/replay.js` | the replay chart's arithmetic as pure functions over plain data — no DOM, no globals — so `node --test` can unit-test the scales and the scrub. A function belongs here if it takes data and returns data; the moment it touches `document` it belongs in the page |
| `browser.py` | headless browser discovery and the DOM dump, in three views: raw, markup (scripts stripped), text |
| `sweep.py` | the page matrix and its checks; each a pure function of a rendered page |
| `mutate.py` | mutation testing: known defects, and which tests notice each. Answers "what would a real bug cost" rather than "what is covered" — see [Measuring the suite](#measuring-the-suite) |
| `demo.py` | deterministic synthetic statement; refuses to touch real data |
| `sections.py`, `compat.py` | shims over py-ibkr's partial statement model |

### Modelled numbers

Every figure the accounting layers report is broker-stated: a fill price, a
commission IBKR billed, a mark from a position snapshot. `blackscholes.py` breaks
that rule on purpose, and is quarantined for it.

Realised P&L is the one worth naming explicitly, because it is stated rather than
derived and that is a choice. IBKR's `fifoPnlRealized` is already net of both
legs' commission (verified arithmetically — see `history.py`'s docstring), and
every headline the journal shows depends on it: win rate, profit factor,
expectancy, the monthly and annual tables, cohorts, strategy groups.

**Why it stays stated.** A test reconstructs the figure from fills alone and
matches IBKR to the cent, so computing it is possible. It is still the wrong
default, for a reason specific to this account: the SIVE sale carries `notes="SL"`,
IBKR's SPECIFIC-LOT marker. Which lots were sold lives in lot-level detail
(`origTradeID`, `origTradePrice`, `holdingPeriodDateTime`) that the Flex query does
not request, so no computation over execution-level fills can reproduce it — the
mapping is the missing input, not the arithmetic. FIFO agreed on SIVE only because
that sale was a full liquidation, where every lot is consumed and FIFO, LIFO and
specific-lot give the same basis. On a partial close it would be wrong, and wrong
in the direction that overstates the gain.

So the broker's figure is preferred wherever a broker supplies one: it already
reflects lot selection and a computed figure never can. The oracle **skips rather
than passes** while every close is a full liquidation, so a green run cannot be
mistaken for "specific-lot handled", and a second test asserts the lot fields are
empty — the day one arrives, the suite says the assumption changed. PLAN.md task 9
has the design for the fallback a broker that reports nothing would need.

Its output reaches the replay panel and nowhere else, through `bars.py`: the
expected-move band, the per-bar P&L on the scorecard, and the effective-delta
series — each labelled as modelled, on a panel whose caption says so. `stats.py`,
`analysis.py` and `serialize.py` never import it, so no headline number, no
calendar day and no annual row can be traced back to a model. The journal's
credibility rests on that separation: "nothing counts until the position is
flat" is worth little if a modelled figure can reach the same card.

The vol it solves against is the contract's own — its daily closes, plus **your
own fills**, which are option prices the market really charged. Fills matter
because a price source's history can begin after a trade did: this journal's TSLA
270P was sold on 2026-07-24 and the source's first bar for it is 2026-07-27, so
without the fill the band, the delta and the P&L were all absent across the entry
session. The fill-derived vol came out at 49.3% against 50.0% for the next daily
close, so it agrees with the source rather than distorting it.

Two assumptions live in `blackscholes.py` as named constants rather than
literals, so they are auditable: `RISK_FREE` (0.04) and `DIVIDEND_YIELD` (0.0,
correct for every underlying this journal has traded options on, and wrong the
day it holds one that pays).

### Clocks

Every journal timestamp is **US Eastern**, settled from the data rather than
assumed (`bars.epoch_et` carries the evidence: Stockholm fills land 03:19-10:57
and a Korean fill at 20:03, both inside those exchanges' sessions in ET and
outside them in UTC). The chart labels the same zone, so fills and bars share one
timeline without conversion.

Bars are stored as epoch UTC. Two daily series from the same source are joined on
the ET trading **day**, not the timestamp, because the source does not stamp them
alike: an option's daily bar arrives at 04:00Z (midnight ET) while its
underlying's arrives at 13:30Z (the session open).

### Perishable data

Bar retention is **asymmetric**, and the collection schedule follows from it
rather than from convenience. Measured pre-market on 2026-08-06: every option
contract in the book returned **zero** hourly bars, while its underlying still
returned five days of them. An option's intraday series exists only while its
session is running, so it cannot be backfilled at any price — miss the session
and those bars are gone.

That splits collection in two, and `bars_manifest` marks the difference with
`BarRequest.perishable`:

| | what | when | cron |
|---|---|---|---|
| Perishable | intraday bars of an **open** option whose replay is drawn hourly | only during its own session | `optjournal-bars-live`, hourly at :05 past, 10:05–16:05 **ET**, weekdays |
| Re-fetchable | daily option closes, the whole underlying series | any time | `optjournal-bars-daily`, 12:30 Dublin, Tue–Sat |
| Audit | did yesterday's perishable bars actually land? | after the daily run | `optjournal-bars-audit`, 13:00 Dublin, Tue–Sat |

Both live in `cron/optjournal_bars.py`. `--live` restricts a run to the
perishable set: running the full manifest seven times a session would re-fetch
three years of settled daily history to collect a handful of new hourly rows.

> **Deployment.** MeshClaw requires cron scripts under `~/.meshclaw/crons/`,
> which is not version controlled, so the implementations live here and each
> deployed file is a **loader shim** that resolves one by path at run time. That
> is what makes editing `cron/*.py` take effect: the shim carries an `IMPL` path,
> a `_load()`, and one delegate per registered entry point, and nothing else.
>
> `optjournal_bars.py` was a byte-for-byte copy for a while, which is the failure
> this pattern exists to prevent — an edit is reviewed, committed, and simply
> never runs, silently, and the copy stays equal only until the next commit
> touches that file. `tests/test_cron.py` now asserts both deployed files are
> shims and that every registered entry point resolves to real code, so the state
> cannot come back unnoticed. It matters more here than anywhere else in this
> project: a perishable session missed is a session no later run can recover.
>
> Each shim is deliberately self-contained, repeating ~15 lines rather than
> importing a shared helper — that helper would have to live in the same
> unversioned directory, which is the problem being solved. A shim whose only job
> is to have no dependencies may not acquire one.
The LEAP is deliberately excluded from hourly collection — the gate is the
chart's own granularity rule, so an option is collected hourly exactly when its
replay is *drawn* hourly.

The intraday series is **cumulative within a session** — a 13:00 poll returns
every completed bar since the open — which is what makes a lost poll harmless
and lets the cron treat a fetch failure as a quiet retry rather than an alert.

That tolerance has one hole, and `bars --audit` is the only thing that sees it: a
session where *every* poll failed is gone and says nothing about it. The audit
asks one question a day, about the one thing that cannot be recovered, and stays
silent otherwise. Three properties make it trustworthy rather than noisy:

- **Eligibility is the live manifest itself**, not a second rule. An audit with
  its own idea of what should have been collected drifts from the collector and
  then reports on a book neither of them holds.
- **The underlying's own hourly series is the holiday oracle.** No date list
  anywhere: US markets shut around nine days a year, a hardcoded calendar would
  need maintaining forever, and the underlying's series — retained for days, and
  re-fetched by the daily run — already answers whether the session happened. No
  bars for anyone means the market was shut; bars for the underlying and none for
  an option means collection failed.
- **A contract opened after the audited session is excluded**, so opening a
  position never triggers a report.

It runs *after* `optjournal-bars-daily`, not before, precisely because that oracle
depends on the daily run having topped the underlying up. Run first, it would read
a stale series, conclude the market was shut, and pass a genuinely lost session.
Exit codes are the whole interface: `0` covered, `1` bars missing (report), `3`
nothing to check.

`marketdata.parse_chart` drops bars off the series' own grid. The source appends
a synthetic bar for the moment you asked, stamped at that moment: a 13:17 request
returns 09:00, 10:00, 11:00, 12:00 and then **12:35**. That stamp is unique per
request, so it upserts over nothing and every poll deposits a fresh phantom bar —
six such rows were already stored from two backfills during one session, and
polling hourly would have added seven a day per contract. Daily bars are
deliberately *not* filtered: a daily bar for a session in progress is
legitimately incomplete and the chart draws it as "where it is now".

Layering rules (import direction only goes down this list):

1. Entry points (`cli`, `web`) construct dependencies — paths from
   `config`, connections via `db.open_journal`, a shared `HistoryReport` —
   and pass them in. Nothing below the entry points reads globals.
2. Domain modules (`history`, `stats`, `analysis`) take a connection or a
   statement and return dataclasses. They know nothing about JSON, HTML,
   or argparse.
3. Presentation (`serialize`, `render`) consumes domain objects and never
   opens its own connections (the two `*_data(conn)` readers are the
   deliberate exception: they wrap single SELECTs over views).
4. A domain module produces raw material; the layer that already holds
   several of them composes. `analysis.py` accumulates commission, taxes
   and fees keyed by the currency they were *billed* in, and deliberately
   does not decide whether one currency can speak for a total — that
   judgement is `Money.gated()`, and `serialize.py` applies it when it
   builds the payload. `analysis` could now import `money.py` directly
   (it is a leaf, so it costs no dependency direction), and the split is
   kept anyway: a report that measures cost has nothing to say about how
   a reader's display currency should be chosen.
5. A quantity and its unit travel together. `Money` carries an amount,
   the currency it was charged in and the base translation as one frozen
   value, because the three-field spelling it replaced (`x_base`,
   `x_native`, `x_native_ccy`) let them drift: `options_friction` took
   its amount from one figure and its currency label from another, and a
   figure could be left half-assigned with nothing to complain. Add a new
   gated figure by returning a `Money`, never by adding a field triple.

Four invariants worth knowing before changing the UI:

* **A charge is shown in the currency it was charged in, when the reader
  is already looking at that currency.** IBKR bills commission per trade
  in the instrument's currency (and, on FX conversions, in the account
  base) and debits it there — no euros move for a dollar commission. So
  `base` figures are an accounting translation, converted per row at
  IBKR's own rate for that row's date, and restating a sum of them into a
  display currency sends each charge on a round trip through two different
  rates. Where one currency accounts for a whole figure the payload also
  carries `native` and its `ccy`, and the page prefers that: the card says
  "as charged" rather than "restated". Where a figure spans currencies —
  this account's stock trades span four — `native` is `null`, because an
  exact-looking number covering part of a total is worse than an honest
  approximation of all of it. See **The Money model** below for the shape
  and where each figure gets one.

* **A tab's numbers change only in response to a control that tab
  displays.** The Trade Types control drives Dashboard/Calendar/Trades
  (which render the filter bar) and nothing else. "0DTE" is a fill-level
  scope within options; "Equities" switches the asset category those three
  tabs run over. Positions, Costs, Annual and 0DTE stay pinned to options.
* **Options P&L counts fully closed round trips only, attributed to the
  close date.** A partial close (sold 3, bought back 1) contributes
  nothing until the position is flat, and premium collected on an open
  short is a liability, not profit — it is shown separately as "open
  premium". Other asset categories keep IBKR's per-fill realisation.
  `Gain % of Net Liq` divides that P&L by the NAV from the statement's
  Equity Summary section (enable it on the Flex query template; the demo
  carries synthetic NAV rows).
* **The payload contract lives in the page, and the suite derives its
  guards from it.** `page.html` opens with `@typedef` blocks declaring
  every shape the page reads and a `@payload`/`@local` table saying which
  binding holds which shape. `tests/test_web.py` parses those blocks and
  enforces the chain in every direction: reads must resolve against the
  typedefs, the typedefs must match a real payload both ways (a required
  key the API stops sending fails, and a key it sends undeclared fails),
  and the binding table may be neither incomplete nor stale. A typo'd key
  fails a test instead of rendering a blank cell.

## Measuring the suite

`optjournal mutate` injects a known defect and reports which tests notice. It
answers a different question from coverage: not "did this line run" but "would a
wrong line be caught, and by how many tests".

Two results are worth acting on. A real defect caught by **zero** tests is an
unguarded invariant — that is how a `_flat` epsilon wide enough to book a
0.4-share residual as a closed round trip was found, having passed 579 tests, and
how `_snapshot_leg` silently taking `abs()` of a short position was found. A
defect caught by **fifteen** tests suggests fourteen are coupled to something they
are not about. Measured over all 31 mutants: 31 caught, median 1, maximum 19.

The two high counts say different things, which is the point of reading the names
rather than the number. At 8 is the Money currency gate — a rule that genuinely
spans money, analysis, strategies and web. At 19 is `_num`, the Decimal-to-float
coercion every payload flows through, and its 19 are not 19 invariants: they are
**11 distinct test functions**, of which one parametrised over the archived
statements contributes 9 on its own, and six more are `costs_data` assertions that
each happen to read a number. A high count is evidence of a well-shared rule only
if the tests that failed are about that rule; otherwise it marks a chokepoint.

**Equivalent mutants are not findings.** Some changes have no observable effect,
so "nothing caught it" says nothing about the suite. The tool reports; judging
whether a defect is real is the reader's job.

The zeroes keep paying for the tool. A later round added ten mutants, one per
finding from a code audit, and **eight were caught by nothing** while 602 tests
passed: a KRW fee claiming to be a EUR amount, broker interest received booked as
a cost, withholding whose effective rate came out negative, `AssetClass.STOCK`
reaching the payload where the page reads `STK`, a table that stopped sizing
columns to their headers, a closed contract taking its side from the *closing*
fill (so every short you sold reads as a long you bought), and the Flex
request-budget cooldown, which had no test at all. They share a shape: each one
produces a well-formed report with a wrong number in it, which is why reading the
code did not find them and passing tests did not either.

One of the ten was not a missing test but a live defect — `analysis` accumulated
`int(abs(quantity))` per fill, so any lot under one whole unit contributed
nothing and a thousand half-share buys summed to zero. An eleventh candidate was
neither: it was unreachable code, and the right response was to delete it. So
"caught by nothing" has three answers — add a test, fix the code, delete the code
— and deciding which is the reader's job too.

It also settled a question that intuition kept getting wrong. `test_web.py` is the
largest test file and much of it greps the page's JavaScript rather than executing
it, which reads like a smell. Ablation says otherwise: typo a payload key the page
reads and the browser renders an **em dash** where a real mark price should be —
no `undefined`, no `NaN`, nothing an executed assertion can see, and
`test_rendered.py` passes. Only the source-text guard catches it, and that is the
bug class the four historical examples above all belong to. The greps are the
defence, not the smell.

The harness lives in the repo rather than in a scratch directory because getting
it right took three attempts, and each failure looked like an alarming coverage
result rather than a broken tool:

* `uv run pytest` inside a clone resolves to the **original** project.
* `cp -R` copies `.venv`, whose editable-install `.pth` hardcodes the original
  repo's `src`, so even the clone's own interpreter imports the original.
* On macOS `/tmp` is a symlink to `/private/tmp`, so a guard written to catch the
  first two by string prefix rejects correct clones.

Each produced "caught by nothing" for a defect that was in fact well covered, and
the first conclusion drawn from it — that the Money gate had one test — was wrong;
the real answer is eight. So `mutate.py` proves the mutation is the code pytest
imported before it will report a number, and refuses to report one otherwise. A
harness that can silently measure the wrong tree is worse than none, because its
output looks like evidence.

## The Money model

Every cash figure has two readings, and conflating them was the largest
source of wrong numbers in this journal's history — a `$22.44` overstatement
on a single option premium, found only after the type made the two readings
impossible to separate.

* **base** — the accounting translation. Each contributing row converted at
  IBKR's own rate for *its own date*, then summed. Always available, always
  addable across currencies, and never exactly what left the account.
* **native** — the amount as charged, in the currency it was charged in.
  Exact, but unaddable: USD, SEK and KRW commission cannot share a number.
  Offered only when one currency accounts for the whole figure.

`Money` (in `money.py`, a leaf so any layer may hold one) carries both as one
frozen value. It replaced three parallel fields per figure — `x_base`,
`x_native`, `x_native_ccy` — plus a five-line ledger accumulation at each
producer and a two-call unpack at each consumer. The spelling was not merely
verbose; it let the halves drift. `options_friction` took its amount from
`abs(commissions_native)` and its currency label from `commissions_native_ccy`,
a field on a *different figure*, and a figure could be left half-assigned with
nothing to complain. Both are now unrepresentable: the constructor refuses a
half-set figure, and `abs()` carries the currency along.

### On the wire

Real values from this account, at their actual payload paths:

```json
costs[].totals.journal_commission  { "base": 6.071888, "native": 6.965211, "ccy": "USD" }
costs[].totals.other_commission    { "base": 3.338990, "native": null,     "ccy": null   }
costs[].totals.friction_base       10.736560
```

`journal_commission` answers because this journal's option trades are all USD.
`other_commission` withholds because the account's stock trades span four
currencies, so no single one can speak for the total. `friction_base` is a bare
float, because it contains the AutoFX markup estimate.

`native` and `ccy` are always both present or both `null` — never one of the
two. **The shape itself carries meaning**: a nested object says "an as-charged
figure could exist here"; a flat `_base` float says it cannot. The sweep
asserts that directly, in both directions.

### Six constructors, six provenances

Each names where the figure came from, rather than being six ways to do one
thing. Choosing one is the decision; the gate is not re-litigated per call
site.

| constructor | for |
|---|---|
| `Money(base, native, ccy)` | both readings already known |
| `Money.restated(base)` | no native can exist *even in principle* |
| `Money.gated(base, by_ccy)` | a base plus a per-currency ledger to judge |
| `Money.charged(rows)` | `(base, native, ccy)` rows: accumulate and gate in one pass |
| `Money.at_rate(native, rate, ccy)` | one row's amount, base derived from that row's own rate |
| `Money.from_rows(rows, field)` | fill rows following this project's `field` / `field_base` / `currency` shape |

`restated` is deliberately distinct from a `gated` figure that happened to
withhold: `friction` includes the estimated AutoFX markup, which IBKR never
billed as a line item in any currency, so it is `restated` **by nature**. A
test asserts it never gains a native, so nobody later "fixes" the gap by
inventing precision.

`at_rate` replaced `natCash(v, rate)` in the page, which multiplied to base and
then applied the display rate — two hops, so a USD value shown in USD had
round-tripped through EUR at two different rates. That was lossless on current
data only by coincidence (the display rate is the positions snapshot's own rate
inverted, so the hops cancel), and would drift the moment positions span report
dates.

### Every level aggregates the leaves, never the level below

`leg → order → strategy group → position lifecycle` each derive their figures
from the **same leaf fill rows** via `Money.from_rows`. The base is identical
either way, since sums are associative — but the gate is not:

> An order spanning currencies has `native: null`. A group summing that order
> cannot distinguish a native *withheld for being mixed* from one that *never
> existed*, so it would gate as though that order contributed nothing.

Asked at each level against the union of those legs' currencies, it answers
correctly everywhere. `trade_legs` carries a `currency`; `trade_orders`
deliberately carries none, because an order can span them — that asymmetry is
why the leaf is the only honest source.

Which decides how each row is shaped, and the rule reads oddly until you see
the reason:

* **Derived rows** (order, strategy group, lifecycle) **replace** their six
  flat keys with three `Money` objects. Nothing aggregates from them.
* **Leaf rows** (`trade_legs`, position snapshots) **keep** the raw triple and
  gain a nested `money` key beside it, because every level above re-aggregates
  exactly those keys.

Writing a leaf's Money over its flat native breaks the aggregation one level
up — it happened twice during the conversion, once caught by inspection and
once by the suite, which is what turned this from a preference into a rule.

### What stays a flat float, and why

Not everything can be `Money`. These have no native in the data, and no amount
of work creates one:

| figure | why |
|---|---|
| `net_liq_base` | IBKR's section is `EquitySummaryByReportDate`**`InBase`** — base at source |
| `autofx_spread_base` | basis points on a converted notional; never billed |
| `friction_base`, `account_friction_base` | contain the AutoFX estimate above |
| `notional_base` | a cross-pair notional, so no single currency |
| `per_base` | an FX **rate**, not an amount |
| `win_rate`, `gain_pct_of_net_liq` | unitless |

Everything else is `Money`. When adding a figure, the question is not "should
this be Money" but "which constructor names where it came from".

`journal_taxes` was the last holdout, and its obstacle was shaped like a
warning: the journal-scoped tax ledger *did* exist, but only inlined inside
`journal_friction_native_by_ccy`. Aggregated, and unreachable — so journal taxes
had a base total and no path to the as-charged figure. Extracting it as
`journal_taxes_native_by_ccy` both exposed the figure and let friction compose
two named ledgers instead of restating "commission plus taxes" a second time,
differently.

Neither journal holds a single taxed trade (`journal_taxes_base` is `0.0` on
both, and no row in the database carries non-zero `taxes`), so real data cannot
tell a working gate from one that never runs. Both branches are therefore
exercised against hand-built statements in `test_analysis.py` and
`test_web.py` — which is what `analysis.py` being a leaf buys. The demo is
deliberately **not** given a fake tax: IBKR levies none on US options, and a
fixture that emits what the broker would not is how the `underlyingSymbol` bug
stayed hidden.

### One entry point in the page

`chargeOf(native, ccy, base)` is the rule; `moneyOf(mo)` applies it to a
Money-shaped payload key. There is no second path — `legProceedsOf` and
`natCash` were both absorbed, and a test asserts they cannot come back.
Figures that share a sentence must share a basis, so the rule is applied per
*block*, not per figure: "as charged · $6.97" beside a €6.07 pill is a
contradiction, not a rounding difference.

## Adding functionality

**A new UI tab**: serializer in `serialize.py` → emit it in
`web.build_state` → declare its shape in `page.html`'s `@typedef` blocks
and its binding in the `@payload` table (same file, same diff) → add the
shape's extractor to `tests/test_web.py::_shape_samples` → view function
in `page.html` + entry in `TABS` + register in the `views` dispatch → add the
key to `sweep.TABS` (a test holds it to `page.html`'s own list, because a tab
missing there is never swept and the sweep still reports a pass) →
tests asserting its figures reconcile with an existing independent number
(see the Annual total-row tests).

**A new payload key on an existing shape**: emit it in the serializer and
add one `@property` line to the shape's typedef — the drift test holds the
two together from both sides.

**A new cash figure**: return a `Money` from the constructor that names its
provenance (see [the table](#six-constructors-six-provenances)), declare it
`{Money}` in the typedef, and read it in the page with `moneyOf`. Never add a
`_base`/`_native`/`_native_ccy` triple: that spelling is what let an amount and
its currency drift apart. If the figure genuinely cannot have an as-charged
form, `Money.restated` says so explicitly — and a flat `_base` float is
reserved for the cases in [what stays a flat
float](#what-stays-a-flat-float-and-why).

**A new sweep check**: a function taking a `Page` and returning `ok()`,
`bad(reason)` or `skip(why)`, added to `sweep.CHECKS` — plus a pair of
fragments in `tests/test_sweep.py::CASES`, one satisfying it and one violating
it. The violation must FAIL, and a test refuses any check that joins `CHECKS`
without that pair. Read `p.markup` for structure, `p.text` for prose; an AST
test forbids reading `p.dom`, which contains the page's own JS.

**A new trade-type filter**: build a `TradeScope` (fill membership, not a
predicate — see `odte_scope` for why) and register it in
`stats.SCOPE_BUILDERS`. The button in `page.html`'s filter bar and the
`?type=` parameter use the same key. Unknown keys fail open to the whole
journal.

**A new CLI command**: `cmd_*` function + subparser in `cli.py`, opening
the database via `db.open_journal`. Emit through `_emit(data, text, json)`
so `--json` comes for free. Reach for `_open_db` only if the connection has to
outlive one block (ingest and sync read rows back after writing); it hands back a
handle the caller must close, and three commands that used it for a single call
each simply never closed one.

**A new terminal report**: a `render_*` in `render.py` reading the serializer's
shapes, plus a case in `tests/test_render.py` that builds its payload with the
REAL serializer over the real archive. A hand-written dict would have passed
throughout the window when `orders` and `history` were both crashing.

<a id="a-new-calendar-feed"></a>
**A new calendar feed**: `events.py` is deliberately NOT a Protocol with a
registry, unlike the broker seam. That seam earned its abstraction by having a
second implementation in prospect and a schema whose identity depended on it; a
calendar has one feed, and an abstraction with one implementation is untested by
construction — which this repo learned expensively (see PLAN.md's "a seam is only
as good as the test that uses two of them"). What keeps a second feed possible is
the `(source, event_id)` primary key, not an interface. So: add a second
fetch/parse pair, and extract the Protocol *at that point*, when it can first be
verified by two implementations.

Two things a second feed must respect. `impact` is the FEED's judgement, stored
verbatim and attributed on the page — an unrecognised value raises rather than
being filed as Low, because a silently downgraded event is a calendar that
looks right. And `event_id` is ours: ForexFactory supplies none, so it hashes
`(starts_at, country, title)`, verified distinct across a real week's 99 rows.

**A new broker**: a class satisfying `StatementSource` (a `broker` name, a
`base_currency(path)` and a `statements(path)` yielding `NormalisedFill`s), plus
one entry in `sources.SOURCES`. That is the whole additive part — `ingest` writes
`NormalisedFill`s and never sees a broker's own model, and every identity in the
schema already leads with `broker`.

Then **write the two-broker test before trusting any of it**. The seam looked
finished for a whole commit while four defects hid in it, each unreachable with a
single source: the `broker` argument was written but ignored by the trades writer,
two tables were still keyed on IBKR's own ids, `trade_legs` summed two brokers'
fills into one leg with doubled quantities, and two `MAX(report_date)` queries let
the most recent filer define "current" for everyone — which in `history._held`
reads a still-open position as CLOSED. See
`tests/test_sources.py::two_brokers`: it registers a second source that *reuses
the IBKR reader*, so the data is identical and any difference in the output is the
journal's handling of `broker` and nothing else. Identical trade ids under two
brokers is exactly the collision the composite keys exist for, and a real second
broker makes it possible on day one.

`ingest.py` reads no broker vocabulary of its own — a test over its AST enforces
that, checking it imports neither `flex` nor `sections` and names none of 26 IBKR
fields in code. So the writers really are broker-agnostic, not just described that
way.

One thing is deliberately unfinished, and the halves are now separated. The
**seam** speaks trading terms throughout — `contract_id`, `exec_id`, `order_id`,
`commission`, `realized_pnl` — while the **database and payload** still spell five
things IBKR's way (`conid`, `ib_order_id`, `ib_commission`, `fifo_pnl_*`,
`ib_exec_id`). Those names are *accurate* while IBKR is the only source, so the
remaining rename is the schema catching up rather than a design question.

What makes leaving it that way safe is that the two vocabularies meet in exactly
one file: `ingest.py`, whose SQL names the column while its values read the
attribute, so `fill.contract_id` is written into `conid` on a single line. A test
fails if any other module starts reading a seam attribute, and another rejects a
seam field whose name contains a vendor word at all. Prose is not a guard —
`conid` sat on the seam for four months with the reason written beside it.

It is 690 occurrences, so it waits for a real second broker — see PLAN.md's "task
8" for the measured scope, the two migration traps that are confirmed by test, and
the commit-by-commit order.

**A new cron job**: implementation in `cron/`, a loader shim under
`~/.meshclaw/crons/` that locates it by path (never a copy — see below), and a
case in `tests/test_cron.py`. The cron scripts run under MeshClaw's interpreter,
which has no py_ibkr, so they may import nothing from the `optjournal` package
and must shell out to the CLI instead. Exit codes are mirrored rather than
imported for that reason, and a test holds the copies to `cli.py`'s originals.

## Data safety

* `raw/` is the provenance root; statements are deduplicated by content
  hash and cost IBKR requests (against a lockout budget) to replace.
* A per-query fetch cooldown refuses to re-spend a request for data that
  cannot have changed. `--force` overrides.
* `optjournal demo` refuses to write into the real archive or database,
  and `serve --demo --query-id` is refused outright: one Sync click would
  ingest real trades into the synthetic database.
* `demo.write_demo_bars` refuses a database holding any statement that is not
  a demo one. Every row in `price_bars` is supposed to be something a source
  really served, so a **computed** bar in the real journal would break the
  reproducibility the archive exists to provide — and unlike a fake statement it
  would sit there looking exactly like a fetched one. The check is on the data,
  not on the path, so pointing `--db` at a copy of the real journal is refused
  too. Computed bars are also ranked below every real source
  (`marketdata.SOURCE_RANK`), so a genuine fetch always displaces one and never
  the reverse.
* The server binds loopback only and refuses anything else: no
  authentication, and the UI exposes an entire brokerage account.

### The demo's option bars are computed

The demo charts real NVDA and SPY history, but its option symbols are invented,
so the price source returns 404 for every one: zero option bars against 1,680
underlying ones. The band and the effective delta both solve implied vol from an
option's own daily closes, so both were reaching for a series that can never
exist — the demo drew a price line and nothing that made it a replay.

Each contract is priced from **one observation the statement itself states** — an
opening fill, or a snapshot's mark — by solving the vol that reproduces it at the
real spot for that day, then repricing along the real spot path. Deriving from the
statement rather than assuming a plausible vol is what keeps the bars consistent
with the demo's own P&L; the bar for the anchor's own session carries that price
verbatim. Vol then drifts in slow regimes with a small per-session jitter, both
fixed by the calendar day so a re-run is reproducible.

A contract whose anchor cannot be solved is **skipped, not defaulted** — and that
turned out to be a real signal rather than a nuisance. It found two: `SPY 600C`
and `SPY 640C` were written for a price level SPY never traded at during their
windows, leaving the statement claiming premiums *below intrinsic*, which no
volatility can produce. Nothing had caught it, because a strike enters no P&L
arithmetic: every money assertion passed while those two replays silently carried
no band, no delta and no modelled P&L. `test_demo.py` now pins each contract's
premium against the real close for its own date.

## Development

```bash
uv run pytest -q            # 642 tests; the raw/ statements are fixtures
uv run ruff check src tests cron
uv run optjournal sweep     # every page in a real browser (~2 min)
uv run optjournal mutate    # all 25 known defects (~15 min: a full suite run each)
uv run optjournal mutate --only fee-ccy --only strike-side   # one or a few
```

`mutate` runs the whole suite once per mutant in a fresh clone, so a full survey
is a coffee break rather than a pre-commit step. Use `--only` while working on one
invariant, and the full run when changing what the suite is *for*.

The suite covers four layers: unit tests over domain arithmetic (with the
generator itself under test — see `test_demo.py`), payload-contract guards
binding `page.html` to `build_state`, static checks over the page's JavaScript
(history discipline, hash round-tripping), and one executed render in a real
browser engine (`test_rendered.py`).

Shared scaffolding lives in `tests/conftest.py` — `RAW_DIR`/`STATEMENTS`, a
migrated `conn`, a `populated_db`, `add_statement`, and the `code_only` comment
stripper both JS guards use. What a *trade row* contains stays in the module
asserting it: that is the subject of those tests, not setup for them.

**Every consumer of a payload is bound to its producer by a test**, because the
one that was not shipped broken: `render.py` kept reading the flat money keys the
`Money` conversion had removed, and `optjournal orders` and `optjournal history`
both died on `float(dict)` behind a green suite. The page has its `@typedef`
guards, `--json` has the sweep, the terminal reports have `test_render.py`, and
the crons have `test_cron.py`. A new consumer needs one too.

Three rules the README used to state in prose and nothing enforced now have
tests in `test_layering.py`: the leaf modules import nothing from the package,
only `bars` and `demo` may import `blackscholes` (the modelled-number
quarantine), and the import graph is acyclic — which matters because the cron
loads this package under an interpreter that has no py_ibkr.

`optjournal sweep` goes further than the suite can afford to: it renders **every
page both journals can show** — each tab, the currency toggle, the asset switch,
the calendar drill-downs — and applies every check to every page. It is a
separate command rather than a test because it launches a browser per page, so
it costs a minute or two where the suite costs ten seconds.

Its checks are in the suite even though it is not. Each is a pure function of a
rendered page, so `tests/test_sweep.py` feeds every one a fragment that violates
it and asserts it **fails** — and treats a skip as a failure of the check
itself, because that is the silent mode: a precondition that stopped matching
the markup reads as "not applicable" forever. Three checks were caught that way
on the first run, including two that could never have failed. A green sweep is
worth something only because the checks have been shown to go red.

Both journals are swept because they cover different ground: the real one is the
only source of true rates and mixed currencies, and the demo is the only one
holding closed round trips, spreads, rolls, expiries, a 0DTE trade and a
commission credit. Skips are reported separately from passes, so a check whose
precondition this data never reaches cannot be mistaken for evidence.
