# Measuring the suite

`optjournal mutate` injects a known defect and reports which tests notice. It
answers a different question from coverage: not "did this line run" but "would a
wrong line be caught, and by how many tests".

**It runs the suite twice per mutant** — a clean baseline it refuses to measure
against unless green, then the mutated run — so the suite's own runtime is paid 82
times over the 41 mutants. Timed end to end, the clone is 1% of a mutant's cost and
the two suite runs are 99%; copying 69 MB per mutant looks like the expensive part
and is not. `--jobs N` runs N mutants concurrently, which is safe structurally
rather than hopefully: each already has its own clone, interpreter and database,
because that isolation is what makes the number trustworthy. Measured over six real
mutants, **750s serial against 144s at `--jobs 6`, a 5.2x speedup with byte-identical
outcomes** — same status, same failed count, same test names. Serial is still the
default, because a concurrent run interleaves the progress lines and a hang is
easier to read about alone.

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
