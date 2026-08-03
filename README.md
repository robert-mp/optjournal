# optjournal

An options trading journal backed by IBKR's Flex Web Service. Fetches
activity statements, archives them, folds them into SQLite, and serves an
IAG-style local dashboard with cost, history, annual and 0DTE views.

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
`positions`, `history`, `costs`, `statements`, `prune`. Every reporting
command takes `--json`.

## Architecture

```
flex.py ──▶ archive (raw/*.xml) ──▶ ingest.py ──▶ SQLite (db.py)
                                                      │
                       ┌──────────────┬───────────────┤
                       ▼              ▼               ▼
                  history.py      stats.py       analysis.py ◀── raw XML
                  (episodes)   (periods, scopes)  (cost report)
                       └──────────────┼───────────────┘
                                      ▼
                     serialize.py (JSON payload contract)
                     render.py    (terminal reports)
                                      │
                            ┌─────────┴─────────┐
                            ▼                   ▼
                         cli.py            web.py + page.html
```

| module | owns |
|---|---|
| `config.py` | filesystem defaults (`raw/`, `journal.db`, `demo/`) |
| `flex.py` | IBKR Flex fetch: token, retries, lockout budget, cooldown |
| `archive.py` | statement store: content-hash dedupe, prune |
| `ingest.py` | statement → SQLite, asset filter, idempotent upserts |
| `db.py` | connection, schema migration, `open_journal()` |
| `history.py` | fills → round-trip episodes (status, 0DTE, holding period) |
| `stats.py` | period stats (month/year/all-time), `TradeScope` filters, cohorts |
| `analysis.py` | cost/friction report from the raw statement (whole account) |
| `serialize.py` | the JSON payload the page renders and `--json` emits |
| `render.py` | human-readable terminal reports |
| `web.py` | loopback HTTP server; `ServeConfig` injected per server |
| `page.html` | the entire frontend: no build step, no external resources |
| `demo.py` | deterministic synthetic statement; refuses to touch real data |
| `sections.py`, `compat.py` | shims over py-ibkr's partial statement model |

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

Two invariants worth knowing before changing the UI:

* **A tab's numbers change only in response to a control that tab
  displays.** The trade-type filter narrows Dashboard/Calendar/Trades
  (which render the filter bar) and nothing else.
* **The payload is guarded in both directions.** `tests/test_web.py`
  asserts every key the page reads is sent, and every payload binding the
  page uses is registered. A typo'd key fails a test instead of rendering
  a blank cell.

## Adding functionality

**A new UI tab**: serializer in `serialize.py` → emit it in
`web.build_state` → view function in `page.html` + entry in `TABS` +
register in the `views` dispatch → register its payload binding in
`tests/test_web.py::_roots` → tests asserting its figures reconcile with
an existing independent number (see the Annual total-row tests).

**A new trade-type filter**: build a `TradeScope` (fill membership, not a
predicate — see `odte_scope` for why) and register it in
`stats.SCOPE_BUILDERS`. The button in `page.html`'s filter bar and the
`?type=` parameter use the same key. Unknown keys fail open to the whole
journal.

**A new CLI command**: `cmd_*` function + subparser in `cli.py`, opening
the database via `db.open_journal`. Emit through `_emit(data, text, json)`
so `--json` comes for free.

## Data safety

* `raw/` is the provenance root; statements are deduplicated by content
  hash and cost IBKR requests (against a lockout budget) to replace.
* A per-query fetch cooldown refuses to re-spend a request for data that
  cannot have changed. `--force` overrides.
* `optjournal demo` refuses to write into the real archive or database,
  and `serve --demo --query-id` is refused outright: one Sync click would
  ingest real trades into the synthetic database.
* The server binds loopback only and refuses anything else: no
  authentication, and the UI exposes an entire brokerage account.

## Development

```bash
uv run pytest -q            # 212 tests; the raw/ statements are fixtures
uv run ruff check src tests cron
```

The suite covers three layers: unit tests over domain arithmetic (with the
generator itself under test — see `test_demo.py`), payload-contract guards
binding `page.html` to `build_state`, and static checks over the page's
JavaScript (history discipline, hash round-tripping). UI changes should
additionally be eyeballed against `serve --demo`, which exercises paths
the real account cannot reach (closed round trips, spreads, rolls,
expiries, a 0DTE trade, a commission credit).
