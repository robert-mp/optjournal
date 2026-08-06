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
product decision rather than a refactor. And the test suite is not too large; it
is concentrated in the wrong places, which is a different fix.

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

The one cheap thing worth doing regardless is `demo.assert_not_real`, which scopes
demo/real separation by PATH. Per-tenant paths would multiply the paths it must
know about; scoping it by DATA (as `write_demo_bars` already does) is more robust
and is a small, independently valuable change.

## The test suite: concentrated wrongly, not oversized

Your hunch is right in aggregate and wrong where it counts. Evidence, from
mutation testing in a scratch clone:

**Dropped the `len(live) != 1` guard in `money.one_currency`** — so a figure
spanning USD, SEK and KRW claims to be a USD figure, the exact defect the Money
model exists to prevent. Result: **1 of 579 tests failed, and it was an unrelated
path test in `test_demo.py`. All 11 money tests passed.** The real journal has
stock trades in four currencies, so this is not hypothetical.

So the suite has a hole at its most load-bearing invariant, while `test_web.py` is
1,770 lines largely greping page source. Cutting by count would make it worse.
The right sequence is: measure with mutation testing, add sentinels where defects
survive, and only then delete the tests that were shown to catch nothing a sharper
test already catches. A test that caught a real shipped bug stays regardless — the
README and several docstrings name those (the `render.py` Money keys, the
`o.fill_count` blank panel, three sweep checks that could never fail).

## Tasks

Independently shippable, in order. Effort is my estimate of focused work.

| # | Task | Why now | Effort |
|---|---|---|---|
| 1 | Add the `one_currency` gate sentinel: a mixed-currency ledger must withhold `native`. | A measured hole at the model's core invariant. Cheapest, highest-value item here. | S |
| 2 | Full mutation survey (~15 defects) in a scratch clone; record which tests caught each. | Turns "too many tests" into evidence. Cutting without it is guessing. | M |
| 3 | Cut what step 2 proves redundant; keep every test that pins a shipped bug. | The entropy you actually asked about, done from data. | M |
| 4 | Scope `assert_not_real` by data rather than path. | Independently right; removes a path assumption before any tenant work. | S |
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
