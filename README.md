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

## Getting started (no terminal)

1. On GitHub, click **Code → Download ZIP**, and open the ZIP to unpack it.
2. Open the unpacked folder and double-click the Start file:
   - **Mac:** `Start optjournal.command`. The first time, macOS says it cannot
     check the file. Open **System Settings → Privacy & Security**, scroll down,
     click **Open Anyway**, and confirm. On older macOS, right-click the file
     and choose **Open** instead.
   - **Windows:** `Start optjournal.bat`. If Windows says it protected your PC,
     click **More info → Run anyway**.
3. A window opens and the first start installs what optjournal needs, which takes
   a minute. Then your browser opens the journal. **Keep that window open** while
   you use it; closing it stops optjournal.
4. In the page, open Settings (the gear) and paste your IBKR Flex token and query
   id. Saving them starts collecting by itself: the last year first, then the
   older years IBKR keeps, then each trading day's statement the day after,
   while optjournal is open. The dot on the Sync button says whether the journal
   is up to date, and a banner appears only when something needs you.

**Updates:** when a new version is out, the page shows a banner. Click
**Update**, and optjournal installs it and reloads by itself.

**Your journal lives in your user folder**, not in the downloaded one:
`~/Library/Application Support/optjournal` on a Mac, `%APPDATA%\optjournal` on
Windows. Every version finds it there, so you can delete old downloads. If you
used a download from before this existed, the page offers to bring that journal
across: click **Use this journal**.

## Requirements (for the terminal)

- Windows 10/11, macOS or Linux, Python 3.12+
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

The commands are identical in PowerShell, Command Prompt and Unix shells because
`uv run` selects the virtual environment's platform-specific executable. On
Windows, the app uses Windows Credential Manager for the Flex token and includes
the timezone data Python needs for US market hours.

`setup` asks for two things: the Flex token, which it writes to the OS keyring
without echoing it, and the Flex Query ID, which it stores in
`.optjournal.json` beside the database. It then spends one request confirming
both work against IBKR — a plausible-looking token and a plausible-looking id
still fail together, and finding that out later from a scheduled sync is worse.
`--no-verify` skips that check; `--query-id` and `--token-stdin` make it
scriptable.

A token expires, and replacing it does not need a terminal: Settings in the page
takes a new one and writes it to the same keyring entry. The page can replace the
token but never read it back, so it reports presence and never a value — and
since only a real fetch can tell whether IBKR still accepts what it was given,
saving one starts a fetch by itself.

On macOS the keychain may ask whether optjournal (it can appear as Python) may
read the token. Choose **Always Allow**. **Allow** grants a single read, and
optjournal asks the keychain afresh for every read rather than reuse an answer
given before the read began, which is what keeps a token replaced elsewhere from
being read stale. With **Allow**, every Check and every scheduled sync asks again.

No IBKR account handy? `uv run optjournal demo` writes synthetic data to
`demo/`, and `uv run optjournal serve --demo` browses it.

## Updating

```bash
uv run optjournal update            # fast-forward, resolve deps, migrate
uv run optjournal update --check    # report what is new, change nothing
```

It refuses rather than merges: fast-forward only, and uncommitted changes to
tracked files stop it before it touches the network. Untracked files (such as the
`.DS_Store` Finder leaves) do not, since git itself refuses a pull that would
overwrite one. It never runs `git stash`. It installs exactly the `uv.lock` it
pulled (`uv sync --locked`, which also refuses a lock that does not match its
`pyproject.toml`), then migrates the journal with the new code, so a migration
that fails is reported by `update` rather than later by `serve`. While
optjournal is running it leaves the migration to the restart it asks for,
because the running copy is still the old code.

It does not restart a running `serve`. The server re-reads the page on every
request but loads its Python once at startup, so a code update needs a restart,
which the command tells you about.

## Commands

`optjournal --help` lists everything. The ones you will use:

| command | what it does |
|---|---|
| `setup` | store the Flex token and query id, then verify them |
| `sync` | fetch the newest statement, ingest it, report what is new |
| `confirms` | fetch today's fills from a Trade Confirmation query (same session, not next-day) |
| `serve` | the dashboard, plus the in-process scheduler |
| `update` | fast-forward a git clone to the latest published commit |
| `prepare` | move the journal to its home folder (the Start file runs this) |
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
| Flex token | OS keyring | `optjournal setup`, or Settings in the page |
| Flex Query ID | `.optjournal.json` | `optjournal setup`, or Settings in the page |
| Confirms Query ID | `.optjournal.json` | Settings in the page, or `$OPTJOURNAL_CONFIRM_QUERY_ID`. Optional |
| Database, archive, settings | the journal's home (below) | `--db` / `--archive`, `$OPTJOURNAL_HOME` |

The query id also reads from `--query-id` and `$OPTJOURNAL_QUERY_ID`, in that
order of precedence, so an existing install or a cron keeps working unchanged.
A running `serve` reads the saved id per request and per job run, so a new one
saved in Settings is used by the next sync without a restart (unless a flag or
the variable outranks it, which Settings then says).

The journal's home is `$OPTJOURNAL_HOME` if set. Otherwise it is the code folder
IF that already holds a journal, which is every git clone set up before homes
existed, so a developer's checkout and its launchd agent keep working untouched.
Otherwise it is the per-user folder, which is where a download's journal lives.

## Publishing an update

Friends on a downloaded ZIP update from GitHub Releases, not from `main`, so you
choose when they get a version:

1. Bump `version` in `pyproject.toml` (the one place it is written), run `uv lock`,
   and commit both files, then push. `uv.lock` records the version too, and the
   test suite fails on a commit whose lock does not match its `pyproject.toml`.
2. Publish a release tagged with that version:
   `gh release create v0.2.0 --title "0.2.0" --notes "What changed"`.

Their page offers it on its next start (it checks at most every six hours). The
release notes are what the banner shows under **What's new**. A release whose
code is not the version its tag names is refused, and so is one that would
contain any journal file.

## Wins and losses

Net P&L and commission are what IBKR booked on each fill, on the fill's trade
date, so every month reconciles with the statement and a partial close counts
the day it fills. The scoreboard counts each closed contract round trip as one
outcome, in the month of its last closing fill. A round trip closed by two
partial fills is one outcome, not two, and a contract still held is no outcome
yet. Each leg of a strangle, and each leg of a roll, is its own win or loss,
which is what a broker trade log shows, so the scoreboard reconciles against one
directly.

The Trades tab groups those contracts into positions, so a roll or a multi-leg
structure is one card there.

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
| [docs/trade-confirmations.md](docs/trade-confirmations.md) | same-session fills: the confirm query's real attribute names, the FX estimate they force, and how the two queries are ranked |
| [docs/contributing.md](docs/contributing.md) | adding functionality, data safety, local development |
| `PLAN.md`, `SCHEDULER_PLAN.md` | open work, with the measured scope for each step |

## Development

```bash
uv run pytest -q                      # deterministic suite
uv run ruff check src tests cron      # lint
uv run mypy                           # static types
uv run optjournal mutate              # mutation testing: see docs/testing.md
```

Normal tests use the redacted statements in `tests/fixtures/statements/`.
Private statements in `raw/` are optional acceptance data and never determine
whether a fresh checkout or CI is green.
