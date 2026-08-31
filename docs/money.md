# The Money model

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

## On the wire

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

## `Charge`: when withholding the native is the wrong answer

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

## Six constructors, six provenances

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

## Every level aggregates the leaves, never the level below

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

## What stays a flat float, and why

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

## One entry point in the page

`chargeOf(native, ccy, base)` is the rule; `moneyOf(mo)` applies it to a
Money-shaped payload key. There is no second path — `legProceedsOf` and
`natCash` were both absorbed, and a test asserts they cannot come back.
Figures that share a sentence must share a basis, so the rule is applied per
*block*, not per figure: "as charged · $6.97" beside a €6.07 pill is a
contradiction, not a rounding difference.
