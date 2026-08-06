"""Daily IBKR statement sync for the options journal.

A script cron rather than an LLM cron: fetching a statement and folding it into
SQLite is fully deterministic, so there is no judgement for a model to add and
no reason to spend tokens on it. The CLI already decides what is new.

Delivery policy, which is the part worth getting right for something that runs
365 times a year:

* New activity        -> Report. Keeps the job and notifies.
* Nothing new         -> return quietly. No notification. A daily "nothing
                         happened" message trains you to ignore the channel,
                         and this account averages two option fills a year.
* Throttled by IBKR   -> Skip. Flex rate-limits repeat generation of the same
                         query, which is expected, not a fault. Skipping means
                         no alert and a natural retry next tick. The CLI's
                         local fetch cooldown maps to the same exit code, so
                         "we chose not to ask" is handled identically.
* No new data (3)     -> return quietly. `sync` does not currently emit this,
                         but every other command uses the
                         `EXIT_OK if data else EXIT_NO_DATA` idiom, so treating
                         it as a failure would turn a future refactor of
                         cmd_sync into a daily false alarm.
* Fetch timed out     -> Report, because it needs a human. The request reached
                         IBKR before the timeout, so it was spent, and the
                         cooldown is recorded only after `download` returns --
                         meaning a timed-out run leaves no cooldown and the
                         next invocation will spend another request. Not
                         something to retry blindly.
* Anything else       -> raise. The job is retained and MeshClaw's failure
                         dedup suppresses repeats of an identical error.

Register with (query ID passed via the cron's message field):

    cron_add(
        name="optjournal-daily-sync",
        script="~/.meshclaw/crons/optjournal_sync.py:sync",
        message="1591754",
        cron_expr="0 12 * * 2-6",
        timezone="Europe/Dublin",
        timeout=900,
    )

The cron timeout must exceed FETCH_TIMEOUT_S below, which must in turn exceed
`optjournal.flex.POLL_WORST_CASE_S`. Get that ordering wrong and the outer
killer fires first, replacing a clean Report with a raw traceback.

Tuesday-Saturday is deliberate: an Activity Statement covers the previous
trading day, so a Monday run would only re-fetch Friday's already-ingested
data and a Sunday run would find nothing at all.

12:00 Dublin time rather than 07:00, from evidence: 07:00 IST is 02:00 ET,
before IBKR has generated the previous day's statement -- the 07:00 run
fetched stale bytes twice (2026-08-03 and -04), missing Monday's fills both
times. Real statements have been observed generating around 05:00 ET, so
12:00 IST (07:00 ET) clears that with margin while still landing before the
US session opens.

After a fetch, any archived statement is committed to the workspace backup
repo (`git add -f`, because optjournal/.gitignore excludes raw/ and nested
gitignores override the workspace root's unignore chain). The raw XML is the
one artifact that cannot be regenerated once IBKR's ~365-day window passes,
and three statements were silently unversioned before this step existed. A
routine backup commit stays silent; a backup *failure* Reports, because a
backup that fails quietly is not a backup.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from mesh_claw.cron_script import Report, Skip

PROJECT = Path.home() / ".meshclaw" / "workspace" / "optjournal"
CLI = PROJECT / ".venv" / "bin" / "optjournal"

#: The workspace repo is the backup home for raw statements; the project repo
#: deliberately excludes them (they carry the account number and belong in a
#: backup, not next to source that might grow a remote).
WORKSPACE = Path.home() / ".meshclaw" / "workspace"
RAW_DIR = PROJECT / "raw"

#: Mirrors optjournal.cli. Kept explicit so a CLI change that renumbers exit
#: codes shows up as a wrong branch here rather than as silent misreporting.
EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_NO_DATA = 3
EXIT_THROTTLED = 4

DEFAULT_QUERY_ID = "1591754"

#: Ceiling on the CLI subprocess, sized from the real polling budget rather
#: than estimated. `optjournal.flex.POLL_WORST_CASE_S` is currently 660s
#: (two retry stages of 330s), so this allows that plus margin for the HTTP
#: round trips, XML parse and SQLite ingest that follow.
#:
#: This was 240s against a then-actual worst case of 2,100s, on a comment
#: claiming "~84s" that had been carried over from a since-deleted script.
#: If MAX_RETRIES changes in flex.py, this and the cron's own timeout must
#: move with it -- verify_timeouts() below fails loudly if they drift.
FETCH_TIMEOUT_S = 720


def _poll_worst_case() -> int | None:
    """Ask the project venv for flex.POLL_WORST_CASE_S.

    Imported rather than duplicated, but it cannot be a plain import: this
    script runs under MeshClaw's interpreter, which has no py_ibkr, so
    `import optjournal.flex` always raises. An earlier version caught that
    ImportError and returned, which meant the guard below never once ran.
    Asking the venv's own python is the only way to read the real number.
    """
    venv_python = PROJECT / ".venv" / "bin" / "python"
    if not venv_python.exists():
        return None
    try:
        out = subprocess.run(
            [str(venv_python), "-c",
             "from optjournal.flex import POLL_WORST_CASE_S; print(POLL_WORST_CASE_S)"],
            capture_output=True, text=True, timeout=30, cwd=str(PROJECT),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return int(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def verify_timeouts() -> None:
    """Fail loudly if the timeout ladder has drifted out of order.

    Called at the top of `sync`, so a mismatch surfaces as a clear error on
    the next run instead of as a mysterious mid-poll kill weeks later.

    Silent when the number cannot be read at all -- a broken venv is the
    CLI's problem to report, and blocking the sync on a self-check that
    cannot complete would be worse than running it.
    """
    worst_case = _poll_worst_case()
    if worst_case is None:
        return
    if worst_case >= FETCH_TIMEOUT_S:
        raise RuntimeError(
            f"FETCH_TIMEOUT_S ({FETCH_TIMEOUT_S}s) must exceed "
            f"flex.POLL_WORST_CASE_S ({worst_case}s), or a routine slow "
            f"statement generation is killed mid-poll. Raise it, and raise the "
            f"cron's own timeout above that, or lower MAX_RETRIES in flex.py."
        )


def _run(query_id: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CLI), "sync", query_id, "--json"],
        capture_output=True,
        text=True,
        timeout=FETCH_TIMEOUT_S,
        cwd=str(PROJECT),
    )


def _describe(payload: dict) -> str:
    # `new_trade_rows` is the row list; `new_trades` beside it is the COUNT, the
    # same shape web._do_sync sends the page. They were once one key holding
    # both, differing by which sync produced it.
    trades = payload.get("new_trade_rows") or []
    lines = [
        f"*IBKR sync* — {payload.get('new_trades', len(trades))} new trade(s), "
        f"{payload.get('new_cash', 0)} new cash row(s)"
    ]
    for t in trades:
        lines.append(
            f"• {t.get('trade_date')}  {t.get('symbol')}  "
            f"{t.get('open_close') or '-'} {t.get('buy_sell') or '-'} "
            f"qty {t.get('quantity')} @ {t.get('trade_price')} "
            f"(comm {t.get('ib_commission')} {t.get('currency')})"
        )
    for w in payload.get("warnings") or ():
        lines.append(f"⚠️ {w}")
    lines.append("")
    lines.append("`optjournal history` / `optjournal positions` for the book.")
    return "\n".join(lines)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(WORKSPACE), *args],
        capture_output=True, text=True, timeout=60,
    )


def _commit_raw_backup() -> str | None:
    """Commit any unversioned raw statements to the workspace backup repo.

    Returns a one-line status when a commit was made, None when there was
    nothing new. Raises RuntimeError when git fails, so the caller can decide
    how loudly to say so.

    Catch-up semantics, deliberately: every `*.xml` in raw/ is (force-)added
    on every run, not just today's file, so a run that fetched nothing still
    sweeps in any statement a previous run archived but failed to commit.
    Adding an already-tracked, unchanged file is a no-op, which is what makes
    the unconditional add safe.

    `-f` because optjournal/.gitignore ignores raw/ wholesale and a nested
    gitignore overrides the workspace root's `!raw/*.xml` unignore chain --
    that override is exactly how three statements went silently unversioned.
    Files are enumerated here rather than passed as `raw/`, so `-f` can never
    drag in non-XML residue like the .fetch-state.json cooldown record.

    The commit pins its pathspec, so anything the user happens to have staged
    in the workspace repo stays out of this commit.
    """
    xmls = sorted(RAW_DIR.glob("*.xml"))
    if not xmls:
        return None
    rel = [str(p.relative_to(WORKSPACE)) for p in xmls]

    add = _git("add", "-f", "--", *rel)
    if add.returncode != 0:
        raise RuntimeError(f"git add failed: {(add.stderr or '').strip()[:300]}")

    staged = _git("diff", "--cached", "--quiet", "--", *rel)
    if staged.returncode == 0:
        return None  # everything already versioned
    names = _git("diff", "--cached", "--name-only", "--", *rel).stdout.split()

    commit = _git(
        "commit",
        "-m", "chore(optjournal): archive raw statement(s) from daily sync",
        "--", *rel,
    )
    if commit.returncode != 0:
        raise RuntimeError(f"git commit failed: {(commit.stderr or '').strip()[:300]}")
    return f"backed up {len(names)} statement(s) to the workspace repo"


def sync(ctx) -> None:
    """Fetch yesterday's statement, ingest it, and report only real changes."""
    if not CLI.exists():
        raise RuntimeError(f"optjournal CLI not found at {CLI}")
    verify_timeouts()

    query_id = (ctx.message or "").strip() or DEFAULT_QUERY_ID

    try:
        proc = _run(query_id)
    except subprocess.TimeoutExpired:
        # The request reached IBKR before we gave up, so it is spent. Worse,
        # flex records the cooldown only after `download` returns, so a
        # timed-out run leaves no cooldown and the next invocation spends
        # another request. That needs a human, not a silent retry.
        raise Report(
            f"*IBKR sync timed out* — no statement after {FETCH_TIMEOUT_S}s.\n"
            f"The request was already sent, so it counted against the IBKR "
            f"request budget, and the fetch cooldown was *not* recorded.\n"
            f"Check `optjournal statements` before re-running, and prefer "
            f"`optjournal sync {query_id}` by hand over triggering the cron."
        ) from None

    if proc.returncode == EXIT_THROTTLED:
        raise Skip()

    # A fetch happened (or at least was attempted and returned), so bytes may
    # have been archived -- back them up before interpreting the outcome, so
    # even a sync that errored after archiving leaves the XML versioned.
    backup_note: str | None = None
    backup_error: str | None = None
    try:
        backup_note = _commit_raw_backup()
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        backup_error = str(exc)

    if proc.returncode == EXIT_NO_DATA:
        # Not a failure: nothing to ingest. Stay silent, same as "nothing new"
        # -- unless the backup broke, which must not fail quietly.
        if backup_error:
            raise Report(f"*Raw statement backup failed* — {backup_error}")
        return

    if proc.returncode == EXIT_CONFIG:
        raise Report(
            "*IBKR sync blocked* — configuration problem, so no data was "
            f"fetched:\n```\n{(proc.stderr or '').strip()[:800]}\n```"
        )

    if proc.returncode != EXIT_OK:
        raise RuntimeError(
            f"optjournal sync exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[:500]}"
        )

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"sync produced unparseable JSON: {exc}") from exc

    if payload.get("changed"):
        text = _describe(payload)
        if backup_note:
            text += f"\n{backup_note}"
        if backup_error:
            text += f"\n⚠️ raw statement backup failed: {backup_error}"
        raise Report(text)
    # Nothing new. A routine backup commit is not worth a notification, but a
    # backup failure is -- a backup that fails quietly is not a backup.
    if backup_error:
        raise Report(f"*Raw statement backup failed* — {backup_error}")
