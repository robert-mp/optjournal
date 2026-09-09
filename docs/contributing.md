# Contributing

How to add to this journal, what must never be committed, and how to run it
locally.

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

**A new cron job**: implementation in `cron/`, a loader shim in the host cron
runner's own script directory that locates it by path (never a copy — see
below), and a case in `tests/test_cron.py`. The cron scripts run under that
runner's interpreter, which has no py_ibkr, so they may import nothing from the
`optjournal` package and must shell out to the CLI instead. Exit codes are
mirrored rather than imported for that reason, and a test holds the copies to
`cli.py`'s originals.

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

## Development

```bash
uv run pytest -q            # deterministic tracked fixtures
uv run ruff check src tests cron
uv run mypy
uv run optjournal sweep     # every page in a real browser (~2 min)
uv run optjournal mutate    # full mutation survey; intentionally slower
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

Shared scaffolding lives in `tests/conftest.py` — `RAW_DIR`/`STATEMENTS` point
at the tracked, redacted corpus, while `LIVE_RAW_DIR`/`LIVE_STATEMENTS` are an
explicit opt-in for private acceptance checks. It also provides a migrated
`conn`, a `populated_db`, `add_statement`, and the `code_only` comment stripper
both JS guards use. What a *trade row* contains stays in the module asserting
it: that is the subject of those tests, not setup for them.

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
