# optjournal

An options trading journal backed by IBKR's Flex Web Service. Fetches
activity statements, archives them, folds them into SQLite, and serves a
local dashboard with cost, history, annual and 0DTE views.

The dashboard is branded **Bitácora** — Spanish for a ship's logbook, and for
the binnacle that housed the compass beside it. `optjournal` stays the name of
the package, the CLI and the database; the page is the only thing the brand
touches.

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
`positions`, `history`, `costs`, `friction`, `statements`, `prune`, `sweep`.
Every reporting command takes `--json`.

Two cost commands, answering two questions. `costs` reads one statement — the
newest archive covers 30 calendar days — and is the only way to see a section no
database column carries. `friction` reads the journal: every ingested fill, over
the account's whole history, narrowable to any set of asset categories.

```
optjournal friction                      the whole account
optjournal friction --assets OPT          options only
optjournal friction --assets OPT CASH     options and the conversions to trade them
optjournal friction --month 2026-08       one month (or a year: 2026)
```

## Architecture

```
flex.py ──▶ archive (raw/*.xml) ──▶ ingest.py ──▶ SQLite (db.py)
                                                      │
                       ┌──────────────┬───────────────┤
                       ▼              ▼               ▼
                  history.py      stats.py       analysis.py ◀── raw XML
                  (episodes)   (periods, scopes)  (cost report)
                       │              │           costs.py ◀── the same costs,
                       │              │           (scoped, whole history)  from SQL
                       └──────────────┼───────────────┘
                                      ▼
                     serialize.py (JSON payload contract; wraps analysis's
                                   per-currency ledgers into Money)
                     render.py    (terminal reports)
                          ▲
                     money.py (Money, Charge: held by every layer above,
                               depends on none)
                     notes.py (IBKR note codes: read by history and analysis,
                               which cannot import each other)
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
| `logs.py` | the rotating application log, beside the journal. A leaf. Exists because macOS rotates NOTHING for a launchd agent's stdout: `newsyslog` only touches files listed in `/etc/newsyslog.conf`, so a supervised `serve` appends to one file forever. Two files answer two questions -- launchd's `serve.out.log` is "did the process start", this one is "what did the jobs do" |
| `locks.py` | cross-process `flock`, because the cron and the server are two processes and a `threading.Lock` only serialises two browser tabs. A leaf |
| `jobs.py` | the job registry, the runner, and the run ledger. The registry being CODE is the point: MeshClaw's registration was an unversioned side channel, and the consequence is measurable — four optjournal crons are registered and none is the calendar refresh, so 143 lines of tested policy have never run on a schedule. Jobs call functions, never the CLI, so a cause arrives as a typed exception instead of an integer. See `SCHEDULER_PLAN.md` |
| `sync.py` | the ONE sync path — fetch, ingest, snapshot — called by `POST /api/sync`, `optjournal sync` and the `sync` job. Its own module because of the import graph, not for tidiness: it briefly lived in `web.py` and `jobs.py` reached it through a deferred import, which `tests/test_layering.py` correctly called a cycle. It raises rather than returning an error dict, because each caller needs a different shape for a cooldown (HTTP body, exit code, ledger status) and flattening them into a string is how a locked keychain became a bare exit 1. Its success reply stays a **dict** on purpose — see [Why the sync reply is not a dataclass](#why-the-sync-reply-is-not-a-dataclass) |
| `history.py` | fills → round-trip episodes (status, 0DTE, holding period) |
| `money.py` | `Money`: an amount, the currency it was charged in, and the base translation. A leaf — imports nothing, so any layer can hold one. See [The Money model](#the-money-model) |
| `notes.py` | IBKR trade note codes (`AFx`, `Ep`, `A`) and the one rule for reading them: whole-token matching, over either the stored `AFx;P` string or py_ibkr's parsed list. A leaf, because its two readers — `history.py` (database) and `analysis.py` (statement) — sit on opposite sides of the graph and cannot import each other |
| `stats.py` | period stats (month/year/all-time), `TradeScope` filters, cohorts. **Never reads `blackscholes.py`** — see [Modelled numbers](#modelled-numbers) |
| `marketdata.py` | price-bar fetch and parse for one contract over one window. A leaf: no DB, no journal shapes |
| `vol.py` | realised volatility from closes, and the move it implies. A leaf, and deliberately NOT `blackscholes` — see [Modelled numbers](#modelled-numbers) |
| `events.py` | economic calendar: fetch, parse and store this week's releases. A leaf. One feed, no Protocol — see [Adding a calendar feed](#a-new-calendar-feed) |
| `clock.py` | the market zone and the four conversions stated against it: `MARKET_TZ`, `epoch_et`, `expiry_epoch`, `et_day`. A leaf. Which zone the journal's stamps are in is a property of the market, settled from real fills in three time zones (see `epoch_et`), and four modules need it at different depths — so it lives where every layer can hold it rather than inside the module that happened to discover it |
| `bars.py` | the journal-shaped half of price bars — which contract over which window (from episodes), the idempotent write, and the series a chart reads. Storage only: it imports no price model, which is what `tests/test_layering.py` now enforces by naming `replay.py` rather than this module in the pricing allowlist |
| `replay.py` | the whole replay concern, in two halves. The MODELLED one is the only place anything is priced: implied vol solved from each contract's own closes, the expected-move band, the mark-to-market series and the effective delta, with one solve shared by the band and the marks so they cannot disagree. The ASSEMBLY one turns lifecycles and position rows into the `replays` map the panel reads — strike segments, fill stamps, event cards — and was `web.py`'s until the concern was made whole. `attach()` is the interface; `build_state` calls it once, last. Holds `bars.close_series` and `bars.replay_bars`, one way — see [Modelled numbers](#modelled-numbers) |
| `blackscholes.py` | option pricing and the implied vol backed out of a market price. A leaf: pure float maths, `math.erf` for the normal CDF, so no numpy or scipy |
| `analysis.py` | cost/friction report from the raw statement (whole account); pure statement mathematics — holds leaves (`notes.py`) and nothing that reads a database |
| `costs.py` | the same costs read from SQLite instead: the account's whole life, narrowed to any set of asset categories, with measured cost kept structurally apart from the estimated AutoFX markup. Reconciled against `analysis.py` statement by statement — see `tests/test_costs.py` |
| `campaigns.py` | which episodes were ONE decision, and what that decision earned. A roll continues a position rather than closing it, and a spread's legs are separate contracts, so the scoreboard's unit is the campaign while the money's stays the episode. A leaf holder (`money.py` only), because its two readers — `strategies.py` for the Trades tab cards and `stats.py` for the win rate — cannot import each other, and the rule living in one of them is how the Dashboard came to count a roll as two wins while the tab drew one card |
| `strategies.py` | orders folded into the strategies they were placed as (a strangle sold as two same-second orders is one group), then linked into position lifecycles. The grouping RULE is `campaigns.py`'s; what lives here is naming the shape and aggregating the legs |
| `serialize.py` | the JSON payload the page renders and `--json` emits; wraps `analysis`'s per-currency ledgers into `Money` |
| `render.py` | human-readable terminal reports. Bound to `serialize`'s shapes by `tests/test_render.py`: it once read flat money keys the `Money` conversion had removed, and `orders`/`history` died on `float(dict)` behind a green suite |
| `cli.py` | argparse wiring only: every command opens the database via `db.open_journal` and emits through `_emit(data, text, json)`, so `--json` comes for free |
| `web.py` | loopback HTTP server; `ServeConfig` injected per server |
| `page.html` | the frontend's markup and JavaScript: no build step, nothing off-origin |
| `static/app.css` | every styling rule, and every colour. Extracted from the page's `<style>` block so all of them sit where `tests/test_web.py`'s layout assertions can read them — four real defects once lived in CSS that nothing in the suite had ever looked at. A `<link>` in `<head>` is render-blocking, so there is no unstyled flash; the extra loopback request measures 1.4 ms. Opens with one `[data-theme]` block per [theme](#themes) |
| `static/replay.js` | the replay chart's arithmetic as pure functions over plain data — no DOM, no globals — so `node --test` can unit-test the scales and the scrub. A function belongs here if it takes data and returns data; the moment it touches `document` it belongs in the page |
| `static/mark.svg` | the Bitácora compass rose as a standalone favicon. Carries its own colours: a favicon renders outside the page, where `page.html`'s custom properties do not reach. `web.STATIC_TYPES` is what lets it be served as an image rather than a script |
| `browser.py` | headless browser discovery and the DOM dump, in three views: raw, markup (scripts stripped), text |
| `sweep.py` | the page matrix and its checks; each a pure function of a rendered page |
| `mutate.py` | mutation testing: known defects, and which tests notice each. Answers "what would a real bug cost" rather than "what is covered" — see [Measuring the suite](#measuring-the-suite) |
| `demo.py` | deterministic synthetic statement; refuses to touch real data |
| `sections.py`, `compat.py` | shims over py-ibkr's partial statement model |

### Why the sync reply is not a dataclass

An architecture review proposed replacing `sync_journal`'s 18-key return dict with
a frozen `SyncResult`, for the win "a key mismatch becomes a type error". It was
investigated and **rejected**, because in this project that sentence is not true.

There is no type checker here — `pyproject.toml` runs ruff and pytest, and ruff
does not resolve attributes. Measured directly: ruff passes a file that reads
`r.new_tradez` off a dataclass just as happily as `d["new_tradez"]` off a dict. At
runtime both raise, one `AttributeError` and one `KeyError`, so a *read* of a
missing name already fails loudly either way.

The drift the review cited would not have been caught either. The recorded defect
is that `new_trades` held a COUNT in one producer and the row LIST in the other —
"one name and two types". Python does not enforce annotations, so
`SyncResult(new_trades=[{...}])` is accepted in silence and the page renders the
list into its sentence, exactly as the dict did.

Two things a dataclass *would* buy, and neither applies. It refuses an unknown
attribute on WRITE (`TypeError` under `slots=True`) where a dict accepts a new key
— but nothing assigns to this reply after it is returned. And it makes the field
set visible in one place — which `sync.py`'s single `return` statement already
does.

What it would COST is real: the whole dict is the published shape of
`optjournal sync --json` (`cli._emit` serialises it verbatim) and the body of
`POST /api/sync` (`web._do_sync` returns it, and `page.html` reads `new_trades`,
`new_cash` and `reused_archive` off it, with a `@typedef` block pinning them). So
the review's companion suggestion — narrow to four fields and move the rest to the
callers — would silently break both the CLI's machine output and the page's sync
toast. The dict is not an internal convenience that grew; it is a wire format.

**The real gap was on the other side, and it was found by measuring instead.**
Renaming a key in the CONSUMER — `result["summry"]` in `jobs._sync` — passed the
entire suite. The neighbouring test reads `_sync`'s SOURCE for the functions it
must not call, which is right for a negative obligation and blind to a typo. So
the fix was a test that runs the translation with a stubbed sync path (a real one
spends an IBKR request, which is why it had never been executed under test), plus
the first `jobs.py` mutant, `sync-empty-ok`.

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

Its output reaches the replay panel and nowhere else, through `replay.py`: the
expected-move band, the per-bar P&L on the scorecard, and the effective-delta
series — each labelled as modelled, on a panel whose caption says so. `stats.py`,
`analysis.py` and `serialize.py` never import it, so no headline number, no
calendar day and no annual row can be traced back to a model. The journal's
credibility rests on that separation: "nothing counts until the position is
flat" is worth little if a modelled figure can reach the same card.

**The allowlist names a module that exists for pricing.** This layer lived in
`bars.py` while that module owned both the modelled series and the manifest, the
upsert and the audit — so `tests/test_layering.py` had to permit pricing in a
module most of which has no business with it. Nothing about a bar window needs
Black-Scholes, and now nothing in `bars.py` can reach it: the allowlist is
`{replay, demo}`, and `replay.py`'s whole purpose is the modelled numbers. The
dependency runs one way, `replay.py` → `bars.close_series`, because a vol solve
needs the closes storage keeps.

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

   A leaf is the escape hatch when two domain modules need one rule and
   neither may hold the other. `history.py` reads note codes out of SQLite
   and `analysis.py` reads them off a statement, so neither can own the
   rule for both; `notes.py` does, and each imports it. The alternative
   was what stood there before — the rule written twice, once per reader,
   agreeing until one input shape changed.
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
  tabs run over. Positions, Annual and 0DTE stay pinned to options.

  Costs carries its own control, because it answers a different question. It is a
  MULTI-select — costs on disjoint asset categories add up, so "options plus the
  conversions I make to trade them" is a real question — and it is styled as
  chips rather than as the segmented `.seg`, since that shape means "pick one"
  everywhere else in the UI. 0DTE appears among the four options but is not a
  category: it is a subset of options, resolved to fills by `stats.odte_scope`,
  so ticking it narrows rather than widens and the chip is dashed to say so.

  What that control deliberately cannot do is narrow a cost that carries no
  attribution. Account fees and withholding are levied on the account — no fee
  row in this archive carries a contract or trade id — so they are shown whole at
  every selection, in their own block, labelled as attributable to nothing.
  Hiding them under a narrow scope would make a tab captioned "broker cost"
  quietly measure less than it claims.
* **Options P&L counts fully closed round trips only, attributed to the
  close date.** A partial close (sold 3, bought back 1) contributes
  nothing until the position is flat, and premium collected on an open
  short is a liability, not profit — it is shown separately as "open
  premium". Other asset categories keep IBKR's per-fill realisation.
  `Gain % of Net Liq` divides that P&L by the NAV from the statement's
  Equity Summary section (enable it on the Flex query template; the demo
  carries synthetic NAV rows).
* **The money counts contracts; the scoreboard counts positions.** Three
  units, narrowing: `fills` are executions, `closed` are contract round
  trips (an *episode*, and what net P&L is attributed by), `decided` are
  positions (a *campaign*, and what W/L/win rate measure). They differ
  because a roll closes one contract and opens the next: on the episode
  unit that scored one continuing decision as two closed trades and two
  wins, and a two-conid put vertical as one win PLUS one loss on a spread
  that netted +562.33. Measured on the demo: 9 closed / 7W / 2L / 77.8%
  by contract against 7 / 6W / 1L / 85.7% by position, with net P&L
  identical at 3695.08 — the money does not move, only the counting.
  A campaign is decided only when every contract in it is closed, and its
  outcome is the SUM of them, so a loser rolled out and scratched on its
  final leg is still a loss. `wins + losses == decided` always; against
  `closed` it need not, and both columns are on screen with a note
  wherever they differ. See `campaigns.py`, which owns the rule.

  Two surfaces deliberately keep the contract unit and say so: `optjournal
  history` (it lists episodes) and the 0DTE cohort (a cohort is defined by
  a contract's expiry, and a rolled position spans several).

  `month_stats` builds its own linkage via `stats.campaigns_for` when a
  caller passes none, so the corrected figures are the default and not an
  opt-in. The `campaign_list=` argument is a COST optimisation only: the
  Annual tab asks for a dozen months, two years and a total from one
  report, and `_period_stats` builds the linkage once for all of them.
  Passing nothing is always correct, just one query per period. It used to
  fall back to one campaign per episode, which meant a forgotten keyword
  silently produced the pre-campaign reading — a default that is wrong in
  silence, and the reason most of the suite was measuring the old rule.

  The two units part company in exactly one place, and the Net P&L card
  names it: a roll settles its near contract for real cash while the
  decision carries on, so that money is in the P&L and out of the
  scoreboard. `inflight_realized` is how much — €270.47 on this account,
  the GOOG 420C closed when the strangle was rolled — and the note reads
  "of this closed inside a position still running". It is NOT netted out of
  Net P&L: the cash left the broker and is on the tax return, so removing
  it would stop the panel reconciling against the statement and break
  monthly rows summing to annual ones. It is the third member of a family —
  `open_premium` is cash collected with no outcome yet, `open_commission`
  is cash paid with no outcome yet, this is cash *settled* with no outcome
  yet. Provenance, never a forecast: it can fall, since rolling a winner
  into a loser leaves the finished campaign worth less.

  A toggle was considered and rejected. Every control on this page exists
  because two readers want different DATA (`type` filters the population,
  `ccy` restates it, the calendar filter has two axes because one could not
  express "USD, every impact"). Nobody wants a Net P&L that IBKR never
  reported, so the gap is a labelling problem, not a choice — and a
  presentation toggle would have to live in the hash like `theme` and
  `ccy`, which would let a shared link carry a non-broker-stated P&L with
  nothing on screen saying which mode produced it.
* **The payload contract lives in the page, and the suite derives its
  guards from it.** `page.html` opens with `@typedef` blocks declaring
  every shape the page reads and a `@payload`/`@local` table saying which
  binding holds which shape. `tests/test_web.py` parses those blocks and
  enforces the chain in every direction: reads must resolve against the
  typedefs, the typedefs must match a real payload both ways (a required
  key the API stops sending fails, and a key it sends undeclared fails),
  and the binding table may be neither incomplete nor stale. A typo'd key
  fails a test instead of rendering a blank cell.

  That chain holds at TEST time. At RUNTIME the two halves reload
  differently: `page_html()` re-reads the file on every request (so editing
  markup needs no restart), while the Python is loaded once at import. A
  server left running across a merge therefore serves NEW markup against an
  OLD payload, and a card reading `undefined` looks like data loss rather
  than a process needing a restart. Happened twice while the campaign unit
  was being built. `staleServerCheck` closes it: `STATS_KEYS_REQUIRED` names
  the newest `stats` keys, and their absence raises the banner the page
  already has for things the reader must act on, naming the missing keys and
  the fix. Two tests hold the list to the typedef and to a real payload (a
  guard watching a renamed key would pass unconditionally while protecting
  nothing) and pin that it runs between the payload landing and the first
  draw. Verified against a server serving current markup over a stale
  payload: banner fires, cards still render, no console errors.

## Measuring the suite

`optjournal mutate` injects a known defect and reports which tests notice. It
answers a different question from coverage: not "did this line run" but "would a
wrong line be caught, and by how many tests".

Two results are worth acting on. A real defect caught by **zero** tests is an
unguarded invariant — that is how a `_flat` epsilon wide enough to book a
0.4-share residual as a closed round trip was found, having passed 579 tests, and
how `_snapshot_leg` silently taking `abs()` of a short position was found. A
defect caught by **fifteen** tests suggests fourteen are coupled to something they
are not about.

Measured over 36 mutants, after the harness repair described below: **31
caught, 5 uncaught**, median 1 test, maximum 20. Five more were added later and sit
outside those counts: four for the `stats.py` gaps described at the end of this
section, and `sync-empty-ok` for the sync job's own reading of the reply. Each was
verified lethal against its own test file individually, but none has been through a
full survey run, so folding them into the totals above would report a measurement
nobody took. The registry holds 41. The two newest of the original 36 guard the campaign
unit — `roll-continues` (a roll counted as decided while a leg is still open) is
caught by 2 tests, and `campaign-sum` (a campaign scored by its final contract
rather than the sum of them) by 1, the demo case where a loser is rolled out and
scratched.

At the top end, `fee-scope` is caught by 20 tests and `cost-scope` by 6. That is
the "fourteen coupled tests" signal above, and it is worth a look: account fees
appear in so many assertions that a change to how they are scoped fails most of
the suite, which tells you little about where the rule actually lives.

**The harness needs the suite to be green in a COPY of the checkout**, because it
runs each mutant in a `copytree` clone and refuses to measure against a baseline
that already fails (`dirty-baseline`). Two tests could never satisfy that, both
correctly: `test_launchd` asserts the plist execs *this* checkout's console script
(launchd stores absolute paths, so a moved repo is the failure it exists to
catch), and `test_flex.test_raw_dir_is_populated` asserts the real archive is
present, which a fresh worktree has no copy of. So every mutant came back
uncounted while the tool still printed a summary line — a survey that looks like
it ran is worse than one that visibly did not.

The first full survey after the repair found **five mutants caught by zero
tests**, every one of them pre-dating the repair and none of them noticed while
the harness was silently measuring nothing:

| mutant | what would break |
|---|---|
| `wire-enum` | the payload would carry `AssetClass.STOCK` where the page reads `STK` |
| `broker-stamp` | `ingest --broker X` would read X's statement and file every row under `ibkr`, making the argument decorative |
| `legs-merge` | two brokers' fills for one order id would SUM, reporting a position of -3 as -6 |
| `current-book` | whichever broker filed most recently would decide what counts as current for all of them |
| `held-scope` | a lagging broker's held positions would be invisible to the open/closed decision, so a position still open reads as CLOSED |

Four of the five are the multi-broker seam, which is exactly the area with no real
second-broker data to test against — the gap the mutants were written for, left
unmeasured because nothing was running them. They are unguarded invariants, not
known-good behaviour, and each wants a sentinel test.

Both blocking tests now carry `conftest.skip_if_copy`, which detects a copy from
what git leaves
behind (`.git` is a FILE in a worktree, absent in a clone, and a DIRECTORY only in
the real checkout) rather than from path names. The guards still guard where they
mean something, and the harness gets its green baseline. The same skip is why
`uv run pytest` is clean inside a git worktree, which is where most of this
project's changes are written.

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

**Counting test references is not measuring coverage**, and this is the cheapest
lesson here. An architecture review grepped `tests/` for each public name in
`stats.py` and reported four functions at zero — `fx_quotes`, `available_years`,
`cohort_data`, `stats_data` — concluding the module's interface was unguarded. The
counts were accurate and the conclusion was wrong: all four run on every
`build_state`, so the payload suite exercises them transitively and no name ever
appears. Breaking twelve of their documented rules one at a time found **eight
already caught** — the option-currency restriction, the rate inversion, the
newest-snapshot preference, the year ordering, the category filter, `win_rate` in
both views, the day list's realised figures. The four that were not are in
`tests/test_stats.py`, and they are all the same shape: a `None` that means "no
data" flattened into a zero that means "measured zero", plus the `> 0` rate filter
standing in front of a division. That is the method the rest of this section
describes, applied to a claim rather than to code.

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

### `Charge`: when withholding the native is the wrong answer

`Money` withholds the as-charged figure the moment a scope spans currencies,
which is right for any figure and wrong for exactly one surface. The Costs tab
is scoped by the reader, and widening from options to the whole account should
*add columns, not delete exactness* — each charge is still known individually;
only the claim that one currency speaks for the total became false.

So a cost is a `Charge`: a base translation plus the whole per-currency ledger,
never collapsed. It adds (that is what makes a multi-select scope possible, each
currency staying its own column through the sum) and it yields its `Money` on
request, so the two types agree where they overlap and the single-figure
surfaces need no special case.

```json
broker_costs.totals.attributable
  { "base": 42.15, "native": null, "ccy": null,
    "charged": { "USD": 21.92, "SEK": 208.41, "EUR": 1.67, "KRW": 4000.0 } }
```

The reader leads with `base`, then dissects into `charged`. Note what `Money`
alone would have said here: `42.15` and nothing else.

The same figure under a single-currency scope keeps its exact reading, so the two
types agree where they overlap and the page needs no special case:

```json
broker_costs.totals.attributable      (scope: options only)
  { "base": 15.16, "native": 17.46, "ccy": "USD", "charged": { "USD": 17.46 } }
```

One case the ledger deliberately cannot express: the AutoFX rate markup has a
base and **no** billing currency, because IBKR never itemised it in one. An
empty ledger with a non-zero base is therefore an *estimate*, distinct from
`is_free` (nothing charged at all), and `costs.Friction` keeps the two apart as
a type rather than as a caption.

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

## Themes

Three palettes ship: **Leather** (the original), **Admiralty** (blue-black with
polished brass, which is what the compass mark was always drawing) and **Ledger**
(deep green buckram and aged gilt). The edition chip beside the title is the
control — it names the active theme and cycles on click — and the choice rides in
the URL hash, so a reload keeps it and a link carries it. No localStorage: this
page persists no other preference that way, and a setting the URL cannot express
is one that disagrees with a shared link.

**A theme has to be visibly different, and that is measured.** The first release
shipped an "Oxblood" whose 48 colours were all within 24/255 per channel of
Leather's — every structural check passed and clicking the chip appeared to do
nothing. Admiralty had the same defect in the two places a reader looks first: the
logo plate and the currency pill stayed brown on a blue-black page.
`test_each_theme_is_visibly_distinct_from_the_default` now measures landmark
distance, and judges the near-black grounds on which channel dominates rather than
absolute distance, since every `--bg` here sits a few points from zero.

The whole mechanism is that **every colour is a custom property, and nothing
outside the theme blocks holds a colour literal.** That had to be earned rather
than declared: 35 hex literals were scattered through the rules — the logo
facets, the edition pill, calendar day borders, put/call, the impact dots — so a
theme swap left brown chrome sitting on a blue page. Promoting them is why the
palette is 68 names long; that is the honest size of this page's colour
vocabulary. `test_no_colour_literal_lives_outside_a_theme_block` keeps it that
way, and `test_every_theme_declares_the_same_palette` keeps a theme a *swap*
rather than a patch — a block missing a name inherits it from `:root`, which
renders one theme's chrome on another's ground, silently and only on the panels
that use it.

**`app.css` is not the only place a colour can hide**, and checking only the
stylesheet is why the performance chart sat out the first release entirely. Its
line and fill were SVG `stroke`/`stop-color` attributes holding hexes, and each
dot was ringed in `#0a0806` — Leather's own *background*, so on Admiralty the
dots wore a brown the page no longer contained. Worse, `note()` set
`style.background` and `style.color` from a literal table: an inline style
outranks every rule, so the message banner could not have been themed even with
the right variable in place. Both were reported from the running app rather than
caught, and `test_no_colour_literal_lives_in_the_page_either` now scans
`page.html` for the same reason the CSS is scanned.

**Usability is measured, not asserted.** The contrast tests recompute WCAG
ratios from the stylesheet for every theme, so retuning a palette is free while
regressing legibility is a red test. Adding the second theme immediately caught
that the original `test_muted_text_...` had been silently checking only the first
block in the file. Between them, the checks found four real defects that reading
the CSS would not have: `--onaccent` failed AA on its own fill in two themes (one
of which, Leather's 4.31:1, predated the themes entirely), and both new themes
shipped a selected tab too close to its neighbours to read as selected — that one
found by **screenshot**, since every text-contrast figure passed while the tab
strip had stopped saying where you were.

**Adding a theme**: one `[data-theme="yourname"]` block in `app.css` declaring
every name `:root` declares → one entry in `page.html`'s `THEMES` table. No
JavaScript to touch, and the tests will tell you which names you missed and which
ratios you broke.

A light theme is the obvious next one and is deliberately not here: it inverts
the bevels (`--bevel*` and `--drop*` assume a lit-from-above dark surface) and
needs every gain/loss hue re-derived, since mint-on-paper fails contrast badly.
The variables it needs already exist, which was the point of naming them.

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

**A new theme**: see [Themes](#themes) — a CSS block plus a `THEMES` entry.

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
