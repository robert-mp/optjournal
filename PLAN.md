# Readiness plan: second broker, multi-tenancy, and the test suite

Working document, not a design doc. Every claim below was measured against this
repo at `76af14c`; the command or the file:line is given so a reader can re-check
rather than trust it. Steps are ordered so each one is shippable alone and none
requires the next.

## The one-paragraph answer

The domain layer is already broker-agnostic and already tenant-parameterised; the
coupling that matters is concentrated in three specific places, not spread through
22 modules. So this is not a rewrite. **Multi-broker is a real but bounded piece of
work** whose hard part is not the parser — it is that `history.py` trusts IBKR to
compute realised P&L. **Multi-tenancy is not a feature of this app**: the plumbing
is ready, but the security model is a deliberate inversion, so it is a separate
product decision rather than a refactor. And the test suite is not too large, but
that was the wrong question: it is **mis-aimed**. Median 2 tests per defect and
none caught by more than 8, so there is nothing worth deleting -- yet a wider
survey found real defects that all 597 tests miss, including one that makes
`ingest` store nothing and exit 0. See [Measuring the suite](#the-test-suite-measured-and-the-answer-is-no).

## On dependency injection: yes, but narrowly

DI is the right instinct and it is already the idiom here — `bars.backfill_bars`
takes `fetch: Callable[..., list[Bar]] = fetch_bars` (bars.py:393) so the suite
exercises the whole path offline, and `stats.SCOPE_BUILDERS` (stats.py:461) is a
string-keyed registry the UI and the `?type=` parameter share.

What I would NOT do is introduce a DI container, a `Broker` abstract base class, or
constructor-injected services throughout. Those solve a problem this codebase does
not have: nothing here needs runtime-swappable object graphs, and the README's
layering rule already achieves the same end more cheaply — entry points construct
dependencies and pass them down, so nothing below reads a global. A container
would add indirection at every call site to buy what a function parameter already
buys.

The DI that pays here is one seam with two implementations:

```
                    ┌─ ibkr.py    (py_ibkr, Flex XML, camelCase attrs)
StatementSource ────┤
   (Protocol)       └─ schwab.py  (whatever that API gives)
                              │
                              ▼
                      NormalisedFill  (frozen dataclass, broker-neutral)
                              │
                              ▼
                    ingest.py ──▶ SQLite
```

A `typing.Protocol` rather than an ABC: structural typing means a source is
anything with the right shape, no inheritance and no registration, and it keeps
`ingest.py` from importing either broker. The registry that maps `--broker ibkr`
to an implementation should look exactly like `SCOPE_BUILDERS` — a dict, with
unknown keys failing loudly (not open, unlike the trade-type scope, because
silently journalling the wrong broker is worse than an error).

**The seam belongs between `flex.py` and `ingest.py`, and today there is none.**
`ingest.py:250-326` reads py_ibkr's camelCase attributes directly
(`t.assetCategory`, `t.tradeID`, `t.fifoPnlRealized`, `t.ibExecID`) and calls
`flex.load()` itself (ingest.py:31). That is the single most important structural
fact in this plan: the parse boundary the README describes does not actually exist
in code, so every broker-shaped decision reaches into the ingest.

## What is already right (do not touch)

Measured, not assumed:

- **No domain module reads global config.** `DEFAULT_DB`/`DEFAULT_ARCHIVE` appear
  16 times, all in `config.py` and `cli.py`, as argparse defaults. `history`,
  `stats`, `serialize`, `ingest`, `bars`, `money` reach for none of them.
- **Two isolated journals already serve concurrently in one process** — verified by
  running the real journal (7 trades) and the demo (25 trades) on two ephemeral
  ports simultaneously, each returning its own data.
- `flex.read_token(account=...)` already takes an account; the fetch cooldown is
  already keyed off `archive_dir` (`flex._state_path`).
- `money.py`, `blackscholes.py` and the bars/replay layer are broker-neutral
  already. `MonthStats` carries no IBKR field.
- The flat 22-module layout is **not** the friction. The dependency graph is
  acyclic and `test_layering.py` enforces it. Subpackages would churn every import
  in the repo to buy tidiness, and would break `config.ROOT`'s `parent.parent.parent`
  and `web.PAGE_PATH`. Not worth it.

## The three real couplings

### 1. Realised P&L is trusted, not computed — the deep one

`history.py:306-307` reads `row["fifo_pnl_realized"]` straight from IBKR, and the
module docstring (history.py:12-18) documents why: IBKR's figure is already net of
both legs' commission, verified arithmetically against a real trade. A broker that
does not supply per-fill realised P&L forces this journal to compute FIFO itself.

That is a change to the domain's core, not a new parser, and it is the one item
here that deserves its own design pass before code. Note the consequence for
correctness: `net_of_commission` currently records a fact about IBKR. Computed
P&L would have to record the same fact about itself, or the "never subtract
commission twice" rule silently breaks.

### 2. `trade_id TEXT PRIMARY KEY` — broker identity is missing

The trades table keys on IBKR's own trade id as a global identity, with no broker
column (only `source_file`). Two brokers can collide. This is cheap to fix now and
expensive to fix after a second broker's rows are in the file.

### 3. IBKR vocabulary reaches the page

Counted in the live payload: `conid` 37 times, `ib_order_id` 36, `fifo` 10,
`fx_rate_to_base` 5. `Episode` carries `conid`. These are contract keys and
`page.html`'s `@typedef` blocks declare them, so renaming is a coordinated
change across serializer, typedefs and the binding table — mechanical, but it
must be one commit or the drift tests fail (which is the system working).

## Found while planning: accounts are recorded but not honoured

`trades.account_id` is populated, but **no domain query filters on it** — verified:
`build_history` and `month_stats` contain no reference to it, and the domain layer
mentions `account_id` once in total. The real journal has one account so nothing is
wrong today, but the moment a second account's statement is ingested, every
figure silently spans both books. Same class of latent defect as the broker key,
and it argues for fixing scoping once, for both.

## Multi-tenancy: the honest answer

`web.py:8-13` states loopback-only and no-auth are "a deliberate pair" because the
page exposes an entire brokerage account and one endpoint spends real IBKR
requests. `serve()` refuses any non-loopback bind (web.py:750).

Multi-tenancy inverts that. It needs identity, authn, per-request authorisation on
every payload key, and a threat model this code explicitly does not have. That is
a different product, not a feature — and the preparatory work is already done,
because the domain is path-parameterised. So: **do nothing for multi-tenancy now.**
The correct next step is not code, it is deciding whether you want a hosted product.
If you do, the work is an auth layer plus per-tenant isolation at the entry points,
and the domain follows unchanged. That is the payoff for the layering discipline.

One cheap thing was worth doing regardless, and is now done (`1370bca`):
`demo.assert_not_real` scoped demo/real separation by PATH, so per-tenant paths
would have multiplied what it had to know. It now asks the DATA, as
`write_demo_bars` already did. That turned out to fix a live defect rather than
merely prepare for one -- the path test refused only the developer's own checkout
and waved through every copy of it, and it was also why the suite failed from a
copied tree, which had blocked the mutation survey entirely.

## The test suite: measured, and the hunch was backwards

The hunch was "we have too many unit tests". Measurement says the opposite: not
one test was worth deleting, and 25 mutants later the suite has grown by 21 tests
to close defects it could not see. The evidence came in two rounds; the first is
the table below, the second is item 3 after it.

Mutation survey, 16 real defects injected one at a time in scratch clones. Each
run proved the mutation was the code pytest actually imported before trusting the
count -- see "the method" below, which mattered more than the results.

| defect | tests that caught it |
|---|---|
| `money.one_currency` gate: report the first of several currencies | 8 |
| `Money.payload` omits null keys instead of always emitting three | 3 |
| `Money.__abs__` drops the currency | 1 |
| `Money.per` divides base only, not native | 1 |
| `money.win_rate` returns 0.0 instead of None when nothing decided | 1 |
| `history._flat` accepts a 0.5 residual | **0** |
| `stats._in_period` compares the raw stored value, not the normalised day | 1 |
| `month_stats` attributes closed P&L by opened date | 2 |
| `marketdata._on_grid` keeps the off-grid live stub | 2 |
| `bars.epoch_et` reads journal stamps as UTC, not ET | 2 |
| `bars.expiry_epoch` accepts only one of the two stored formats | 3 |
| `replay.replay_model` stops sharing the vol solve | 1 |
| `ingest._commission_base` always converts, ignoring the currency | 1 |
| `render._charged` reads base where it means native | 2 |
| `web._snapshot_leg` takes abs() of the seeded quantity | **0** |
| `web.serve` stops refusing a non-loopback bind | 4 |

**The suite is not oversized. It is also not as well-targeted as this table
suggested** -- two corrections, both from being challenged rather than from
re-reading my own work:

1. **The counts here were inflated by one.** The harness counted its own
   stale-mutant guard as a catcher, because a mutation removes the very text that
   guard looks for. Fixed in `2886cd1`. A disputed `_num` figure I reported as 11
   is really 4. Corrected across 15 mutants: median 2, max 8, min 1 -- the shape
   holds, the numbers were wrong.

2. **Median-2 measures the defects I chose, not the codebase.** This survey covered
   8 of 22 modules. A wider one found real defects that all 597 tests miss, and the
   worst was in code written the same day: dropping the `"ALL"` sentinel from
   `cli._asset_filter` makes `optjournal ingest` store ZERO trades, positions and
   securities and exit 0. Every test passed because they all hand
   `ingest_file` the already-decoded `ASSET_FILTER_ALL` constant and never
   exercise the decoder. Closed in `a1f34d1`.

So "do the tests earn their place" and "does the suite cover the code" are
different questions, and answering the first was not evidence for the second.

3. **A wider round settled it: the suite was undersized, not oversized.** Ten
   more mutants, one per audit finding, written BEFORE any test. **Eight were
   caught by nothing**, and 602 tests said so:

| defect | before | after |
|---|---|---|
| `analysis` attributes a KRW fee to the EUR base (313 archived rows) | **0** | 1 |
| `analysis` widens the FEES gate and books interest received as a cost | **0** | 4 |
| `analysis` keeps IBKR's negative sign on withholding, inverting the rate | **0** | 3 |
| `serialize._wire` leaks `AssetClass.STOCK` where the page reads `STK` | **0** | 1 |
| `render.table` stops sizing columns to their headers | **0** | 1 |
| `web._strikes_of` sides a closed contract by its CLOSING fill | **0** | 2 |
| `flex.fetch` loses the request-budget cooldown (no test existed at all) | **0** | 1 |
| `analysis` truncates each fractional fill — **a live defect, not a gap** | **0** | 6 |
| `analysis.credit_fills` counts charges as credits | 2 | 3 |
| `sources._qty` rounds a 0.0007-share fill to 0 | 1 | 1 |

   Two things this round taught that the first one could not. First, the reason
   these were invisible is *shape*, not luck: every one produces a well-formed
   report with a wrong number in it, and three of them (the fee currency, the
   withholding sign, `credit_fills`) had tests nearby that asserted on
   hand-built dataclasses and so never reached the code that computes them.
   Second, "no test caught it" has three answers, not two: an eleventh candidate
   was neither a gap nor a defect but unreachable code, and the right response
   was to delete it (`3e82b21`).

Both zeroes are now closed (`f3a23dd`, `3709773`), and they were different in
kind. `_flat` was a genuine test gap on a live path -- it decides whether a round
trip is CLOSED, and fractional stock lots make it reachable on real data.
`_snapshot_leg` was a FIXTURE gap: the path is unreachable in both journals
because every short they hold is claimed by an open lifecycle, so no test could
have caught it without constructing the case.

So the honest revision to the hunch: there is nothing worth deleting for its own
sake. The `test_web.py` source-greping is the one place I would still look, but it
is 1,770 lines *because* the frontend has no executable seam -- the fix is more
`replay.js`-style extraction, and the regex guards then fall away as a
consequence. That is a frontend task, not a test-cutting task.

### The one deletion candidate, and why I did not take it

`test_analysis.py` parametrises 7 tests over all 8 archived statements: 56 of its
99 nodes. The audit recommended collapsing that to 3 representatives on the
grounds that one statement alone reaches 216 of 220 lines.

I could not reproduce the premise. Hashing each statement's structural features
-- asset classes, currencies, commission-currency mismatches, cash types, credit
commissions, sections present -- gives **7 distinct sets out of 8**. Only
`20260805T092057Z` duplicates `20260804T153112Z`.

Line coverage and feature distinctness are different measures, and the fan-out
guards the second. Measured across the eight: the SEK withholding appears in
**2**, and the fractional stock lot in the same 2. Three of this round's eight
findings (the withholding sign, the fee currency, the per-fill truncation) were
measured against features that a 3-fixture sample chosen for line coverage could
easily have dropped -- so the cut would have removed the evidence that later
proved those defects real.

The honest cut is one fixture, roughly 7 nodes, which is not worth a commit.
Recorded because the recommendation looked well-evidenced and its central number
did not survive being checked.

### Where it landed

Full survey over all 25 mutants, after the sentinels: **25 caught, median 2,
minimum 1, maximum 18, and no measurement the harness refused to trust.**

The maximum moved 8 -> 18 and now sits on `serialize._num` rather than the Money
gate. That is not a better result. Eight of those 18 are one test parametrised
over the eight statements and six more are `costs_data` assertions that each read
a number, so it is four concerns fanned out over a chokepoint every payload flows
through -- whereas the gate's 8 are eight different layers applying one rule. The
headline number cannot tell those apart, which is the argument for keeping the
per-mutant test lists in the output and not just the counts.

Net effect on the original hunch: the suite grew from 595 to 642 tests and
nothing was deleted. What got deleted was code.

### The method, because it produced three false results first

Every one of these failures presented as "the mutation was caught by nothing" --
an alarming coverage result rather than a broken harness:

1. `uv run pytest` inside a clone resolves to the ORIGINAL project.
2. `cp -R` copies `.venv`, whose editable-install `.pth` hardcodes the original
   repo's `src`, so even the clone's own interpreter imported the original.
3. On macOS `/tmp` is a symlink to `/private/tmp`, so the guard I added to catch
   (1) and (2) rejected correct clones.

The working recipe: clone, rewrite `.venv/.../_editable_impl_optjournal.pth` to
the clone's `src`, then run `env -u PYTHONPATH -u VIRTUAL_ENV .venv/bin/python -m
pytest`, and assert with a canary that the imported module lives under the clone
before believing any number. My first "1 of 579" claim about the Money gate came
from (1) and was wrong: the real answer is 8.

## Tasks

Independently shippable, in order. Effort is my estimate of focused work.

| # | Task | Why now | Effort |
|---|---|---|---|
| ~~1~~ | ~~`one_currency` gate sentinel~~ — **VOID**: already guarded by 8 tests. My "1 of 579" was a broken-harness artefact. | — | — |
| ~~2~~ | ~~Mutation survey~~ — **DONE**: 16 defects, table above. | — | — |
| ~~3~~ | ~~Cut redundant tests~~ — **DONE, as nothing to cut**: median 2 tests/defect. Instead CLOSED the two zeroes (`f3a23dd`, `3709773`). | — | — |
| ~~4~~ | ~~Scope `assert_not_real` by data~~ — **DONE** (`1370bca`), and it unblocked the survey. | — | — |
| ~~4b~~ | ~~Sentinels for the remaining audit findings~~ — **DONE** (`8ff9a4e`, `952a007`, `3e82b21`). Ten mutants written first: **2 caught, 8 uncaught**. See below. | — | — |
| ~~5~~ | ~~`broker` in the schema, `(broker, trade_id)` identity~~ — **DONE** (`c5c071a`), extended in `eeef122`: `cash_transactions` and `position_snapshots` needed the same key and did not get it first time. | — | — |
| ~~6~~ | ~~Honour `account_id`~~ — **DONE** (`dcb49d5`): episode identity is `(broker, account_id, conid)`. | — | — |
| ~~7~~ | ~~`NormalisedFill` + `StatementSource` Protocol~~ — **DONE** (`03ac3d7`) and then **actually finished** (`eeef122`). See below: the first pass looked complete and was not. | — | — |
| 7b | Move the remaining sections across the seam: cash, positions, securities and equity summaries still read py_ibkr models and `raw_sections` dicts directly in `ingest.py`. | The trade path is done and is the dense one. These four are the rest of the same job, and a second broker needs them. | M |
| ~~14~~ | ~~Market Awareness + Watchlist~~ — **DONE** (`c222144`, `1343981`, and this commit). Realised vol, not implied: the measurement is in `vol.py`'s docstring. | — | — |
| 8 | Rename the IBKR vocabulary (`conid`, `ib_order_id`, `ib_commission`, `fifo_*`, `ib_exec_id`). **TODO, with a plan below.** | Deferred on purpose: 663 occurrences, another schema migration, and the names are still ACCURATE while IBKR is the only source. | L |
| ~~9~~ | ~~Design pass on computed realised P&L~~ — **DONE as a design, plus the one buildable piece.** Prototype measured against real data, recommendation below, and the FIFO-vs-broker oracle is now in the suite. | Implementation still waits on a real second broker. | — |

Steps 1-4 are pure entropy reduction and touch no architecture. 5-7 are the
broker seam and are done; 7b is its remainder. 8 waits for a real second broker,
and 9 is the thing to decide before committing to one. Multi-tenancy is
deliberately absent: it is a product decision, and the code is already as ready as
it can be without one.

### A seam is only as good as the test that uses two of them

Worth recording, because it is the most transferable thing this exercise
produced. After step 7 the seam had every part a reader looks for: a Protocol, a
registry, a broker-neutral `NormalisedFill`, `broker` on four tables, a composite
primary key, and tests. Registering a second source and running one real statement
through under two broker names found **four defects, none of which one broker can
expose**:

1. `ingest_file(broker=...)` resolved the right source, read the right statement,
   and wrote every row under `'ibkr'` -- the writer had `DEFAULT_BROKER` inline, so
   the argument was decorative and agreed with the schema default.
2. Two of the four tables were still keyed on IBKR's own numbering, so a second
   broker's colliding id would be silently swallowed by `ON CONFLICT DO NOTHING`.
3. `trade_legs` GROUPed without `broker` and summed two brokers' fills into one
   leg: 18 trades became 8 legs with doubled quantities.
4. Two separate `MAX(report_date)` queries let the most recently filed broker
   define "current" for all of them. In `history._held` that decides open versus
   closed, so a lagging broker's open positions read as CLOSED.

Every one produces a well-formed answer with wrong numbers in it, and every one is
unreachable while `SOURCES` has a single entry. The generalisable rule: an
abstraction with one implementation is untested by construction, however complete
it looks, and the cheapest test is a second implementation that reuses the first
one's reader so the DATA is identical and only the plumbing differs.

The same round found a bug in the mutation harness itself (`cf2b734`): it counted
only pytest's `FAILED` lines, so a defect caught by a *fixture's* assertion -- which
pytest reports as `ERROR` -- read as caught by nothing.

## Task 9: computing realised P&L — the design pass

This was the item I called "the one genuinely hard problem". Having now built a
prototype and measured it against real data, the shape of the answer is clearer
than expected, and one thing is settled: **the arithmetic is not the hard part.**

### What was measured

A FIFO lot-matching walk over `trades` alone (no IBKR P&L read at all) reproduces
IBKR's `fifoPnlRealized` **to the cent** on both closed positions in the archive,
including SIVE — the case the `history.py` docstring cites, bought 400 then 1,400,
sold 1,800, then re-entered three days later:

| | |
|---|---|
| price basis | 400 × 27.96 + 1,400 × 36.72 = 62,592.00 |
| + opening commission | 62,642.46 |
| proceeds | 1,800 × 73.00 = 131,400.00 |
| − closing commission | 131,326.14 |
| **P&L** | **68,683.68** — IBKR's figure exactly |

So the model is: match closes against opens oldest-first, P&L is
`(exit − entry) × qty × multiplier` plus BOTH legs' commission share. Everything
that needs is already stored, on every fill: `quantity`, `trade_price`,
`multiplier`, `open_close`, `ib_commission`, `date_time`. Verified — no nulls.

**But be precise about what that match proves.** Both closed positions are FULL
liquidations — SIVE sold 1,800 against lots of 400 + 1,400, consuming every lot.
When the whole position goes, FIFO, LIFO and specific-lot all produce the same
number, because lot ORDER only matters when some lots survive. The SIVE sale even
carries IBKR's `SL` (specific-lot) note, and FIFO still matched — not because FIFO
is confirmed, but because on a full close the method is unobservable. The archive
contains no partial close, so **the arithmetic is verified and the lot-matching
POLICY is not.** That distinction is the difference between "we can compute this"
and "we know which method the broker used", and only the first is established.

### What is actually hard

Not the algorithm. Three things around it:

1. **The commission convention is a policy, not a fact.** Running the same walk
   against the DEMO journal mismatches on 7 of 11 contracts. That is not a bug in
   the walk: the demo's `realized` values are hand-written literals, and for the
   expiry case it charges only the OPENING commission (628.05) where the walk
   charges both (626.10). The real archive contains no expiry, so it cannot
   settle which is right — and this is exactly the kind of question a second
   broker will answer differently. **Whatever is built must record which
   convention produced a number**, which is what `Episode.net_of_commission`
   already does for IBKR and would have to do for itself.
2. **This account ALREADY sells specific lots, and a computed figure can never
   follow that.** Not a hypothetical about some future broker: the SIVE sale
   carries `notes="SL"` — IBKR's specific-lot marker, which `history.py:33`
   documents as a lot-matching method precisely so it is not mistaken for a
   closure type.

   Which lots were sold is reported in IBKR's LOT-LEVEL detail:
   `origTradeID`, `origTradePrice`, `origTradeDate`, `holdingPeriodDateTime`.
   py_ibkr models all four and `raw` preserves all four, so nothing is being
   dropped here. They are EMPTY because the Flex query asks for
   `levelOfDetail="EXECUTION"`, and lot detail is a different level the query
   template does not enable.

   So the limit is structural rather than a matter of effort: **no computation
   over execution-level fills can reproduce specific-lot selection**, because the
   lot-to-close mapping is the input it lacks. FIFO agreed on SIVE only because
   the sale was a full liquidation — every lot consumed, so FIFO, LIFO and
   specific-lot all give basis 62,592.00. On a partial close, which is exactly
   the trade you make to optimise tax, FIFO reports the wrong gain and in the
   direction that overstates it.

   **This is a broker-side config question, not a code one.** If the journal
   should report a true specific-lot basis, the change is to the Flex query
   template, and it is worth knowing before relying on these numbers in April.
   `raw` has been preserving the fields all along, so enabling lot detail needs
   no refetch of anything already archived.
3. **It changes the domain's core, and every headline depends on it.**
   `realized_pnl_base` feeds win rate, profit factor, expectancy, the monthly and
   annual tables, cohorts, strategy groups and the calendar. There is no partial
   rollout: the figure is either the broker's or ours.

### The recommendation

**Compute it, but only as a fallback, and never silently.** Concretely:

- `NormalisedFill.realized_pnl` stays `float | None`. A broker that supplies the
  figure keeps supplying it; `None` means "this broker does not tell us".
- A new leaf module — `lots.py`, alongside `money.py` and `fills.py` — implements
  the FIFO walk as a pure function over fills, importing nothing internal. It is
  the natural home: the prototype is ~30 lines and its inputs are exactly a
  `NormalisedFill` stream.
- `ingest` uses the broker's figure when present and the computed one when not,
  and **stores which it used**. That is a new column, not a comment: something
  like `pnl_source TEXT NOT NULL` holding `'broker'` or `'fifo'`. The reason it
  must be a column rather than an inference is the whole lesson of this project —
  a derived number that looks like a reported one is the defect shape that keeps
  recurring here.
- The page and the terminal reports say so when a figure is computed. Same
  principle as the AutoFX markup caveat, which already distinguishes an estimated
  cost from a measured one.

**Why fallback rather than always-compute.** Tempting to compute everywhere for
consistency, and I would argue against it: the broker's figure is what appears on
the tax document. Recomputing it means either matching to the cent (in which case
the computation adds nothing but risk) or disagreeing with the statement (in which
case the journal is wrong by definition). The measurement above shows we CAN match
— which is the argument for trusting the broker where it speaks, not for replacing
it.

### The one piece worth building before a second broker exists — DONE

Everything above waits. This did not, and is now in
`tests/test_history.py::test_computed_fifo_pnl_agrees_with_the_brokers_own_figure`:
the FIFO walk run against the archive, asserted against IBKR's own figure per
contract and in total.

Two data points today, one more per closed position, so it accumulates the
evidence while nothing depends on it. What it will catch:

- the first PARTIAL close, where FIFO and specific-lot diverge and the archive
  finally says which method IBKR used;
- any drift in the commission convention, since the walk charges both legs and a
  disagreement surfaces as a fixed offset rather than noise.

Three hand-built tests sit beside it, because the archive cannot express what they
check: that the walk charges BOTH legs (asserting it is not the opening-only
reading the demo's literals use), that it matches OLDEST lots first (FIFO gives 50
where LIFO gives 10 — without this, "FIFO agrees with IBKR" could hold for a walk
that is not FIFO), and that a re-entry after a full close does not inherit the
closed lots.

Verified by ablation: charging one leg fails the real-data oracle with
`SIVE: computed 68757.5379 against IBKR's 68683.6800`, and switching FIFO to LIFO
fails ONLY the hand-built test — confirming the archive genuinely cannot
distinguish the methods.

**And the oracle now refuses to be reassuring.** It SKIPS, with a message, when
every closed position is a full liquidation — which is the case today. A green
tick would have read as "specific-lot handled" when the agreement says nothing
about method at all. The predicate behind that skip (`_partially_closed`, computed
from the running position rather than from `open_close`) has its own test, because
both its failure modes are silent: under-report and the oracle skips forever,
over-report and it starts asserting a method-sensitive agreement it has no
evidence for.

A second test pins the limitation itself: every lot field is empty across the
archive, and one fill carries `SL`. Both are asserted, so the day a statement DOES
carry lot detail — or the day this account stops using specific lots — the suite
says the assumption changed rather than leaving it to be discovered in April.

**What was deliberately NOT done: promoting the lot fields to columns.** I
proposed it and then measured it: all 160 trades already carry all six fields in
`raw`, which is precisely the case db.py's docstring describes ("a field can be
promoted to a real column later without re-fetching"). Six always-empty columns
plus a migration would store data that is already stored, to serve a feature that
does not exist. The test asserting they are empty is the whole cost of staying
ready.

It is tests, not features: no schema change, no payload change, nothing in the
domain reads any of it, and the walk lives in the test file rather than in `src`
so it implies no decision task 9 has not made. If any of it fails, the failure IS
the design input this task has been waiting for.

**Then, when a second broker is real**: read one statement. Whether it supplies
per-fill realised P&L decides whether the fallback is needed at all, and its
commission convention decides what the fallback must record. The prototype and
this note make that a half-day question rather than an open one.

## Task 8, the vocabulary rename: STEPS 1-2 DONE, 3-5 DEFERRED

**Steps 1 and 2 are done** (commit `4796fc6`). `conid` -> `contract_id` on every
`fills.py` shape, with `sources.py` constructing them and `ingest.py` translating.
No schema change, no payload key moved, no migration. Verified by re-ingesting the
whole archive into a scratch database and hashing: 162/162 trades, 100/8 cash,
51/51 positions, 13/13 securities, and the `conid` VALUES identical to the live
journal on all three contract-bearing tables. The task-13 fingerprint test -- which
compares real rows against hashes taken from the pre-seam ingest at `dcb49d5` --
still passes, so the rename is transparent rather than merely green.

**Steps 3-5 (the schema, the payload, the mutants) are deferred**, and the reason
is below. Measured after the seam moved:

| name | src | tests | where it hurts |
|---|---|---|---|
| `conid` | 254 | 181 | 8 column decls in db.py, 15 in page.html, 13 other modules |
| `ib_commission` | 36 | 54 | schema column, `_base` sibling, commission-currency rule |
| `ib_order_id` | 31 | 18 | schema, two views, the `Order`/`Leg` payload shapes |
| `fx_rate_to_base` | 23 | 17 | on four tables |
| `fifo_pnl_realized` | 17 | 20 | schema + `_base` sibling |
| `fifo_pnl_unrealized` | 9 | 1 | schema, payload |
| `ib_exec_id` | 5 | 23 | schema, the UNIQUE index |

**690 occurrences**, down from 703 before step 2 and 663 when first measured. Note
what step 2 actually bought: only 19 sites in `src`, and `tests` went UP by 6
(the two new sentinels name the vendor words in order to reject them). So the
remaining work is essentially unchanged in size -- step 2's value is not that it
shrank the problem but that it CONTAINED it. Every seam-to-schema translation is
now in `ingest.py`, guarded by
`test_ingest_is_the_only_place_the_two_vocabularies_meet`.

Still not a sed job: `conid` is a schema column on five tables, a `price_bars`
primary-key component, a payload key the page reads 15 times, and a join key in
`bars.underlying_ids`.

### The surface grows with every feature, and where it grows matters

Tasks 13 and 14 added 40 sites, all in the three names that were already largest.
But the growth is NOT uniform, and the split is what decides the sequencing:

* **`page.html` is unchanged at 15.** Two new tabs, no new `conid` reads. The
  payload surface is stable.
* **`db.py` went 45 -> 56 vocabulary lines and 4 -> 5 keys/indexes**, the new one
  being `PRIMARY KEY (broker, conid)` on `securities`.

So the schema half grows and the payload half does not. `conid` alone is 64% of the
total. Every feature that touches a contract raises the cost of the half that was
already the expensive one.

### Why it is worth doing eventually

The names are accurate today and will become lies. `fifo_pnl_realized` says IBKR
computed this with FIFO lot matching -- true, and `history.py` depends on it being
net of both legs' commission. A broker using average-cost or supplying nothing
would store a differently-derived number under a name that claims FIFO. That is
the failure mode this rename prevents, and it is the same shape as every defect
this project has actually hit: a well-formed answer under a label that no longer
describes it.

`ib_exec_id` and `ib_commission` carry a vendor prefix that will read as "the
IBKR one" beside a second broker's column. And `conid` is IBKR's word; note that
`price_bars` is KEYED on conid but FETCHED by OCC symbol from Yahoo, so there the
conid is purely a local join handle and the name misleads already.

### Why not now

1. **The names are still true.** Every row in the database did come from IBKR, so
   today the vocabulary is accurate rather than misleading. Renaming ahead of the
   second broker buys nothing and carries a migration's risk for no behaviour
   change. This is the whole argument, and it is the only one left standing.
2. **It is mechanical once decided.** The drift tests make it safe: `test_web`'s
   payload guard fails in both directions, so a serializer key without a typedef
   and a typedef without a key both fail. The work is finding the 703 sites, not
   knowing whether the change is right.

### CORRECTION: the migration is not the twelve-step rebuild

An earlier version of this section claimed a column rename "means the twelve-step
rebuild again, over five tables", and used that cost as the first reason to defer.
**That was wrong, and it was never tested before being written down.** Measured
against SQLite 3.50.4 on a scratch database:

* `ALTER TABLE ... RENAME COLUMN` works on a **primary-key component**
  (`position_snapshots.conid`, `price_bars.conid`) with rows present, and the PK
  keeps enforcing afterwards -- an upsert naming the NEW column in `ON CONFLICT`
  updates in place rather than inserting a duplicate.
* SQLite **rewrites dependent VIEW and INDEX definitions itself**. A view whose
  body said `SUM(fifo_pnl_realized)` read `SUM(realized_pnl)` after the rename,
  with no intervention, and still returned the same row.
* A guarded pass (`rename only if old column present and new column absent`) is
  **idempotent**: second and third runs are no-ops, which is what `migrate()`
  requires since it runs on every open.

So the schema step is one `ALTER` per column, not a rebuild per table. That removes
the cost argument entirely. The reason to defer is now only reason 1 -- the names
are accurate today -- which is a judgement about VALUE, not about risk. Worth
being explicit that this correction makes the task *cheaper* than advertised, and
therefore a smaller thing to say no to.

**One ordering trap, confirmed by test.** `migrate()` drops the views, runs
`_SCHEMA`, and only then reaches the table work. A renamed column in `_SCHEMA`
means the view bodies there name the new column, and `executescript` fails with
`no such column: realized_pnl` on an existing journal -- before any rename has
happened. So the rename pass must run BEFORE `_SCHEMA`, which is the opposite of
where `_rekey_by_broker` sits. Anyone doing this task should write that test first.

### The plan: 1-2 done, 3-5 remaining

1. **DONE. The target names are decided**, and `fills.py` had already chosen them:
   `contract_id`, `order_id`, `exec_id`, `commission`, `realized_pnl`,
   `unrealized_pnl`. The seam uses all six, so the database is what disagrees --
   which is the argument for these over any others, and it means the remaining work
   is making the schema catch up rather than inventing a vocabulary.
   (`fx_rate_to_base` needs no change: both sides already agree.)
2. **DONE (`4796fc6`).** `conid` -> `contract_id` and `underlying_conid` ->
   `underlying_contract_id` across all four seam shapes that carry them, plus the
   construction in `sources.py` and the attribute reads in `ingest.py`. Nineteen
   sites in `src`. Two sentinels added, both ablated -- see "What step 2 taught"
   below, because two of its findings change how step 3 should be done.
3. **The schema, in ONE commit**, as a guarded `ALTER TABLE ... RENAME COLUMN`
   pass driven by a `_RENAMED_COLUMNS` table of `(table, old, new)` -- NOT
   `_rekey_by_broker`'s rebuild, see the correction above. The pass runs BEFORE
   `_SCHEMA` (the trap above), skips a column already renamed so it is idempotent,
   and raises if both names somehow exist. One commit rather than one per table
   because a half-renamed schema is a state no `_SCHEMA` can describe: the shipped
   DDL names either the old columns or the new ones, so the views cannot be valid
   for both. Verify on a copy of the live journal before applying: row counts AND
   values, and re-run `migrate()` twice to prove idempotency.
4. **`serialize.py` + `page.html` in ONE commit.** The typedef blocks, the
   `@payload` binding table and the JS readers must move together or the drift
   test fails -- which is the point: it makes this step atomic by construction.
5. **A mutant per renamed payload key**, asserting the page reads the new name.
   The existing `wire-enum` mutant is the model.

Do NOT rename `raw`. It holds the source's own attribute dict verbatim, camelCase
included; that is provenance, and renaming its contents would falsify it.

### What step 2 taught, and what it changes about step 3

**1. The frozen slotted dataclasses make a half-done rename impossible to miss.**
Ablated deliberately: renaming the field but not the construction is a
`TypeError: unexpected keyword argument` across 111 tests, not a silent `None`.

The schema half has a weaker but still adequate version of the same protection,
and it is worth knowing exactly where it stops. Tested, not assumed:

* `INSERT` naming a column that no longer exists -> raises `no such column`. Loud.
* `ON CONFLICT` naming a combination that matches no key -> raises
  `ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint`. Loud.
* Duplicate key with no `ON CONFLICT` at all -> raises `UNIQUE constraint failed`.
  Loud.

So a rename that merely misses a site fails loudly. **The one silent case is an
`ON CONFLICT` that targets a DIFFERENT but still valid unique constraint.** With
`PRIMARY KEY (broker, contract_id)` and a `UNIQUE(exec_id)` index, an upsert on
`ON CONFLICT(exec_id)` accepts a second broker's row and quietly UPDATES the first
broker's instead of inserting -- no error, wrong data, one row where there should
be two. That is the exact shape that bit task 5, and `trades` has both a composite
primary key and the `trades_exec` unique index, so it is reachable. Step 3 must
check every `ON CONFLICT` clause names the intended key, not merely a valid one.

**2. `tests/` grew while `src/` shrank, and that is expected.** The two new
sentinels NAME the vendor words in order to reject them, so a grep for `conid`
now counts the guard as an occurrence. Any future measurement of this task should
count `src` only, or exclude `tests/test_layering.py`, or it will look like the
problem is growing while it is being fixed.

**3. The containment is now enforced, which is what makes step 3 small.**
`test_ingest_is_the_only_place_the_two_vocabularies_meet` fails if any module
other than `ingest`/`fills`/`sources` reads a seam attribute. So step 3's blast
radius is knowable by construction rather than by grep: the schema rename touches
`db.py`'s DDL, `ingest.py`'s SQL strings, and the modules that query columns
directly (`history.py`, `stats.py`, `demo.py`, `bars.py`, `web.py`).

**4. The fingerprint test from task 13 is the right acceptance gate for step 3
too.** It compares real trade rows against hashes derived from a pre-seam
worktree, so it detects a value that changed for ANY reason, including a rename
that silently maps two columns onto each other. Run it, and re-ingest the archive
into a scratch database and diff the counts and the value hashes against the live
journal -- that is what verified step 2, and it caught nothing only because there
was nothing to catch.

### The remaining risk, stated plainly

Step 3 is the only step that touches a live database, and the journal is the
authoritative record of a real brokerage account. `raw/` is the provenance root, so
a rebuild is always possible from the statements -- but a rename that silently
mis-maps a column would produce a journal that looks right and is wrong, which is
the exact failure shape this project keeps hitting. Hence: on a COPY first, values
compared individually rather than counted, `migrate()` run twice to prove
idempotency, and the fingerprint test as the gate.

## Task 14: Market Awareness and Watchlist

Two new tabs. Measured what is actually reachable before designing, because both
features live or die on a data question rather than on UI work.

### What I verified first

**The econ-calendar feed is real and keyless.** ForexFactory publishes
`https://nfs.faireconomy.media/ff_calendar_thisweek.json`: 99 events this week,
flat JSON, six stable keys (`title`, `country`, `date`, `impact`, `forecast`,
`previous`), ISO dates with offset, `impact` in High/Medium/Low/Holiday. The four
USD high-impact events it lists for this week are ISM Manufacturing PMI and the
three 08:30 jobs releases — which is exactly the "Jobs report (NFP) · 7:30 AM ·
high" row in the mockup. So the feature is buildable with `urllib` and no new
dependency, the same way `marketdata.fetch_bars` already calls Yahoo.

**True implied vol is NOT reachable for a symbol you do not hold.** This is the
finding that reshapes the watchlist. `replay._vol_series` solves IV from *the
option's own daily closes*, so it only exists for contracts already in the
journal — measured: TSLA 270617C700 has 461 bars, the traded GOOG/META legs have
14-15, and a symbol you merely watch has none. Yahoo's options endpoint now
answers `{"error":{"code":"Unauthorized","description":"Invalid Crumb"}}`, and the
v6 path is gone, so there is no keyless chain to solve against.

What IS computable from stored daily closes, with no chain and no model:
**realised (historical) vol**. Verified on the real journal — TSLA 63.0% and META
42.4% over 20 sessions, straight from `price_bars`.

That is a different claim from IV and must be labelled as one. Realised vol says
what the stock DID; implied says what the market CHARGES for what it might do. An
options seller reads them differently, and a column headed "IV" showing realised
vol would be the worst kind of defect this project keeps finding: a well-formed
number under a label that does not describe it.

### The recommendation, and where I am pushing back

You chose "IV/vol context" for the watchlist. I would build **realised vol now,
labelled as realised**, and treat IV as a follow-on that needs a chain source. Two
reasons beyond the measurement:

1. It puts the watchlist under the modelled-number quarantine for nothing.
   Realised vol from closes is arithmetic over broker-stated prices — it is
   `stdev(log returns) * sqrt(252)`, no Black-Scholes, no `blackscholes` import,
   so `tests/test_layering.py`'s `MAY_MODEL = {"bars", "demo"}` allowlist does not
   have to grow. Adding `watchlist` to that set is a real cost: the allowlist IS
   the quarantine.
2. An expected-move column derived from realised vol is honest and useful (`last
   x rv x sqrt(days/252)`), and it is the number a seller actually wants for
   sizing. It just must not be called IV.

If you want true IV later, the seam is the same shape as the broker one: a
`ChainSource` Protocol, and the first implementation needs a keyed provider.
Recorded as 14c below rather than guessed at.

### Schema

Two tables, one migration, v6 -> v7:

```
watchlist         (symbol PK, added_at, note, target_price, kind)
market_events     PRIMARY KEY (source, event_id)
                  source, event_id, starts_at_utc, country, title,
                  impact, forecast, previous, raw, fetched_at
```

`market_events` is keyed `(source, event_id)` for exactly the reason `trades` is
keyed `(broker, trade_id)`: an id is the issuing feed's, not universal, and a
second source would silently overwrite the first's rows. That lesson cost two
migrations to learn; it applies here for free. `event_id` is a hash of
`(starts_at_utc, country, title)`, since the feed supplies no id of its own — so a
re-fetch corrects a revised forecast rather than duplicating the event.

`watchlist` has no `broker` column and should not: a symbol you are watching is
not a broker's record of anything. It is the first table in this schema that is
genuinely user input rather than ingested fact, which is worth a note in db.py's
docstring so the next reader does not "fix" it.

### Commits, each independently shippable

**14a — the event source and its table.** ONE module, `events.py`: a fetch, a
parse, a normalised row, a write. No Protocol and no registry.

That is a correction to my own first draft, which proposed a `MarketSource`
Protocol mirroring `sources.py`. It is the wrong lesson to copy. The broker seam
earned its Protocol by having a second implementation in prospect and a schema
whose identity depended on it; a calendar has ONE feed, and an abstraction with
one implementation is untested by construction -- which is the thing this repo
learned the expensive way three commits ago. The `(source, event_id)` key is what
keeps a second feed possible later; the Protocol can arrive with the second feed,
which is when it can first be verified.

Drift test in the shape of `test_flex.py::test_field_drift`: the six keys are
present, and an unknown `impact` fails loudly rather than being bucketed as Low.

**14b — `optjournal market`,** CLI + payload + tab. Text report first (it is the
cheaper thing to verify), then `market_data()` in serialize.py, the `@typedef`
block and `@payload` binding in page.html in ONE commit as the drift test
requires, then the week strip and the day list from the mockup. `sweep.TABS` gains
`market`, which a test already holds to page.html's own list.

**14c — `optjournal watch add/rm/ls`** and the watchlist table. Prices and change
from `price_bars`, position context from `current_option_positions`, realised vol
and expected move computed in a new leaf (`vol.py`) that imports nothing —
deliberately NOT `blackscholes`, so the quarantine allowlist stays at two modules.
Every vol figure labelled `realised` in the payload key itself
(`realised_vol_20d`, not `iv`), so the page cannot accidentally present it as
implied.

**14d — the bars manifest learns about watched symbols.** Today
`bars_manifest` derives windows from positions only, so a watched symbol has no
bars and its row would be empty — measured: GOOG has 5 daily closes, PLTR 4, which
is not enough for a 20-day vol. This is the commit that makes the watchlist
actually populate, and it is deliberately last: it changes what the nightly cron
fetches, so it should land once the read side is proven.

### What to be careful about

* **The feed is `thisweek` ONLY — verified, not assumed.** `ff_calendar_nextweek`,
  `_thismonth` and `_lastweek` all 404. The endpoint returns 2026-08-02..08 and
  nothing else, so a "next FOMC in 12 days" card cannot be built from it. Two
  consequences: the mockup's one-week strip is exactly what the source supports
  (fine), and the TABLE becomes the history — because rows persist, a journal that
  fetches weekly accumulates a past calendar the feed itself will not serve. That
  is an argument for storing rather than rendering straight from the response, and
  for `market_events` being append-and-correct rather than replace-per-fetch.
  Verified the key holds: all 99 rows this week have a distinct
  `(date, country, title)`, so hashing those three is a sound `event_id`.
* **Impact is the feed's judgement, not a fact.** Same class as the AutoFX markup
  caveat: the page should attribute it (`impact per ForexFactory`) rather than
  presenting it as the journal's own assessment.
* **Timezones.** The feed sends ISO with an offset; the mockup shows CDT while
  every existing journal stamp is ET (`bars.MARKET_TZ`). Store UTC, render in
  `MARKET_TZ`, and do not introduce a second display zone — one timeline is why
  fills and bars currently line up without conversion.
* **A cron, not a page fetch.** The Sync button spends an IBKR request and is
  guarded by a cooldown for it. A calendar fetch is cheap but not free, and the
  existing pattern is `cron/optjournal_*.py` shells out to the CLI. Follow it.

## Do not do

- **A DI container or `Broker` ABC.** Nothing needs runtime-swappable graphs; a
  Protocol plus a dict registry matches the idiom already here (`SCOPE_BUILDERS`,
  the injected `fetch`) and costs no indirection.
- **Subpackages for their own sake.** The graph is acyclic and enforced; the churn
  would touch every import and break `config.ROOT` and `web.PAGE_PATH`.
- **Cutting tests by count or percentage.** Measured: the most important invariant
  in the codebase is guarded by almost nothing while the page is over-tested.
- **`tenant_id` columns.** The blocker is authn/authz and a threat model, not
  schema. Adding the column buys nothing and implies readiness that is not there.
- **Splitting `page.html` to fit multi-broker.** The `@typedef` contract is what
  catches payload drift; broker-specific fields fit it as-is.
- **Touching `raw/`.** It is the provenance root, each statement costs an IBKR
  request, and it is the one artifact that cannot be regenerated.
