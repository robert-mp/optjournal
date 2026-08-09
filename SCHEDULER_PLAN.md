# Scheduling plan: move the clock into the application, and make the page the console

Working document, not a design doc. Every number below was measured against this
repo, and the command or the `file:line` is given so a reader can re-check rather
than trust it. Steps are ordered so each is shippable alone, green on its own, and
none requires the next.

Baseline: **769 passed, 2 skipped, ruff clean** at `d43ba26`.

## Revisions after review

The first draft of this document was reviewed against the live machine, and three
things changed. They are recorded here rather than silently edited in, because two
of them are corrections to *this file*.

**1. Step 1 is DONE, but not as written.** `d43ba26`. The draft proposed an early
return from `migrate` when the version stamp and the five views looked current.
`tests/test_db.py` refused it, correctly: three tests require that merely OPENING a
journal heals it (an ALTER for a column added after ship, two row-level backfills),
and an early return disabled all of that silently. The draft even anticipated the
trap -- "a journal stamped at the current version but missing a *column* would be
skipped" -- and the suite caught it anyway. The shipped fix is narrower and lands
where the damage was: `_stale_views` compares each view's STORED sql to the shipped
sql, and only a mismatch is dropped. Steady state drops nothing, so a reader has
nothing to miss, and every healing step still runs. 4,941 failures -> 0.

**2. Step 2 (the raw/ backup) is CUT.** The draft argued this was second-most
urgent. It is not, and the reasoning was wrong in a way worth stating: it treated
"`raw/` is the provenance root" as "`raw/` is irreplaceable". Measured -- the daily
Flex query is **`Last30CalendarDays`**, a rolling window, and two archived
statements are full-year pulls (`20250801->20260731`, `20250810->20260803`). So
every one of the 168 trades, 101 cash rows, 63 snapshots, 15 securities and 261 NAV
rows comes back from IBKR for the cost of a request. Committing that XML to a git
repo protects the cheapest artefact in the project.

What the draft got RIGHT is buried in the same paragraph: the unrecoverable data is
**not in `raw/` at all**, so a `raw/` backup would never have saved it either.
`price_bars` 1,816 rows, of which **329 are hourly** spanning 2026-07-20 to
2026-08-08, plus `market_events` 99 and `watchlist` 6. The README already says why
those are different in kind: "an option's intraday series exists only while its
session is running, so it cannot be backfilled at any price". A `git add raw/*.xml`
does nothing for any of it.

So the 100 lines of `backup.py` become **one `VACUUM INTO` on the sync path**, as a
footnote to step 5 rather than a step. It captures the whole journal, perishable
bars included, with one stdlib call, no git, and no configurable repo root to point
at the wrong repository. The genuine finding underneath the draft's step 2 survives
and is promoted: `web._do_sync` and `cli.cmd_sync` do different things, and the plan
makes the web path primary. Two paths that claim to be the same and are not is this
project's recurring bug shape.

**3. Where the app lives is now an explicit step.** The draft never said, and the
answer matters more than it looks. See the next section.

## The one-paragraph answer

`optjournal serve` becomes the application: one long-lived process holding a
**60-second wall-clock reconciler thread** and a **single job worker**, with every
job's schedule declared as code in one module, every run recorded in one
`job_runs` table that `/api/state` already carries to the page, and one
`POST /api/jobs/run` that the page uses to trigger anything by hand. launchd
supervises the *process* (`RunAtLoad` + `KeepAlive`), never the jobs. The
scheduler lives inside `serve` for one measured reason and not for elegance: the
IBKR fetch cooldown is a check-then-act guard on a hard lockout budget, and one
in-process scheduler beside the request handlers means the scheduled sync and the
browser's Sync button are two threads in one process rather than two blind
processes. `bars-audit` stops being a job at all and becomes a field computed on
every page load (measured **0.76 ms**), which severs the coupling that let the
watchdog and the thing it watched stop together.

**What this explicitly does NOT do**, because three critics independently said the
machinery was outgrowing the job:

- **No pause/resume/ack/edit-schedule endpoints.** Two POSTs total
  (`/api/jobs/run`, `/api/jobs/cancel`). Schedules are code, so there is nothing
  to disable over HTTP. On a server with **no authentication**, every write
  endpoint widens what any local process able to forge an `Origin` header may do,
  and "silently disable the sync" is a state change that outlives the request and
  whose effect is invisible until data is already missing.
- **No 1.5 s `/api/state` poller.** Polling full state costs 142 SQL statements
  and ~165 KB per tick against the journal the running job is writing. Progress
  polls a dedicated cheap endpoint instead.
- **No second CLI subcommand, no `notify.py`, no `osascript` push, no
  `schedule.py`/`ledger.py`/`runner.py` split.** One module, `jobs.py`.
- **No croniter, no APScheduler, no task queue, no web framework, no Docker, no JS
  toolchain.** Runtime dependencies stay at exactly two (`py-ibkr`, `keyring`).
- **No `backup`/`snapshot` as scheduled jobs.** They belong in the shared sync
  path, beside the thing they protect.
- **No cancel in v1.** See [What is not worth doing](#what-is-not-worth-doing).

---

## Where the app lives

The draft did not answer this and it is load-bearing, because the answer today is:
**inside the thing being migrated away from.**

```
/Users/robrtmar/.meshclaw/workspace/optjournal
```

Code, `journal.db`, `raw/`, `demo/` and `.venv` all sit in MeshClaw's workspace
directory. Three cron implementations hardcode
`Path.home() / ".meshclaw" / "workspace" / "optjournal"`, and the editable install
pins `/Users/robrtmar/.meshclaw/workspace/optjournal/src` in a `.pth`.

**The good news, measured: `src/` has ZERO references to meshclaw.** All 13 live in
`cron/` (7), `tests/` (2), and the README (2) -- and `cron/` is deleted by step 8
anyway. `config.ROOT` is `Path(__file__).resolve().parent.parent.parent`, so the
database and the archive follow the code wherever it goes. `journal.db`, `raw/` and
`demo/` are already gitignored, so code and data are separable without a migration.

So this is a MOVE, not a refactor. The only thing that breaks is the venv's
absolute `.pth`, and `uv sync` regenerates it.

### The recommendation: `~/optjournal`, and keep data beside code

Move the repo to `~/optjournal` (or anywhere outside `~/.meshclaw`). Do NOT split
data into `~/Library/Application Support/optjournal` or `~/.local/share`:

* `config.py`'s own comment states the rule -- "data next to code keeps the whole
  journal one directory to back up" -- and that is still right for a one-user tool.
* An XDG-style split trades one directory for two, and buys nothing here: there is
  no package manager installing this, no multi-user case, no read-only prefix.
* It would make the `VACUUM INTO` snapshot land somewhere different from the thing
  it snapshots, which is how a backup gets forgotten.

**Why the move is not merely tidiness.** While the app lives under
`~/.meshclaw/workspace`, retiring MeshClaw means either leaving the journal in the
directory of a retired tool, or moving it later under a launchd plist that has the
old path baked in. Doing it BEFORE step 7 means the plist is written once against
the final location. It also removes the last reason anyone would think MeshClaw's
lifecycle owns the journal's data.

**Cost, measured rather than estimated:** 13 grep hits, of which 7 die with
`cron/`; one `uv sync`; one `git mv`-equivalent (`mv` plus re-running `uv sync`);
and the three MeshClaw shims need their `IMPL` path updated, which is one line each
and is exactly the indirection the shim pattern was built to make cheap.

**Where this lands in the sequence: a new step 2**, replacing the cut backup step.
It is small, it is reversible, and everything after it -- the plist, the log paths,
the snapshot destination -- wants to be written against the final location once.

---

## Two robustness bugs that exist TODAY

These are live defects, not future work. One is fixed; one is not; a third that
the briefing listed as fixed is **only half fixed**, and that is the most
important finding in this document.

### 1. The cross-process fetch cooldown race — FIXED at `03ab000`, do not regress

`flex.fetch` (flex.py:369) now wraps the whole sequence in
`with locked(archive_dir / FETCH_LOCK)` and delegates to `_fetch_locked`, so the
cooldown check (flex.py:388), the keyring read, the download and the stamp
(flex.py:412) are one critical section across *processes*. Verified by reading the
code end to end, not by trusting the commit message.

**The obligation this plan carries is negative**: the sync job must *call*
`flex.fetch`, never reimplement the check-download-record sequence. The commit
message for `03ab000` records that an earlier cooldown test reimplemented that
sequence with its own lock and therefore passed against a completely unguarded
fetch. That is exactly the trap a scheduler rewrite walks into, so it is
[asserted in step 5](#step-5), not left as a comment.

### 2. `busy_timeout` — FIXED at `03ab000`

`db.BUSY_TIMEOUT_MS = 15_000` (db.py:456) and `connect` sets it explicitly
(db.py:468). Measured on a fresh connection: `busy_timeout = 15000`. The briefing
said this was the unset 5000 ms default; it is not, any more.

But 15 s is now a **blast radius**, not only a safety margin — see the wedge in
step 1. Every writer that collides with a stuck transaction waits the full 15 s
before failing, and I measured exactly that: **15.55 s**.

### 3. The migrate view-drop race — **NOT FIXED. Still live. This is step 1.**

Design 1 opened by asserting this was "DONE ... does not need to re-solve them"
and excluded it from its plan. **That is false, and I reproduced it.** The lock
made concurrent migration *correct*; it did not stop a migrating request from
dropping views under a request that is querying them, because `migrate` releases
the lock when it returns (db.py:707-710) and the caller then runs its SELECTs
unprotected.

```
3 readers + 2 migrators on a copy of the live journal, 6 seconds:
  ok: 68   fail: 160
    103  no such table: trade_orders
     57  no such table: trade_legs
```

`_migrate_unlocked` unconditionally does `DROP VIEW IF EXISTS` x5 (db.py:715-716)
then `executescript(_SCHEMA)`. DDL autocommits, so the drop is visible to every
other connection, and `open_journal` migrates on every request (db.py:766).
`web.py:792` turns the result into `HTTP 500 database not readable: ...`.

I disagree with Design 1 and side with Judge 3 and both over-engineering critics
here. The reason is not only that the bug is real: it is that this plan's own
progress polling multiplies journal opens, so shipping the poller first would turn
an occasional 500 into a constant one, and it would look like the new feature
caused it.

**Ordering matters and is counter-intuitive.** Do not raise `busy_timeout`
further, and do not add the poller, before the view refresh is non-destructive. A
longer wait makes the `no such table` window *more* observable, so the fix would
look like the cause.

---

## The sequence

### Step 1 — Make the read path stop writing DDL — **DONE at `d43ba26`**

**Highest value, lowest risk, and it is a bug fix that stands alone with no
scheduler anywhere near it.**

**What shipped, and why it is not what this step proposed.** The proposal below was
an early return when the schema looked current. `tests/test_db.py` rejected it:
`migrate` HEALS a journal whose version stamp is already right, via
`_ADDED_COLUMNS` ALTERs and two row-level backfills, and three tests assert that
merely opening a journal repairs it. An early return disabled all of it silently.

The shipped fix narrows the DROP instead of skipping the migration. `_stale_views`
compares each view's stored `sql` in `sqlite_master` against the definition parsed
out of `_SCHEMA`, and only a mismatch is dropped -- so the steady state drops
nothing, a concurrent reader has nothing to miss, and every healing step still runs.
Existence alone would not have done: `CREATE VIEW IF NOT EXISTS` never updates, so a
view can exist and be WRONG, which `test_db` pins by planting a garbage definition.

Result, on the reader-vs-migrator shape that exposed the bug: **4,941 failures ->
0**, with read throughput up from 60k to 89k in the same two seconds. Over HTTP,
16 concurrent `/api/state` went from 10 failures in 160 to 0.

Two bugs found while writing it, both by running it: the `_SCHEMA` view parser
initially found 1 view of 5 (the others are preceded by `--` comments) and a
`.get(name, stored[name])` fallback made that invisible, so view refreshing would
have been silently dead with a green suite; and `schema_is_current` raised
`no such table: schema_version` on a brand-new file. An unparseable view now raises.

<details><summary>The original proposal, kept for the reasoning</summary>

**What changes.** An early return at the top of `db._migrate_unlocked`: if
`MAX(version) == SCHEMA_VERSION` **and** all five `_VIEWS` are present in
`sqlite_master`, return `SCHEMA_VERSION` without touching anything. Keep the
`flock` for the genuine-migration case.

Measured, monkeypatched in so the comparison is against real code:

| | throughput | per call |
|---|---|---|
| today | 68 ok / **160 fail** | 1.62 ms |
| with fast path | 274 ok / **0 fail** | **5.3 µs** (304x cheaper) |

**Why both conditions.** The version alone is not sufficient. `db.py`'s own
comment at the top of `migrate` (db.py:700-704) explains that the version stamp is
not the only thing the migration writes — `_ADDED_COLUMNS`, the rekeys and the
backfills all guard themselves, so "needed" is not a single comparison. Requiring
the five views to be present as well is what makes the early return safe: the one
piece of work that is *not* self-guarding is exactly the view rebuild, and the
check confirms it has already happened. This also closes the crash-window bug the
ops survey found separately (a process killed mid-migrate leaves a viewless
journal that `PRAGMA integrity_check` reports as fine).

**Test that proves it.** A real two-writer test — `multiprocessing` or
`threading`, readers looping `SELECT COUNT(*) FROM trade_legs` while migrators
loop `open_journal`, asserting zero `OperationalError`. This is the first
concurrency test in the suite: `grep -rl 'threading|multiprocessing|Barrier'
tests/` currently returns nothing, and every finding in this document was
invisible to a green 766-test suite. `tests/test_locks.py` (352 lines) is the
pattern to follow.

**What could go wrong.** The early return skips a guard that a merely-opened
journal still needs. Mitigated by the views condition, and by keeping the full
path for any journal not already at `SCHEMA_VERSION`. A journal stamped at the
current version but missing a *column* would be skipped — so the test must include
"stamp the version, drop a column, assert migrate still repairs it".

*(That last sentence is what the suite went on to enforce, and what killed the
proposal. The prediction was right and the mitigation was not enough: the views
condition does not cover a missing column, and `_ADDED_COLUMNS` spans six tables.)*

</details>

### Step 2 — Move the app out of `~/.meshclaw/workspace` — **DONE**

**Small, reversible, and it must precede step 7 so the launchd plist is written
once against the final path.**

**What actually happened, because two things differed from the plan.**

**`uv sync` was NOT enough.** The plan said regenerating the `.pth` would do it, and
it did rewrite the path correctly -- `uv run python -c "import optjournal"` worked.
But `uv run pytest` still raised `ModuleNotFoundError: No module named 'optjournal'`
while `.venv/bin/python -m pytest` passed 15/15 on the same tree. Neither
`uv sync`, nor `uv sync --reinstall-package optjournal`, nor appending the missing
trailing newline to the `.pth` fixed it. `rm -rf .venv && uv sync` did. So a venv
CREATED at the old path carries state that survives a resync, and the honest
instruction is REBUILD it rather than repoint it. Recorded as observed behaviour
rather than diagnosed further: the fix is cheap and the failure is loud. Side effect
worth knowing -- the rebuild installed pytest 8.4.2, which is what `uv.lock` pins;
the old venv had drifted to 9.0.2.

**The workspace backup repo is now broken, and that is accepted.**
`~/.meshclaw/workspace` is still a git repo tracking nine XMLs at
`optjournal/raw/`, and those files moved out from under it, so `git status` there
shows nine deletions. The history still holds them, so nothing is lost -- but it
cannot back up a new statement. NOT repointed, deliberately: see
[Revisions](#revisions-after-review). Every row in `raw/` is refetchable from a
`Last30CalendarDays` query, and the data that genuinely cannot be recovered was
never in `raw/` for this to protect. `cron/optjournal_sync.py` records that in
place, where the next reader of `WORKSPACE` will find it.

Also removed the three git worktrees first (`delta-holding-only`, `rebrand-logo`,
`replay-event-cards`) -- all merged into main with nothing ahead and one stray
screenshot between them. Their `.git` files hold absolute gitdir paths, so a move
would have broken all three; `git worktree repair` exists, but repairing worktrees
whose work is already merged is effort spent on nothing.

Verified: `config.ROOT` reports `/Users/robrtmar/optjournal`, schema still v8, every
row count unchanged and the trades content hash identical (`c70a06e6c8f281a4`), 10
XMLs present, 803 tests pass, ruff clean, and all three MeshClaw shims load and
resolve their `IMPL` to the new path.

**What changes.** `mv ~/.meshclaw/workspace/optjournal ~/optjournal`, then
`uv sync` to regenerate the editable-install `.pth` (which pins an absolute path).
Update `IMPL` in the three MeshClaw shims -- one line each, and precisely the
indirection the shim pattern exists to make cheap. Update `PROJECT`/`WORKSPACE` in
`cron/*.py`, `DEPLOYED` in `tests/test_cron.py`, and the two README mentions.

**Why it is a move and not a refactor.** `src/` has zero references to meshclaw;
all 13 are in `cron/` (7, deleted by step 8), `tests/` (2), README (2).
`config.ROOT` is derived from `config.py`'s own location, so the database and
archive follow the code with no migration. Verified.

**Data stays beside code.** No XDG split -- see [Where the app
lives](#where-the-app-lives). One directory to back up is the property that makes
the `VACUUM INTO` snapshot land next to the thing it snapshots.

**Test.** Nothing new: `tests/test_cron.py` already asserts every deployed shim
resolves to real code, so a shim left pointing at the old path fails there. That is
the test earning its keep rather than a test written for the move.

**What could go wrong.** The old path still exists with a stale `.venv`, and a
forgotten shell or a `launchctl` entry keeps using it -- two live journals, one of
them silently not being updated. Mitigation: move rather than copy, and after
`uv sync` assert `optjournal.config.ROOT` reports the new location before deleting
anything.

<details><summary>CUT: the original step 2, the raw-statement backup</summary>

**Cut after review. The reasoning is in [Revisions](#revisions-after-review):
`Last30CalendarDays` makes every row in `raw/` refetchable, and the data that is
genuinely unrecoverable is not in `raw/` at all. What survives is one
`VACUUM INTO` on the sync path, folded into step 5, plus the real finding that
`web._do_sync` and `cli.cmd_sync` diverge.**

**Second because it is durability of the one artefact that cannot be regenerated,
and the web UI already bypasses it.**

**What changes.** New `src/optjournal/backup.py`: `_commit_raw_backup` lifted out
of `cron/optjournal_sync.py:196-239` with its behaviour intact — force-adds every
`raw/*.xml` (catch-up semantics, so a run that fetched nothing still sweeps in a
previously-uncommitted file), enumerates files individually so `-f` cannot drag in
`.fetch-state.json`, and pins the commit pathspec so unrelated staged work stays
out. The hardcoded `~/.meshclaw/workspace` repo root becomes a `config.py` value,
and the "`-f` beats a nested `.gitignore`" reasoning gets restated in its new
home. Called from **both** `web._do_sync` and `cli.cmd_sync`.

Also here, because it touches the same file for the same reason: `VACUUM INTO` a
timestamped snapshot beside the raw commit.

**Why it cannot wait.** Measured: `raw/` holds **10** XMLs; the backup repo tracks
**9**; the untracked one does not even appear in `git status` because a nested
`.gitignore` hides it, which is why `-f` is load-bearing. `grep -rn 'git add|
_commit_raw|backup' src/` finds nothing — the logic exists **only** in the cron,
and `web._do_sync` has no backup step. The user's explicit goal makes the web UI
the way syncs happen, so every sync would bypass the only backup logic there is.
Shipping the scheduler first would mean "more production ready" measurably
*reduces* durability.

And the README's "every row is rebuildable from `raw/`" is already false. Measured
on the live journal: `price_bars` **1816**, `market_events` **99**, `watchlist`
**6** — none rebuildable, **105** of those bars being perishable 1 h option bars
that the README itself says cannot be refetched at any price. The backup repo has
**no remote** (`git remote -v` → 0 lines) and Time Machine has no destination. One
stdlib call closes it.

**Test.** `backup.py` joins `LEAVES` in `tests/test_layering.py:41` (it is
`subprocess` + `pathlib` only). A test that a sync through *both* paths leaves the
new XML tracked, and one that `-f` does not stage `.fetch-state.json`.

**What could go wrong.** `git add -f` in a repo whose root is now configurable
could commit into the wrong repository. The pinned pathspec limits it, and the
test should assert the commit touches only `raw/*.xml`.

*(Note how this risk reads in hindsight: the mitigation for "might commit to the
wrong repo" was a pathspec and a test, when the correct answer was that the commit
buys nothing. The 1,816 `price_bars` rows in the paragraph above are the actual
asset, and no amount of care around `git add` would have protected one of them.)*

</details>

### Step 3 — A regression net for the 21 delivery decisions, against the existing cron files — **DONE at `344fafe`**

**Zero behaviour change. This is the step that makes the rewrite safe.**

**What changes.** Only `tests/test_cron.py`. 17 of the 21 delivery decisions are
currently unverified: every `sync_cron.`/`bars_cron.` reference in that file is
`_describe`, `FETCH_TIMEOUT_S`, or an `EXIT_*` integer comparison. `sync()`,
`live()`, `daily()`, `audit()` and `_collect()` are **never called**, and
`test_the_bars_cron_maps_every_outcome_to_a_delivery` would pass unchanged if
every branch in `_collect` were deleted.

The `market` tests (test_cron.py:267-398) are a proven template: monkeypatch
`subprocess.run` to return a chosen `CompletedProcess`, assert which sentinel is
raised. Apply it to sync's 13 branches and bars' 7.

**Why before the scheduler.** "It passes 766 tests" currently offers no protection
whatever for the policy being rewritten. This is doable today, against unchanged
files, and it is the only thing standing between a rewrite and silently inverting
a decision.

**What could go wrong.** These tests are deleted in step 8 along with `cron/`. That
is not waste: they are the oracle the new `jobs.py` tests are written against, and
they are the reason a reviewer can believe the mapping survived.

### Step 4 — The ledger and the status surface, read-only, while MeshClaw still runs — **DONE**

**Ship the status surface BEFORE the schedule moves.** If the app ships a
scheduler before it ships a status surface, the system gets quieter and less
trustworthy in the same change. All three judges agreed on this ordering; it is
the one point of unanimity.

**What changes.** `SCHEMA_VERSION` 7 → 8, purely additive:

```sql
CREATE TABLE job_state (          -- the SCHEDULING ANCHOR. O(jobs) forever.
  job            TEXT PRIMARY KEY,
  last_fired_for INTEGER,         -- UTC epoch of the newest instant claimed
  last_status    TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  heartbeat_at   INTEGER          -- written by the tick loop itself
);

CREATE TABLE job_runs (           -- bounded, disposable history
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  job         TEXT NOT NULL,
  fired_for   INTEGER,            -- NULL for a manual run from the page
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  status      TEXT NOT NULL,      -- running|ok|nothing|missed|failed|interrupted
  detail      TEXT,
  done        INTEGER NOT NULL DEFAULT 0,
  total       INTEGER NOT NULL DEFAULT 0,
  note        TEXT,
  slept       INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX job_runs_fired ON job_runs(job, fired_for)
  WHERE fired_for IS NOT NULL;
```

**Two tables, and the reason is load-bearing.** Pruning history must not be able
to delete the catch-up anchor. Fold them into one and a retention pass trimming
`job_runs` can remove the row that says when `sync` last fired, at which point the
reconciler either replays a year of instants or silently loses the schedule.
Design 1 derived due-ness from the table it pruned; Design 2 caught this and I am
taking its shape. The partial index (`WHERE fired_for IS NOT NULL`) is what keeps
manual run-now presses unconstrained, so the button is never blocked by the
schedule.

`state["jobs"]` in `build_state`, with per job: last run, last status, detail,
`next_due_at`, `stale`, plus a top-level `scheduler` block carrying heartbeat age.
`state["audit"]` computed fresh from `bars.audit_perishable()` — measured
**0.76 ms**. A read-only Jobs strip in `page.html`. Also here: `BEGIN DEFERRED`
around `build_state`'s connection block, because it issues 142 statements with no
read transaction, so a writer committing mid-build produces a payload whose panels
disagree — which web.py:31-33 promises can never happen.

The existing MeshClaw crons get a ~10-line helper to write a `job_runs` row, so
for a few days the current scheduler keeps running and the page finally shows what
it did. That validates the ledger shape against real runs before anything depends
on it.

**Three staleness signals, because "did it run at all" is a different question
from "did it succeed"**, and it is the question that actually failed.

RE-MEASURED at review, because the draft's figures here were wrong and the
conclusion survives anyway. The draft said 08-08 had no hourly OPT bars; it has 20.
The honest evidence is `price_bars.fetched_at`, which records when a row was
WRITTEN rather than which session it covers:

```
written 2026-08-08T23   1d: 1481   1h: 299   <- a MANUAL run during the review
written 2026-08-07T14   1h: 2
written 2026-08-06T20   1h: 28
written 2026-08-06T11   1d: 1
```

So the bars jobs wrote 28 hourly rows on 08-06, two on 08-07, and nothing after
until a human ran the command by hand. And `~/.meshclaw/crons.json` at that moment:

```
optjournal-bars-live    last_status: ok      consecutive_failures: 0
optjournal-bars-daily   last_status: ok      consecutive_failures: 0
optjournal-bars-audit   last_status: ok      consecutive_failures: 0
optjournal-daily-sync   last_status: error   consecutive_failures: 1
```

**Three jobs reporting `ok` while collecting nothing, including the audit job whose
entire purpose is to notice that.** The sync's own `last_error` names the cause and
proves the typed-exception argument in step 5: a locked keychain
(`keyring.backends.macOS ... find_generic_password`), which is not `TokenMissing`,
so it never mapped to exit 2 and arrived as a traceback that reached nobody.

That is the failure this step exists for. A green `last_status` is not evidence of
anything, and no amount of backup would have recovered the sessions it hid.

1. **`heartbeat_at`, written by the tick loop itself** (grafted from Design 3, and
   I side with all three judges against Design 1 here — Design 1 derives staleness
   from job outcomes, which cannot distinguish "no jobs were due" from "the thread
   is dead"). Red banner when older than 5 minutes.
2. **`next_due_at` + `stale` per job.** A row can be green-on-outcome and
   red-on-freshness at once, which is exactly the state the system was in.
3. **`state["audit"]` on page load, unconditionally.** So opening the page answers
   "did yesterday land?" even if the scheduler thread is dead.

**Test.** `job_runs`/`job_state` migration is additive and idempotent on a v7
journal. New `@typedef` + `@payload` rows in `page.html` for `JobRow` and
`AuditBadge`, both sampled from real `/api/state` so they go in `_shape_samples`
rather than joining `_UNSAMPLED` (tests/test_web.py:253). A `BEGIN DEFERRED` test
that a mid-build commit cannot change what the payload reports.

**What could go wrong.** `SCHEMA_VERSION` 7 → 8 activates the concurrent-migration
hazard exactly once, on a journal not yet at the new version — where step 1's fast
path cannot help, because the version is stale by definition. The `flock` around
`migrate` is what makes that safe, which is *why the bump lands after step 1 and
not before*. The read transaction must stay scoped to `build_state`'s ~14 ms body:
a pinned reader blocks WAL checkpointing.

#### What step 4 actually shipped, and the four places this plan was wrong

Commits `0aa3d92` (ledger), `075bb0d` (the `ok` hole), `bbb983b` (the strip).

**1. The crons could not write the ledger, so the CLI does.** The plan said "the
existing MeshClaw crons get a ~10-line helper to write a `job_runs` row". They
cannot: a cron runs under MeshClaw's own interpreter, which has no `py_ibkr`
(verified — `import py_ibkr` there is a `ModuleNotFoundError`), which is the whole
reason they shell out to the CLI. So `record_run` is called by the CLI commands the
crons already invoke, and the cron files stayed untouched. Better anyway: the ledger
records what the WORK did rather than what the cron's delivery policy decided, and
the delivery decision was already visible in Slack.

**2. `audit.ok` IS GREEN IN A TOTAL BLACKOUT.** The plan had the page render
`state["audit"]` and said nothing about which field. `ok` is
`not market_traded or not missing`, and `market_traded` is answered by "does any
underlying have hourly bars that day" — an oracle that fails the same way as the
thing it certifies. Reproduced on three copies of the real journal:

```
healthy           traded=True   covered=5  missing=0  ok=True
option poll dead  traded=True   covered=0  missing=5  ok=False   <- caught
TOTAL blackout    traded=False  covered=0  missing=0  ok=True    <- MISSED
```

The same watchdog-and-watched-stop-together shape that moved this audit out of a
cron, one level down. `witnesses` and `blackout` travel now and the page reads
those; `ok` stays for `bars --audit` and the cron policy.

**3. `blackout` could not tell a stopped collector from one never started.**
Measured, not reasoned about: `price_bars` emptied out of a copy of the real journal
versus a fresh journal gave **byte-identical** payloads (`market_traded` False,
`witnesses` 0, `blackout` True, `ok` True). So the demo journal — and every real
journal before its first `optjournal bars` — showed a red collection alarm for the
absence of something that had never been there. Added `ever_collected`. Note the
shape of the mistake: the heartbeat already drew exactly this distinction with
`ever_ran`, and the audit half simply did not.

**4. `next_due_at`/`stale` per job did NOT ship, deliberately.** Nothing schedules
anything yet, so there is no next instant to compute — a due time derived from a
registry that does not exist would be a guess rendered as a fact. It lands in step 6
with the reconciler, the first code with an opinion about when a job should fire. The
heartbeat covers the question that actually failed.

**Also deferred to step 5: `BEGIN DEFERRED` around `build_state`.** A torn-payload
probe could not reproduce the defect — 0 of 5 attempts with a writer committing
mid-build, against a control that also read 0 of 5. Worth doing on the argument, not
worth claiming a fix for a failure that would not reproduce.

**What building the strip cost, as a warning for step 5.** Two more instances of the
dead-declaration shape, both found only by a browser: `<b class="pos">` inside a
`.pill` lost its tint to `.pill b` (specificity 0,1,1 beats 0,1,0 — four figures
affected), and `.jrow:first-child{border-top:0}` matched nothing because the card's
first child is its header. Both were well-formed CSS under a selector that could not
reach the element, with the whole suite green. Three guards now cover the class in
`tests/test_web.py`: every class the page names has rules, every rule is reachable,
and no rule sets a layout property its display mode cannot use.

### Step 5 — `jobs.py`: the registry and the runner, manual only, no thread yet — **DONE**

<a id="step-5"></a>

**What changes.** One new module, `src/optjournal/jobs.py`:

- **`JOBS: tuple[Job, ...]`** — frozen dataclasses: `name`, `run` callable,
  schedule (minute/hour/weekdays triple plus a `zoneinfo` zone — three integers do
  not need a cron parser), `catchup` (a three-valued enum, see step 6),
  `timeout_s`, `spends_broker_request: bool`. Four entries: `sync`, `bars_daily`,
  `bars_live`, `market`.
- **`run_job(...)`** — takes a per-job `flock` at `raw/jobs/<name>.lock` with
  `timeout_s=0` (non-blocking), writes a **committed `running` row before starting
  work**, calls the work directly, writes the outcome, prunes to 200 rows per job
  while unconditionally keeping the newest `fired_for` row.

**The registry is code, and that is the fix for a specific failure.**
`crons.json` holds 7 jobs and **none** is `optjournal-market` (re-verified at
review: `grep -c optjournal-market ~/.meshclaw/crons.json` → 0). So 143 lines of
reviewed, tested, README-documented calendar policy have **never run on a
schedule** — corroborated in the data: `market_events` holds 99 rows all stamped
`fetched_at 2026-08-08`, a single fetch from the web UI during the review, not the
daily accumulation its docstring's argument depends on. Registration was an
unversioned, hand-typed side channel that no test could see. In `jobs.py`,
"exists" and "registered" become one fact. Treat `optjournal_market.py` as a spec
for a job being switched on, not as working code to port faithfully.

**Jobs call functions, not the CLI.** `web -> jobs` keeps the import graph acyclic
(`cli` imports `web`, so `web -> jobs -> cli` would close a cycle that
`tests/test_layering.py:105` fails on). This also deletes ~6 exit-code branches
per job and lets the runner match on typed exceptions. That is not cosmetic: the
2026-08-07 sync failure was a `KeyringLocked`, which is **not** `TokenMissing`
(flex.py:125), so it never mapped to exit 2, arrived as an uncaught traceback →
exit 1 → a generic `RuntimeError` that reached nobody. And today `bars` treats a
DB-locked exit 1 as a routine `Skip` and would silently retry a lock seven times a
session.

**One shared sync path, and one `VACUUM INTO`. (Absorbed from the cut step 2.)**
`web._do_sync` and `cli.cmd_sync` currently do different things, and this plan makes
the web path the primary one -- two paths claiming to be the same and diverging is
the bug shape this project keeps finding. The sync job calls one function, and both
entry points call it too.

That function ends with `VACUUM INTO` a timestamped file beside the journal. One
stdlib call, no git, no configurable repo root to aim at the wrong repository, and
unlike a `raw/` commit it captures **the data that cannot be refetched**: 1,816
`price_bars` rows of which 329 are hourly option bars the README says "cannot be
backfilled at any price", plus `market_events` and `watchlist`. Keep the last N and
delete older ones, so it cannot grow without bound.

Deliberately NOT a scheduled job of its own: it belongs beside the write it
protects, so it cannot be the thing that silently stopped running. That is the same
argument that turns `bars-audit` from a job into a page-load field.

**Endpoints.** `POST /api/jobs/run` `{job}` → 202 `{ok, run_id}` | 409
`{kind:"busy", run_id}` | 400 `{kind:"unknown"}`. `GET /api/jobs/run?id=<id>` →
the status row, ~1 ms, no DB write, **no migrate**. Target in the request *body*,
matching the `/api/watchlist` idiom, so `do_POST` keeps exact path comparisons —
which is what makes the `Origin` guard's position ahead of the router
(web.py:945) a structural guarantee for every endpoint added later rather than
something each new route must remember.

Also here, and it must land *with* the endpoint rather than after: **one
`try/except sqlite3.OperationalError` around the `do_POST` route dispatch**,
returning 503 `{kind:"busy"}`. Verified: `except sqlite3.OperationalError` appears
exactly once in web.py, at line 792, inside `do_GET`. `do_POST` (web.py:940)
catches nothing, and `BaseHTTPRequestHandler` has no error handler, so a locked
database drops the connection with **no response at all** — the page sees a
browser network error, not a cause. Same shape and same reasoning as the `Origin`
check: guard before routing, so later endpoints inherit it.

**Progress.** `bars.backfill_bars` gains `on_progress: Callable[[int,int,str],
None] | None = None`, called between the 24 serial requests, matching the injected
`fetch=` parameter the function already uses. `total` is exact from the first tick:
`bars_manifest` measured **4.66 ms for 24 windows** (9 option-1d, 6 underlying-1d,
5 option-1h, 4 underlying-1h) with zero network.

**Honest about why the bar exists.** A full run is ~1.4 s and nobody needs a
determinate bar for 1.4 s. The justification is the **tail**:
`marketdata._TIMEOUT_S` is 25 s per request and `backfill_bars` collects
per-request failures rather than aborting (bars.py:496), so the worst case is
24 × 25 = **600 s**, and today there is no way to tell a 1.4 s success from a hung
ten-minute run from the browser. The manifest preview delivers most of the value;
the fill is the smaller half.

**`--no-scheduler` must default ON for `serve_ephemeral`, from the first commit.**
`tests/conftest.py:34` points `RAW_DIR` at the **live** `raw/` directory, and
`serve_ephemeral` is called against it at four sites in `tests/test_web.py`
(971, 987, 1016, 1032), plus `test_rendered.py:54` and `sweep.py:705`. A scheduler
that started by default would let the test suite fire real fetches against the
real archive and the real `.fetch-state.json`.

**Test.** `run_job` maps each typed exception to the right status. The
`flock`-refusal path returns 409 rather than blocking. A test asserting the sync
job's `run` **calls `flex.fetch`** rather than reimplementing the cooldown
sequence — this is the negative obligation from bug 1, and it needs an assertion,
not a comment. A real fixture for the run-status typedef.

**What could go wrong — two things, both measured.**

**(a) A refused claim wedges the whole database.** `sqlite3` does **not** roll back
on `IntegrityError`. Measured against real `db.connect()` and the real index
shape:

```
refused: UNIQUE constraint failed: job_runs.job, job_runs.fired_for
in_transaction AFTER refusal: True        <-- write lock retained
other writer FAILED after 15.55s: database is locked
after rollback() other writer OK in 0.000s
```

So `run_job` **must** `conn.rollback()` in every `IntegrityError` branch, and that
needs a test, because the failure mode is "the scheduler wedges its own database
by losing a race it was designed to lose". Design 2 found this by running it;
Design 3 built its claim on the same index without knowing. Blast radius at
`BUSY_TIMEOUT_MS = 15_000`: every other writer, including the heartbeat, waits
15 s and then fails.

**(b) The `running` row must be written and committed BEFORE the work starts.** The
adversarial review refuted Design 3 precisely here: it specified `LiveRun` as
in-memory with the row written on terminal state, which means the window between
"is this slot claimed?" and "this slot is claimed" spans the entire job. Then a
`SIGKILL` mid-fetch (which launchd `KeepAlive` makes routine, ~10 s respawn) leaves
no row, no stamp — `_record_fetch` runs only after a successful download
(flex.py:412) — and the next boot's reconcile finds the slot unclaimed and spends
a **second IBKR request**, deterministically, with no concurrency involved. Claim
first, commit, then work.

An interrupted run is then resolved by the **kernel**, not a heuristic: a `running`
row whose per-job `flock` is free (try to acquire with `timeout_s=0`) is
`interrupted`. No PID, no staleness guess, correct across laptop sleep and
`SIGKILL` because `flock` releases on process death. Four sub-millisecond
attempts per page load. This is Design 1's best idea and it is the reason to
prefer `flock` over a lock table.

#### What step 5 shipped, and where the plan was wrong again

Commits `eeefe23` (registry, runner, shared sync), `ae8768a` (endpoint, 503 guard,
interrupted-run detection), `a32574b` (the Run buttons).

**1. `sync.py` is a new module, and the layering test is why.** The plan said
"`web -> jobs` keeps the import graph acyclic", which is true and insufficient: the
`sync` job has to CALL the shared sync path, so `jobs -> web` appeared too. It
worked through a deferred `from optjournal.web import sync_journal` inside a
function, and `tests/test_layering.py` correctly reported `cli -> jobs -> web ->
jobs`. A deferred import is a workaround for a wrong graph, and the README already
calls that shape a design smell — so the honest fix was recognising that
`sync_journal` was never a web concern. It needs `flex` and `ingest`, both below
`jobs`. Now `sync -> {flex, ingest}`, everything imports downhill, and no deferred
import remains on the path.

**2. `jobs_data` read `job_state`, so a job that never ran was invisible.** No row,
no button, no way to start it — and `market` has never run anywhere, so the job the
registry exists to rescue would have been absent from the surface built to make it
runnable. The payload now iterates the REGISTRY (what exists) and uses the table
only for what has happened. A `job_state` row whose job left the registry is kept
and flagged `retired` rather than dropped, because `bars_audit` is exactly that case
and a row that vanishes reads as "this never happened".

**3. `spends_broker_request` had to reach the page.** The plan listed it as a
registry field for the reconciler's benefit. It is also what decides which Run
button asks for confirmation, so it travels in the payload as
`JobRow.spends_request`. The page holds no list of which jobs touch IBKR — the same
rule that keeps "USD high-impact" out of the calendar's markup.

**4. The 503 guard was needed exactly as predicted, and the measurement was
worse than the plan's.** Verified before writing it: against a journal held by
`BEGIN EXCLUSIVE`, `POST /api/watchlist` got `RemoteDisconnected` after **16.06 s**
(one `BUSY_TIMEOUT_MS`) with no response at all, while `GET /api/state` answered in
0.03 s because WAL lets readers through. The page could not name the cause or say
that waiting would fix it.

**Also not shipped: `BEGIN DEFERRED` around `build_state`, again.** Still deferred,
still for the reason recorded under step 4 — the torn-payload probe read 0 of 5
against a control that also read 0 of 5. It is now the only item in step 5's
original scope that has not landed, and it should be argued on its merits or
dropped rather than carried forward a third time.

**Progress reporting did not ship either**, and that is a deliberate scope cut
rather than an oversight: the runner is synchronous, so a POST returns after the
work is done and there is nothing to report progress ABOUT until step 6 moves the
runner to a thread. The plan's own honesty applies — a full run is ~1.4 s, and the
justification was always the 600 s tail rather than the common case.

**What building it cost, three lessons for step 6.**

* Two source-level pins were reading the wrong function after the consolidation.
  Both caught the move rather than the move breaking a consumer, which is what a
  source-level pin is for — but it means a pin's TARGET is itself a thing that rots.
* Two of my own ablations were invalid rather than uncaught: one renamed a method to
  one that does not exist (a crash, not a silent defect), and one grepped for
  `confirm(` while `window.confirm(` still contained the substring. An ablation that
  cannot fail is worth as little as a test that cannot fail.
* The first browser probe's stub never applied, because it rewrote `jobs.py` after
  the module was imported — so a click that was supposed to be stubbed hit the real
  calendar feed. Harmless here (a public endpoint, a scratch journal, no IBKR
  request, live journal verified untouched) and it would not have been if the button
  under test had been `sync`. Stub through the object the server will actually
  consult.

### Step 6 — The reconciler thread — **DONE**

**What changes.** A `threading.Thread` started by `serve()` unless
`--no-scheduler`: `while not stop.wait(60): reconcile()`.

**It is a wall-clock catch-up reconciler, never a sleeping timer, and that is
forced rather than stylistic.** Verified on this machine:

```
monotonic impl: mach_absolute_time()
monotonic        = 138522
CLOCK_UPTIME_RAW = 138522     <-- identical: monotonic EXCLUDES sleep
CLOCK_MONOTONIC  = 298925
sleep excluded from monotonic: 44.6 hours
```

So `ev.wait(seconds_until_next_fire)` is not approximately right here, it is 55%
slow (44.3 h asleep of 80.7 h wall, 292 sleep/wake cycles, mean 7.5 min awake).
A 60 s tick is never accumulated into a deadline, so oversleeping costs one tick
of latency and nothing else. A noon job on a laptop asleep at noon runs within a
minute of the lid opening.

**Due-ness.** A job is due when `now` is at or past its most recent scheduled
instant in its own zone, **no `job_runs` row exists for that `fired_for`** (note:
*recorded*, not *succeeded* — see below), and the instant is inside the job's
catch-up window. Idempotency is a database constraint, not a convention, which is
also the DST fall-back guard: the repeated 01:30 is the same instant, so it cannot
fire twice and spend two IBKR requests.

**Due-ness keys on "recorded", not "outcome='ok'", and I am overruling Design 1
here.** Design 1's rule was "no row with `outcome='ok'`", with auto-pause
deliberately dropped and no backoff. Judge 1 worked out the consequence and I
agree: a sync that fails for a real reason (the locked keychain that actually
happened) is due again on the very next tick and stays due for the whole 12 h
window, so the 900 s cooldown becomes the only brake — roughly 48 real IBKR
requests in twelve hours against a lockout budget. Keying on "recorded" plus a
`consecutive_failures` count on `job_state` costs one column.

**Catch-up is per job, as a three-valued enum on the spec** (`latest` | `window` |
`none`), so "run everything overdue" is impossible to write by accident and the one
genuinely dangerous case is in the type rather than in a comment.

| job | catchup | why |
|---|---|---|
| `sync` | `latest`, 12 h | A missed noon is worth running at 18:00. The docstring records a badly-timed sync missing Monday's fills twice. |
| `bars_daily` | `latest`, 20 h | Re-fetchable by definition. |
| `market` | `latest`, 24 h | The feed serves this week; a late run still gets it. |
| `bars_live` | `window` | **Not a wall-clock fire at all.** |

**`bars_live` collapses seven cron slots into one predicate**, and this is the
biggest genuine simplification any design found (Design 1's, grafted by two of the
three judges). Due if `now` is inside the ET session window **and** the last
success is more than 55 minutes old. Because the intraday series is *cumulative
within a session* — a 13:00 poll returns every completed bar since the open — one
wake at 14:30 after sleeping since 10:00 collects the whole session. That is
launchd's coalescing expressed in the job rather than begged from the substrate,
and it removes the job with the most slots from the code path most likely to have
slot-arithmetic bugs. Outside the window it is **never** due, so a 20:00 wake
records the session `missed` rather than fetching nothing and calling it `ok`.

**Ordering becomes a real happens-before edge.** One worker runs due jobs
sequentially in declaration order, so `sync` precedes `bars_daily`, which derives
its manifest from the positions the sync ingests. Today that ordering is two
wall-clock guesses plus a coin flip: MeshClaw's `_compute_jitter` returns
`random.uniform(0, 59*60)` for these expressions, and it was observed putting a
538 ms audit job 25 minutes late.

**Empty ledger means UNKNOWN, not OVERDUE.** `job_runs` lives in `journal.db`,
which `raw/` rebuilds, so a rebuilt journal shows no runs. A job with **no**
recorded run at all is scheduled for its next natural slot and never caught up.
Without this rule the first reconcile after a restore spends an IBKR request and
24 bar requests unprompted. Both losing designs stated it and Design 1 did not;
it is the sharpest foot-gun here and it belongs in the code as a comment.

**Containment.** Each job's due-check is wrapped in `try/except` so one job's bad
zone arithmetic cannot stop the tick, and the failure is recorded. The heartbeat
is written by the tick loop itself, so a dead loop reads as a dead heartbeat.
Design 3 named this failure exactly and it is worth quoting: a daemon thread that
raises leaves the HTTP server perfectly healthy and the schedule dead — the
40-hour outage reproduced inside its own fix.

**`slept=1`** is stamped on any run claimed in a tick where the wall-clock delta
materially exceeds the monotonic delta. Both clocks are already read, so it is
free, and it turns "why did this fire at 09:14 instead of 12:00" into a field.

**Run it in parallel with MeshClaw for a few days.** Safe because of the `flock`
plus the 900 s cooldown: a doubled sync **refuses** rather than double-spending.
Enable `market` first — it has never run, so it cannot regress anything, and it is
the cheapest proof the reconciler fires. Then `bars_daily`, then `bars_live`, then
`sync` last, because it is the only one that spends an IBKR request. Disable each
MeshClaw cron as its counterpart proves out, one at a time, never in a batch.

**Test.** `due_jobs(now, ledger)` is a pure function taking a `datetime`, so the
whole sleep and catch-up policy is testable by passing a clock rather than by
waiting. Frozen-clock tests at **both** DST boundaries — the fall-back hour must
not double-fire, and the nonexistent spring-forward 02:30 must normalise. A test
that `bars_live` outside the window records `missed`, not `ok`.

**What could go wrong.** A wrong zone or a DST edge silently converts "missed,
unrecoverable" into "ran, ok" — the exact inversion the audit exists to catch,
which is why the boundary tests are not optional.

#### What step 6 shipped, and the two bugs only RUNNING it found

Commits `c81fe83` (`due_jobs`), `454c8f4` (the thread).

Everything above shipped as written, including both DST boundary tests and the
empty-ledger rule. Two additions the plan did not name:

**`FAILURE_BACKOFF = 5`.** The plan's due-ness rule ("recorded, not succeeded")
stops a failed job being retried for its *own* instant, but nothing stopped a job
whose every instant fails from being started once per instant forever. Five
consecutive failures now stops the RECONCILER starting it, while leaving it
runnable by hand from the page — a brake, not a black hole, and the count clears on
any healthy outcome so recovery needs no restart.

**`tick_failures` beside `ticks`.** A test forced this: the first version counted
only COMPLETED ticks, so a loop that was alive and failing every tick read as dead
— 94 raises against a `ticks` of 0. That is `crons.json`'s two green days inverted,
and the fix is the same one this whole plan applies elsewhere: "is the loop alive"
and "are its ticks working" are two questions, so they are two counters.

**THE BUG THAT ONLY RUNNING IT COULD FIND, and it is the most instructive thing in
this step.** `due_jobs` was written `registry: tuple[Job, ...] = JOBS`. A default
argument is evaluated at DEFINITION time, so the tuple was captured once at import
and `monkeypatch.setattr(jobs, "JOBS", ...)` never reached the function. **Every
test that replaced the registry was silently exercising the real one**, and they
passed because the real schedules happened to agree with what the stubs asserted.
Green, and measuring something else.

Nothing in a source-reading or unit-level test could see it. It surfaced when the
actual `Scheduler` ran six ticks against a one-job stub registry and did nothing at
all. The lesson for step 7 is direct: the plan's step-6 test list was entirely
about `due_jobs`, a pure function — and the defect was in how the caller reached
it. **Test the wiring by running the wiring.**

Two of my ablations were also wrong in ways worth recording, because both patterns
will recur: one was behaviourally identical to the correct code (`registry or JOBS`
versus `registry is None`), and one asserted "the thread was joined" by checking an
attribute the ablation cleared anyway. An ablation that cannot fail is worth as
little as a test that cannot fail.

**Not yet done, and it is the remaining risk in this step:** the parallel-run
rollout. The reconciler is written and tested but has never run beside MeshClaw
against the live journal. The plan's own order stands — `market` first (it has never
run, so it cannot regress anything), then `bars_daily`, then `bars_live`, then
`sync` last — and disabling each MeshClaw cron one at a time, never in a batch.
That is step 8's opening move rather than a loose end here.

### Step 7 — Supervision, logs, and a shutdown that actually works — **DONE**

**What changes.** `launchd/com.optjournal.serve.plist`, tracked in the repo:
`RunAtLoad`, `KeepAlive`, `StandardOutPath`/`StandardErrorPath`,
`EnvironmentVariables: PYTHONUNBUFFERED=1`. A plist is data, not a dependency.
`PYTHONUNBUFFERED` is load-bearing: `print()` to a file is block-buffered and a
redirected `serve` log measured **0 bytes** while running. Plus a
`RotatingFileHandler` (macOS rotates nothing), and a bind failure treated as
fatal-and-loud rather than something to be respawned into — `KeepAlive`'s 10 s
`ThrottleInterval` turns a startup failure into an invisible crash loop, and two
stale `serve` processes were live during the surveys.

**The SIGTERM handler, and a refutation I am fixing rather than dropping.** Design
3 prescribed "a `signal.SIGTERM` handler calling `httpd.shutdown()`". I reproduced
the consequence:

```
mode=main (Design 3's shape: serve_forever on the MAIN thread):
  HANDLER ENTERED
  STILL ALIVE 3s after SIGTERM => DEADLOCK      <- shutdown() never returned

mode=thread (serve_ephemeral's shape, web.py:1072):
  SHUTDOWN RETURNED in 0.196s
```

`web.serve` runs `httpd.serve_forever()` on the **main** thread (web.py:1029).
CPython delivers signals on the main thread, so the handler interrupts
`serve_forever` and then calls `shutdown()`, which waits on an event only
`serve_forever` can set. `socketserver`'s own docstring says so: "This must be
called while `serve_forever()` is running in another thread, or it will deadlock."

The chain is worse than a slow exit, and all of it is a consequence of *this*
plan putting a reconciler and a worker in that process: `scheduler.stop()` on the
next line never runs, so the heartbeat keeps advancing while the listener is dead
— inverting the honesty signal that justified building it — the port stays
`LISTEN`-bound so a replacement bind fails `Errno 48`, and launchd's `ExitTimeOut`
eventually `SIGKILL`s, orphaning the in-flight run's `running` row on every
**normal** stop.

**Fix**: run `serve_forever` on a thread and block the main thread on an event,
matching `serve_ephemeral`, which is the only shape the suite exercises. It does
not change the design's shape, and it must be written down because the natural
reading deadlocks while the suite reports green — this project's signature failure
mode.

Note that every current caller is safe by accident: today `serve` catches only
`KeyboardInterrupt` (web.py:1030) and never calls `shutdown()`, so `SIGTERM` takes
the default disposition and exits cleanly. This plan adds the first `shutdown()`
against a main-thread server.

**Also fix here, and do not mistake one for the other.** `_Handler.timeout = 30`
is right, but it is a per-connection *socket* timeout on `rfile`/`wfile` — it does
**nothing** about the 600 s tail. Python cannot kill a thread blocked in a
syscall, so the job timeout must be an explicit **socket timeout passed down the
fetch path**. `py_ibkr` calls `urlopen` with no timeout and
`socket.getdefaulttimeout()` is `None`, so today the only killer of a hung fetch
is the cron's 720 s subprocess timeout — which this plan deletes. Do **not** reach
for `socket.setdefaulttimeout()`: it is process-global and would apply to the web
server's own sockets. The real fix is a `timeout` argument upstream in `py_ibkr`.

**Test.** A test that sends a real signal to a real server and asserts it exits.
`grep signal tests/` currently finds nothing, which is why the deadlock was
invisible.

#### What step 7 shipped

Commits `d2fee81` (the deadlock), `4903234` (plist and logs), `014d4c4` (the socket
timeout).

The deadlock was **reproduced before being fixed**, twice: from first principles
with the bare stdlib (main thread: still alive 3s after SIGTERM; thread: exited in
1.0s), then through the real CLI (the plan's prescribed shape sat alive 15s; the fix
exits in 0.53s, code 0, port released). `tests/test_shutdown.py` is new and sends
real signals to the installed console script.

**The `py_ibkr` timeout could not be fixed upstream**, because it is a pinned
third-party package. A subclass overriding `_get` -- the single choke point both
Flex calls go through -- gets the same coverage without touching the dependency.

**Three defects found by RUNNING each piece, none visible to a reading:**

1. `plutil -lint` accepted a plist Python's expat parser rejected: my comments used
   `--` as an em-dash substitute, which XML forbids inside a comment. The tool a
   reader reaches for is more lenient than the one launchd uses.
2. A real `serve` with a live scheduler wrote a log of **zero bytes**, because
   `reconcile` logs only when something is due. A log empty because all is well
   cannot be told from one empty because the loop is dead -- this project's
   signature failure, reintroduced inside the logging added to prevent it. The
   scheduler now logs one line at start and one at stop.
3. A socket timeout raises a bare `TimeoutError`, not a `URLError`. My comment
   asserted the opposite, so the timeout escaped unhandled rather than arriving as
   a `FlexError`.

**And one caused by the fix itself:** naming the new client class at the call site
broke `tests/test_locks.py`'s network stub, which replaced `flex.FlexClient` -- a
name nothing called any more -- so two subprocesses went to the real IBKR endpoint.
There is now one seam, `flex._client_factory`, with a test asserting the fetch path
goes through it. "The stub applies to a name nothing calls" is invisible by
construction, which is why it needs a guard rather than care.

**The plist is written and tested but NOT INSTALLED.** Loading it starts a service
at login that spends IBKR requests on a schedule; that is the user's decision, and
it is the first move of step 8 rather than a loose end here.

#### FIRST REAL RUN against the live journal, 2026-08-09 21:54 UTC

`serve` restarted onto current code with the scheduler live (heartbeat 6s), and
`POST /api/jobs/run {"job":"market"}` was pressed by hand:

```
reply   202 {"ok":true,"kind":"queued","job":"market","run_id":1}
status  GET ?id=1 -> status ok, detail "73 fetched, 73 stored", done 73, total 73
data    market_events 99 -> 172, newest fetched_at 2026-08-09T21:54:57+00:00
ledger  job_runs row 1, fired_for NULL (a manual run claims no instant)
```

**This is the job that had never run on a schedule anywhere** -- the 143 lines of
calendar policy whose absence from `crons.json` is the reason the registry became
code. It has now run once, from the page, with no terminal involved.

Due-ness re-derived from the real ledger afterwards, which is the part worth
recording because it is what tomorrow depends on:

```
Sun 22:5x         -> nothing          (empty-ledger rule; a restart cannot surprise)
Mon 11:05 Dublin  -> market           claims instant 1786356000 = Mon 11:00 +01:00
Mon 12:05 Dublin  -> market           the SAME instant, so it collapses
Mon 15:00 ET      -> bars_live, market
after that claim  -> nothing at 11:05, 12:05; bars_live only at 18:05
```

So `market` fires once tomorrow at 11:00 and the claim holds for the rest of the
day. `bars_live` becomes due inside the US session, and `sync`/`bars_daily` stay
held by the empty-ledger rule until each has one recorded run -- which is the
parallel-run order the step below prescribes, arrived at by the code rather than by
remembering to follow it.

### Step 8 — Retire MeshClaw

Once the in-app ledger shows a full week matching the crons run for run:
`cron_delete` the four registered jobs, delete the three shims under
`~/.meshclaw/crons/`, delete `cron/*.py` (663 lines), replace
`tests/test_cron.py` (398 lines) with `tests/test_jobs.py`, and update the
README's cron table.

Last, because the crons are the only working scheduler until the replacement has
proven itself on this machine — and because the thing being replaced already
demonstrated that a scheduler can stop for 40 hours while every surface reports
health.

**Three unversioned side channels disappear with it**, and one of them is already
broken:

1. **Registration.** Covered in step 5: 143 lines that never ran.
2. **The two-layer timeout ladder, already wrong.** `bars-audit` is registered at
   `timeout: 120` while its own `FETCH_TIMEOUT_S` is 240, and MeshClaw wraps the
   script in `wait_for(timeout + 5)` = 125 s — so the outer killer always wins,
   and a MeshClaw script timeout is silent by its own admission. That directly
   contradicts the audit docstring's "a timeout or a crash DOES raise... staying
   quiet about that would leave the one thing watching for silent loss silently
   broken itself." The project even built `verify_timeouts()` for this bug class,
   but it cannot see the registered value because that lives in unversioned JSON.
   One `timeout_s` per job in `jobs.py` deletes the bug **and** the guard.
3. **The stale toolbox.** The running gateway is `3.3.6` on python3.10 while
   `info.json` says CurrentVersion is `3.3.7` on python3.12. The delivery policy
   executes under an interpreter and a library this project does not pin, does not
   test, and cannot see — the same class of failure the by-path shim was built to
   prevent, one layer up.

**One inversion that changes what "faithful" means.** The docstrings imply `raise`
is louder than `Report`. It is the opposite: the script branch of `_cron_callback`
catches every exception, logs, and returns — zero `notify`/`slack`/`post_message`
calls in that whole branch. So 14 of the 21 decisions reach only `gateway.log`.
Proven live: the 2026-08-07 sync raised on a locked keychain and the only trace is
one log line. True tiers today are `Report`(chat inject) > `raise` == `Skip` ==
silent(nothing). Porting `raise` as "loud, visible in the Jobs table" is an
**improvement**, and a migration that carefully preserved "raise is quiet" would be
preserving a bug.

**The six runtime services, each an explicit decision, not a silent drop.**

| service | decision |
|---|---|
| Auto-pause after 5 failures | **KEEP** as `consecutive_failures` + backoff, visible in the UI. Judge 1 was right that Design 1's drop-it-entirely creates a retry storm. |
| Identical-failure dedup | **DROP.** A table row is a state, not an event, so it cannot repeat. |
| Concurrent-execution guard | **KEEP and strengthen** — per-job `flock` is cross-process, which MeshClaw's never was. |
| Bounded run history | **KEEP**, 200/job, never pruning the newest `fired_for`. |
| Credential redaction | **KEEP**, and this is the one with a security edge. `detail` is capped and built from typed exception fields, never raw `stderr[:800]`, because sync messages carry account-derived data and the ledger is now served over HTTP into a page. |
| Sandboxed subprocess execution | **DROP**, and it is a real loss — see below. |

---

## What stays on the CLI, and why

"No need to run anything from CLI" is about the *daily operating surface*, not
about deleting the CLI. All 17 subcommands stay. Three reasons, in order of how
load-bearing they are:

1. **The CLI is the recovery path.** This plan makes scheduling conditional on one
   process. When that process is down — and step 7 exists because it will be — the
   CLI is how you sync, ingest and inspect. Deleting it would make "the UI is the
   only way" true in the bad sense.
2. **The test suite, the mutation harness and the sweep drive it.** `sweep.py`
   spins its own `serve_ephemeral` (sweep.py:705) over ~35 pages; `mutate.py`
   clones the repo and runs the suite once per mutant (31 mutants, `_SUITE_TIMEOUT_S
   = 500` each) and rewrites `.venv` `.pth` files. These are dev tools driven by
   argparse.
3. **`locks.py` is what makes the CLI safe beside the daemon.** `optjournal sync`
   typed in a terminal while the daemon syncs is a genuine two-process case, and
   `flex.fetch`'s `flock` covers it.

**Gets a web button** (as a job row): `sync`, `bars` (all modes), `market`,
`ingest` (0.45 s worst measured, idempotent — a plain blocking POST, no job
machinery needed).

**Must NOT get a web button**, each for a specific reason rather than caution:

- **`serve`** — it *is* the server. A button to start it is circular.
- **`mutate`** — rewrites `.venv` `.pth` files (mutate.py:496), clones 84 MB, 31 ×
  ~500 s. A dev tool, not an operation.
- **`sweep`** — spawns Chrome per page and starts its own `serve_ephemeral`, so it
  is self-referential from inside the served process.
- **`demo`** — `demo.assert_not_real` (demo.py:666) exists precisely to keep
  synthetic statements out of the `raw/` and `journal.db` the server is pointed at.
  `cli.py:669-679` already refuses the analogous `--demo` + `--query-id`.
- **`show`** — its argument is an arbitrary filesystem **path** (cli.py:861). Over
  HTTP on a server with no authentication that is a file-read primitive, the same
  class of thing the `/static/` handler explicitly defends against
  (web.py:766-774). If the summary is wanted, put `summary_data` for the *newest*
  statement into `build_state`; do not add a path parameter.
- **`prune --apply`** — the only operation that DELETES from the provenance root
  (archive.py:197-201). It gets a typed-confirmation two-step at most, and I would
  ask before wiring it at all.

**Already web-reachable as a read**, so no endpoint is needed: `statements`,
`costs`, `orders`, `positions`, `history`, `watch --show`, `market --show` are all
already in `build_state` and drawn.

---

## What is not worth doing

- **A metrics backend, tracing, structured JSON to a collector, containers, a task
  queue, or authentication.** One user, one loopback socket, two runtime deps.
  Everything in this plan is stdlib `logging`, `fcntl`, `threading`, `zoneinfo`, a
  plist and `VACUUM INTO`. The minimalism is documented as a value (README:183 —
  a module whose purpose is to have no dependencies may not acquire one) and none
  of these gaps needs breaking it.
- **A Cancel button in v1.** You cannot kill a thread mid-`upsert_bars`, and the
  honest version (a flag checked between the 24 requests) buys little for a 1.4 s
  median. What the UI gives instead is the truth about a stuck run: a `running`
  row older than the job's `timeout_s` renders red as "stuck for 23 min", and
  recovery is a restart, which `KeepAlive` makes cheap and `flock` makes safe. If
  the 600 s tail later forces a cancel, build it with honest granularity — the flag
  is checked *between* windows, the button says "stopping after the current
  request", `upsert_bars` commits per request so a cancelled fill is a **partial**
  fill and the run records `cancelled` **with its `done` count**, or a half-filled
  session reads as a full one.
- **An acknowledge table for the audit badge.** A 7-day window costs a table, an
  endpoint and an affordance less. This is the place my "delete" lean is most
  likely to be wrong: a session lost 8 days ago silently stops being mentioned,
  and for a permanently unrecoverable artefact that is arguably the wrong trade.
  Flagging it rather than hiding it.
- **Editing schedules from the web.** Schedules stay in `jobs.py` where a test can
  see them and git records who changed them. This is what fixes the never-registered
  calendar job, and it means "control everything from the web" stops short of the
  schedule itself. A deliberate trade.
- **launchd `StartCalendarInterval` per job.** It coalesces missed fires on wake
  for free and correctly, which is a genuine argument against reimplementing that
  in Python — Design 3 was honest enough to raise it against its own lean. It is
  rejected because N agents plus a server is N+1 mutually blind processes racing a
  guard on a lockout budget, and because it cannot express the ordering edge. The
  documented fallback (one agent per job whose only action is a loopback POST) is
  **not** taken: `curl` sends no `Origin`, so it would require carving an exemption
  into the single defence this server has. If the reconciler proves fiddly, debug
  the reconciler.
- **A push notification channel.** `osascript display notification` is three lines
  and would be the first thing to add if the silence proves too quiet. It is not in
  v1 because progress must be **pull, never push**: ~1,800 live polls, ~365 syncs
  and ~365 calendar runs a year are carried today by 5 silent returns and 4 `Skip`s
  so that 8 sites are the only things that ever speak. A history table is one
  careless commit away from becoming the noise those 660 lines exist to avoid.
- **`socket.setdefaulttimeout()`.** Process-global; it would apply to the web
  server's own sockets. The fix is a `timeout` argument upstream in `py_ibkr`.
- **A schema downgrade guard.** Real (a journal stamped at 99 opens silently under
  code at 7) but the exposure is "ran an old venv from a stale shell", and the
  migration is additive. A known gap with a 3-line fix available; not worth
  building migration tooling for.

---

## What this design gives up, stated plainly

- **Scheduling becomes conditional on one process.** Three independent short-lived
  crons fire whether or not anything else is up; afterwards, if `serve` is not
  running, nothing is collected. `KeepAlive` covers crash and reboot but **not a
  user who quits the server on purpose**. This is the central bet, and the coupling
  runs the wrong way for the perishable data: `bars_live` is the one job whose
  missed fire is unrecoverable, and it is now hostage to the health of an HTTP
  server it does not need.
- **Crash isolation is genuinely lost.** Today a segfaulting fetch kills a
  subprocess and the web server survives. In-process it takes the UI and the
  schedule with it. Traded for typed exceptions and the single-fetcher cooldown
  guarantee, and it is the strongest reason to keep the CLI paths alive.
- **The LLM-investigates channel is gone and nothing replaces it.** A `Report`
  today is injected into a Claude chat session where an agent with tools can go
  look; all three optjournal Reports ever delivered landed as `role:inject` lines,
  and `notifications.jsonl` has zero rows from these crons. A badge is read by
  whoever opens the tab. For the one signal about permanently unrecoverable data
  this is **worse than what it replaces**, and calling it equivalent would be
  dishonest. What partially redeems it: the audit is now computed on page load, so
  it cannot be silently broken by the same outage that caused the loss — which the
  current arrangement demonstrably could.
- **The 660 lines are rewritten, not moved.** 630 of them are pure stdlib and look
  portable, which makes the migration look cheap — but the exit-code branching is
  the wrong shape for in-process jobs and should be deleted rather than translated.
  The delivery *judgement* in the docstrings is the expensive part and it is 81%
  untested, which is why step 3 exists and why it is doing more work than a step 3
  usually does.
- **`state["audit"]` borrows its oracle.** `market_traded_on` reads the underlying
  hourly series that `bars_daily` tops up, so if daily has not run the badge can
  read "nothing to audit" for a session that was actually lost — the same wrong
  answer the jittered cron gave. Mitigated by having the badge say "cannot tell
  yet" when the last `bars_daily` success predates the day in question (~5 lines),
  but the oracle is borrowed rather than independent.
- **The sleep measurements are one boot of one laptop.** 55% asleep, 292 cycles,
  mean 7.5 min awake. If the machine ends up docked with sleep disabled, the 60 s
  tick is pure overhead and a plain timer would have been fine. The design is not
  wrong under that change, just unnecessarily careful.
