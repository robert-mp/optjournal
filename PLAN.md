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
| `bars.replay_model` stops sharing the vol solve | 1 |
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
| 5 | Add `broker` to the schema; make `(broker, trade_id)` the identity. Migrate the existing journal as `ibkr`. | Cheap now, expensive after a second broker's rows land. | M |
| 6 | Honour `account_id` in the domain queries, or state in the README that one file means one account. | Latent silent-merge defect; same scoping fix as step 5. | M |
| 7 | Introduce `NormalisedFill` + a `StatementSource` Protocol; move py_ibkr attribute reads out of `ingest.py` into `sources/ibkr.py`. Registry like `SCOPE_BUILDERS`. | The actual seam. Makes a second broker additive rather than invasive. | L |
| 8 | Rename the IBKR vocabulary that reaches the payload (`conid`, `ib_order_id`, `fifo_*`), serializer + typedefs + binding table in one commit. | Only worth doing once step 7 gives the neutral names a home. | M |
| 9 | Design pass (not code) on computing FIFO realised P&L for brokers that do not supply it. | The one genuinely hard problem. Deserves a decision before implementation. | L |

Steps 1-4 are pure entropy reduction and touch no architecture. 5-8 are the
broker seam. 9 is the thing to think about before committing to a second broker.
Multi-tenancy is deliberately absent: it is a product decision, and the code is
already as ready as it can be without one.

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
