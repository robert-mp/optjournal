# Testing strategy

The default suite is deterministic. It uses redacted Flex statements committed
under `tests/fixtures/statements/`, temporary SQLite databases, seeded external
events and local HTTP servers. A fresh checkout and CI therefore test the same
data. Private statements under `raw/` are reserved for explicitly named
acceptance checks and never decide whether the normal suite is green.

Run the quality gate with:

```bash
uv run ruff check src tests cron
uv run mypy
uv run pytest -q
```

The suite covers four complementary layers:

- domain and persistence tests over hand-built rows and tracked statements;
- payload-contract guards binding Python serializers to `page.html`;
- pure frontend modules executed with Node's test runner;
- rendered-page checks through a local headless browser.

The source-text frontend guards are intentional. JavaScript commonly turns a
misspelled payload key into `undefined` and then into a plausible blank cell;
rendering alone does not reliably expose that class of defect.

## Mutation testing

`optjournal mutate` injects one known defect at a time and reports which tests
notice. It answers “would the suite reject this wrong behaviour?”, which is
different from line coverage.

```bash
uv run optjournal mutate --only fee-ccy
uv run optjournal mutate
```

Use `--only` while developing one invariant. A complete survey is deliberately
slower because every mutant gets an isolated clone and a clean baseline run.
The harness verifies that pytest imported the mutated clone before reporting a
result; otherwise an editable install could silently execute the original tree.

Interpret an uncaught mutant rather than automatically adding a test:

- a real behavioural defect needs a focused sentinel test;
- an equivalent mutant has no observable effect;
- unreachable or needless code should be removed.

High failure counts also deserve inspection. They may identify a genuinely
cross-cutting invariant, or merely many tests coupled to one shared helper.
Names and behaviour matter more than a stale headline count.

## Browser sweep

`optjournal sweep` renders the page matrix across tabs, filters and journals and
applies checks to each rendered page. It stays outside the fast pytest gate
because launching many browser pages is comparatively expensive.

Every sweep check is itself tested with passing and failing fragments in
`tests/test_sweep.py`. A skipped precondition is treated separately from a pass
so a check cannot become a silent no-op when markup changes.
