# Architecture

Data flow, the module table, and the layering rules.

`tests/test_readme.py` parses the module table below: a module that ships
without a row here fails the suite, and a row naming a module that no longer
exists fails it too.

```
flex.py ──▶ archive (raw/*.xml) ──▶ ingest.py ──▶ SQLite (db.py)
                                                      │
                       ┌──────────────┬───────────────┤
                       ▼              ▼               ▼
                  history.py      stats.py       analysis.py ◀── raw XML
                  (episodes)   (periods, scopes)  (cost report)
                       │              │           costs.py ◀── the same costs,
                       │              │           (scoped, whole history)  from SQL
                       └──────────────┼───────────────┘
                                      ▼
                     serialize.py (JSON payload contract; wraps analysis's
                                   per-currency ledgers into Money)
                     render.py    (terminal reports)
                          ▲
                     money.py (Money, Charge: held by every layer above,
                               depends on none)
                     notes.py (IBKR note codes: read by history and analysis,
                               which cannot import each other)
                                      │
                            ┌─────────┴─────────┐
                            ▼                   ▼
                         cli.py            web.py + page.html
```

| module | owns |
|---|---|
| `config.py` | filesystem defaults (`raw/`, `journal.db`, `demo/`) |
| `settings.py` | preferences that outlive a process: the Flex query id and the scoreboard unit, in a gitignored `.optjournal.json`. Owns the query id's precedence (argument, then `$OPTJOURNAL_QUERY_ID`, then stored) so `serve`, `sync` and the cron cannot disagree about it. Fails open on damage, like the fetch sidecar: a preference file must never stop the journal reading itself. The SECRET is not here -- that is the keyring, via `flex.py` |
| `flex.py` | IBKR Flex fetch: token, retries, lockout budget, cooldown |
| `fills.py` | `NormalisedFill`: one executed fill in broker-neutral terms. A leaf, like `money.py` -- the seam between a broker's statement and the database |
| `sources.py` | `StatementSource` Protocol and the `SOURCES` registry: reads a broker's statement into `NormalisedFill`s. `IbkrSource` is the only implementation today and the one place that knows py_ibkr's attribute names |
| `archive.py` | statement store: content-hash dedupe, prune |
| `ingest.py` | fills → SQLite, idempotent upserts (stores every asset category; scoping is query-time). Trades arrive via `sources`, so the writer is broker-agnostic |
| `db.py` | connection, schema migration, `open_journal()` |
| `logs.py` | the rotating application log, beside the journal. A leaf. Exists because macOS rotates NOTHING for a launchd agent's stdout: `newsyslog` only touches files listed in `/etc/newsyslog.conf`, so a supervised `serve` appends to one file forever. Two files answer two questions -- launchd's `serve.out.log` is "did the process start", this one is "what did the jobs do" |
| `locks.py` | cross-process `flock`, because the cron and the server are two processes and a `threading.Lock` only serialises two browser tabs. A leaf |
| `jobs.py` | the job registry, the runner, and the run ledger. The registry being CODE is the point: the host cron runner's registration is an unversioned side channel, and the consequence is measurable — four optjournal crons are registered there and none is the calendar refresh, so 143 lines of tested policy have never run on a schedule. Jobs call functions, never the CLI, so a cause arrives as a typed exception instead of an integer. See `SCHEDULER_PLAN.md` |
| `sync.py` | the ONE sync path — fetch, ingest, snapshot — called by `POST /api/sync`, `optjournal sync` and the `sync` job. Its own module because of the import graph, not for tidiness: it briefly lived in `web.py` and `jobs.py` reached it through a deferred import, which `tests/test_layering.py` correctly called a cycle. It raises rather than returning an error dict, because each caller needs a different shape for a cooldown (HTTP body, exit code, ledger status) and flattening them into a string is how a locked keychain became a bare exit 1. Its success reply stays a **dict** on purpose — see [Why the sync reply is not a dataclass](#why-the-sync-reply-is-not-a-dataclass) |
| `history.py` | fills → round-trip episodes (status, 0DTE, holding period) |
| `money.py` | `Money`: an amount, the currency it was charged in, and the base translation. A leaf — imports nothing, so any layer can hold one. See [The Money model](#the-money-model) |
| `notes.py` | IBKR trade note codes (`AFx`, `Ep`, `A`) and the one rule for reading them: whole-token matching, over either the stored `AFx;P` string or py_ibkr's parsed list. A leaf, because its two readers — `history.py` (database) and `analysis.py` (statement) — sit on opposite sides of the graph and cannot import each other |
| `stats.py` | period stats (month/year/all-time), `TradeScope` filters, cohorts. **Never reads `blackscholes.py`** — see [Modelled numbers](#modelled-numbers) |
| `marketdata.py` | price-bar fetch and parse for one contract over one window. A leaf: no DB, no journal shapes |
| `vol.py` | realised volatility from closes, and the move it implies. A leaf, and deliberately NOT `blackscholes` — see [Modelled numbers](#modelled-numbers) |
| `events.py` | economic calendar: fetch, parse and store this week's releases. A leaf. One feed, no Protocol — see [Adding a calendar feed](#a-new-calendar-feed) |
| `clock.py` | the market zone and the four conversions stated against it: `MARKET_TZ`, `epoch_et`, `expiry_epoch`, `et_day`. A leaf. Which zone the journal's stamps are in is a property of the market, settled from real fills in three time zones (see `epoch_et`), and four modules need it at different depths — so it lives where every layer can hold it rather than inside the module that happened to discover it |
| `bars.py` | the journal-shaped half of price bars — which contract over which window (from episodes), the idempotent write, and the series a chart reads. Storage only: it imports no price model, which is what `tests/test_layering.py` now enforces by naming `replay.py` rather than this module in the pricing allowlist |
| `replay.py` | the whole replay concern, in two halves. The MODELLED one is the only place anything is priced: implied vol solved from each contract's own closes, the expected-move band, the mark-to-market series and the effective delta, with one solve shared by the band and the marks so they cannot disagree. The ASSEMBLY one turns lifecycles and position rows into the `replays` map the panel reads — strike segments, fill stamps, event cards — and was `web.py`'s until the concern was made whole. `attach()` is the interface; `build_state` calls it once, last. Holds `bars.close_series` and `bars.replay_bars`, one way — see [Modelled numbers](#modelled-numbers) |
| `blackscholes.py` | option pricing and the implied vol backed out of a market price. A leaf: pure float maths, `math.erf` for the normal CDF, so no numpy or scipy |
| `analysis.py` | cost/friction report from the raw statement (whole account); pure statement mathematics — holds leaves (`notes.py`) and nothing that reads a database |
| `costs.py` | the same costs read from SQLite instead: the account's whole life, narrowed to any set of asset categories, with measured cost kept structurally apart from the estimated AutoFX markup. Reconciled against `analysis.py` statement by statement — see `tests/test_costs.py` |
| `campaigns.py` | which episodes were ONE decision, and what that decision earned. A roll continues a position rather than closing it, and a spread's legs are separate contracts, so the scoreboard's unit is the campaign while the money's stays the episode. A leaf holder (`money.py` only), because its two readers — `strategies.py` for the Trades tab cards and `stats.py` for the win rate — cannot import each other, and the rule living in one of them is how the Dashboard came to count a roll as two wins while the tab drew one card |
| `strategies.py` | orders folded into the strategies they were placed as (a strangle sold as two same-second orders is one group), then linked into position lifecycles. The grouping RULE is `campaigns.py`'s; what lives here is naming the shape and aggregating the legs |
| `serialize.py` | the JSON payload the page renders and `--json` emits; wraps `analysis`'s per-currency ledgers into `Money` |
| `render.py` | human-readable terminal reports. Bound to `serialize`'s shapes by `tests/test_render.py`: it once read flat money keys the `Money` conversion had removed, and `orders`/`history` died on `float(dict)` behind a green suite |
| `cli.py` | argparse wiring only: every command opens the database via `db.open_journal` and emits through `_emit(data, text, json)`, so `--json` comes for free |
| `web.py` | loopback HTTP server; `ServeConfig` injected per server |
| `page.html` | the frontend's markup and JavaScript: no build step, nothing off-origin |
| `static/app.css` | every styling rule, and every colour. Extracted from the page's `<style>` block so all of them sit where `tests/test_web.py`'s layout assertions can read them — four real defects once lived in CSS that nothing in the suite had ever looked at. A `<link>` in `<head>` is render-blocking, so there is no unstyled flash; the extra loopback request measures 1.4 ms. Opens with one `[data-theme]` block per [theme](#themes) |
| `static/replay.js` | the replay chart's arithmetic as pure functions over plain data — no DOM, no globals — so `node --test` can unit-test the scales and the scrub. A function belongs here if it takes data and returns data; the moment it touches `document` it belongs in the page |
| `static/mark.svg` | the Bitácora compass rose as a standalone favicon. Carries its own colours: a favicon renders outside the page, where `page.html`'s custom properties do not reach. `web.STATIC_TYPES` is what lets it be served as an image rather than a script |
| `browser.py` | headless browser discovery and the DOM dump, in three views: raw, markup (scripts stripped), text |
| `sweep.py` | the page matrix and its checks; each a pure function of a rendered page |
| `mutate.py` | mutation testing: known defects, and which tests notice each. Answers "what would a real bug cost" rather than "what is covered" — see [Measuring the suite](#measuring-the-suite) |
| `demo.py` | deterministic synthetic statement; refuses to touch real data |
| `sections.py`, `compat.py` | shims over py-ibkr's partial statement model |
