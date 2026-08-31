# Design notes

Decisions whose reasoning is not recoverable from the code alone.

## Why the sync reply is not a dataclass

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

## Modelled numbers

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

## Clocks

Every journal timestamp is **US Eastern**, settled from the data rather than
assumed (`bars.epoch_et` carries the evidence: Stockholm fills land 03:19-10:57
and a Korean fill at 20:03, both inside those exchanges' sessions in ET and
outside them in UTC). The chart labels the same zone, so fills and bars share one
timeline without conversion.

Bars are stored as epoch UTC. Two daily series from the same source are joined on
the ET trading **day**, not the timestamp, because the source does not stamp them
alike: an option's daily bar arrives at 04:00Z (midnight ET) while its
underlying's arrives at 13:30Z (the session open).

## Perishable data

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

> **Deployment.** The host cron runner requires its scripts to live in a
> directory of its own, which is not version controlled, so the implementations
> live here and each deployed file is a **loader shim** that resolves one by path
> at run time. That
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

## The demo's option bars are computed

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
