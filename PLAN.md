# Deferred work for a second broker

Working document, not a design doc. Every claim below was measured against this
repo at `76af14c`; the command or the file:line is given so a reader can re-check
rather than trust it. Steps are ordered so each one is shippable alone and none
requires the next.

Two tasks from the second-broker readiness plan are still open, and both wait for
a real second broker. The rest of that plan shipped: the `(broker, trade_id)`
identity, `account_id` scoping, the `StatementSource` seam for every section
(task 7b, `37ac3a8`), the mutation survey, and Market Awareness with the
Watchlist. The full plan, with the measurements behind each finished step, is
`git show a64f223:PLAN.md`.

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
