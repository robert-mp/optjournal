# optjournal

An options trading journal backed by IBKR's Flex Web Service. It fetches your
activity statements, archives them, folds them into SQLite, and serves a local
dashboard with cost, history, annual and 0DTE views.

The dashboard is branded **Bitácora** — Spanish for a ship's logbook, and for
the binnacle that housed the compass beside it. `optjournal` stays the name of
the package, the CLI and the database; the page is the only thing the brand
touches.

Local tool, one journal per install: single user, loopback only, no
authentication, and your data never leaves the machine. Several people can each
run their own copy; an install is never shared between them.

## Requirements

- macOS or Linux, Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- An IBKR account with a **Flex Web Service** token and an **Activity
  Statement** Flex query (Client Portal → Performance & Reports → Flex Queries)

## Install

```bash
git clone <repo-url> optjournal && cd optjournal
uv sync
uv run optjournal setup      # asks for the token and query id, then verifies
uv run optjournal sync       # fetch + ingest + report what is new
uv run optjournal serve      # dashboard on http://127.0.0.1:8765
```

`setup` asks for two things: the Flex token, which it writes to the OS keyring
without echoing it, and the Flex Query ID, which it stores in
`.optjournal.json` beside the database. It then spends one request confirming
both work against IBKR — a plausible-looking token and a plausible-looking id
still fail together, and finding that out later from a scheduled sync is worse.
`--no-verify` skips that check; `--query-id` and `--token-stdin` make it
scriptable.

No IBKR account handy? `uv run optjournal demo` writes synthetic data to
`demo/`, and `uv run optjournal serve --demo` browses it.

## Updating

```bash
uv run optjournal update            # fast-forward, resolve deps, migrate
uv run optjournal update --check    # report what is new, change nothing
```

It refuses rather than merges: fast-forward only, and a dirty working tree stops
it before it touches the network. It never runs `git stash`. It does not restart
a running `serve` — the server re-reads the page on every request but loads its
Python once at startup, so a code update needs a restart, which the command
tells you about.

## Commands

`optjournal --help` lists everything. The ones you will use:

| command | what it does |
|---|---|
| `setup` | store the Flex token and query id, then verify them |
| `sync` | fetch the newest statement, ingest it, report what is new |
| `serve` | the dashboard, plus the in-process scheduler |
| `update` | fast-forward this install to the latest published commit |
| `history` | closed-position P&L, round trip by round trip |
| `positions` | the current option book |
| `costs` | one statement's cost report |
| `friction` | what the broker cost, across the journal's whole history |
| `demo` | synthetic data in `demo/`, never in `raw/` |

Every reporting command takes `--json`.

Two cost commands, answering two questions. `costs` reads one statement — the
newest archive covers 30 calendar days — and is the only way to see a section no
database column carries. `friction` reads the journal: every ingested fill, over
the account's whole history, narrowable to any set of asset categories.

```
optjournal friction                       the whole account
optjournal friction --assets OPT          options only
optjournal friction --assets OPT CASH     options and the conversions to trade them
optjournal friction --month 2026-08       one month (or a year: 2026)
```

## Configuration

| what | where | set by |
|---|---|---|
| Flex token | OS keyring | `optjournal setup` |
| Flex Query ID | `.optjournal.json` | `optjournal setup`, or Settings in the page |
| Scoreboard unit | `.optjournal.json` | Settings in the page |
| Database, archive | beside the code (`journal.db`, `raw/`) | `--db` / `--archive` |

The query id also reads from `--query-id` and `$OPTJOURNAL_QUERY_ID`, in that
order of precedence, so an existing install or a cron keeps working unchanged.
`$OPTJOURNAL_HOME` moves the settings file, which is what lets the test suite
and a packaged build keep out of the repo directory.

## Wins and losses: two ways to count

The scoreboard counts outcomes in one of two units, switchable in Settings.
**Per position** (the default) treats a multi-leg structure and every roll of it
as one decision, so a hedge leg cannot be a loss inside a winning position and a
loser cannot be rolled out and scratched into a win. **Per contract** scores each
round trip on its own, which is what a broker trade log shows and therefore the
reading to use when reconciling against one.

The money is identical either way — net P&L, commission and the fill counts are
sums over the same round trips — so the two differ only in how many outcomes
that same cash is divided into. On one real account the same 29 closed round
trips read 14W/1L by position and 24W/5L by contract.

## Your data

Nothing is uploaded and the server binds loopback only. What lives on disk:

- `raw/` — your archived statements. They contain your account number, legal
  name and every fill, so they are **deliberately not version controlled**, and
  `.gitignore` carries two spellings of the rule plus a note explaining why.
- `journal.db` — derived from `raw/`, rebuildable with `optjournal ingest`,
  also not version controlled.
- The Flex token — OS keyring only, never a file in this tree, never printed.

See [docs/contributing.md](docs/contributing.md#data-safety) for the full rules,
including what a broad `git add -A` has swept in before.

## Documentation

The design rationale lives in `docs/`, out of this file on purpose: a README
should get you running, not explain every decision.

| document | what is in it |
|---|---|
| [docs/architecture.md](docs/architecture.md) | data flow, the module table, layering rules |
| [docs/design-notes.md](docs/design-notes.md) | clocks, perishable data, modelled numbers, why the sync reply is not a dataclass |
| [docs/money.md](docs/money.md) | the `Money` model: base vs as-charged, and when the native figure is withheld |
| [docs/testing.md](docs/testing.md) | what the suite measures, and mutation testing |
| [docs/theming.md](docs/theming.md) | the theme registry and the contrast rules |
| [docs/trade-confirmations.md](docs/trade-confirmations.md) | same-day fills: what IBKR documents about the confirm query, what is only inferred, and the two columns that block it |
| [docs/contributing.md](docs/contributing.md) | adding functionality, data safety, local development |
| `PLAN.md`, `SCHEDULER_PLAN.md` | open work, with the measured scope for each step |

## Development

```bash
uv run pytest -q          # the suite
uv run ruff check         # lint
uv run optjournal mutate  # mutation testing: see docs/testing.md
```
