"""Mutation testing: inject a defect, count which tests notice.

The question this answers is not "what is covered" but "what would a real bug
cost". Coverage says a line ran; this says a wrong line was caught -- and by how
many tests, which is the part that tells you whether a suite is well-targeted or
merely large.

Two results are worth acting on. A real defect caught by ZERO tests is an
unguarded invariant, and the suite should grow there: that is how `history._flat`
(a 0.4-share residual booking a partial close as a completed round trip) and
`web._snapshot_leg`'s sign were found, both of which had passed 579 tests. A
defect caught by fifteen tests means fourteen are coupled to something they are
not about. Measured over all 25 mutants: **25 caught, median 2, minimum 1,
maximum 18**.

That 18 is `serialize._num` letting a Decimal reach the payload, and it is worth
reading as a caution rather than as strength. Its 18 are not 18 invariants: eight
are ONE test parametrised over the eight archived statements, and six more are
`costs_data` assertions that each happen to read a number -- four distinct
concerns, fanned out. Contrast the Money currency gate at 8, which spans money,
analysis, strategies and web because the RULE does. So a high count separates a
shared rule from a shared chokepoint only if you read which tests failed, which
is why `format_report` lists them for the low counts and the JSON always does.

The uncaught result keeps earning its place. A later round added ten mutants for
findings a code audit raised, and EIGHT were caught by nothing: the fee currency
attribution, the fee/interest gate, the withholding sign, the enum-to-wire
mapping, the table's column sizing, the strike side of a closed contract, and
the Flex request-budget cooldown (which had no test at all). One of the ten was
not a missing test but a live defect -- `analysis` accumulated
`int(abs(quantity))` per fill, so any lot under one whole unit contributed
nothing and a thousand half-share buys summed to zero. An eleventh candidate
turned out to be unreachable code (`serialize`'s cost-basis fallback, whose two
sides read the same column), and was deleted rather than sentinelled -- worth
recording because "no test caught it" has three possible answers, not two: add a
test, fix the code, or delete the code.

A fourth round asked a question the survey could not answer by inspection: is the
broker seam real? Four mutants over `ingest`, `db`'s views and `history` said no.
`ingest_file(broker=...)` resolved the right source, read the right statement, and
filed every row under 'ibkr'; `trade_legs` summed two brokers' fills for the same
order id into one leg; and two independent `MAX(report_date)` queries let whichever
broker filed most recently decide what counted as current for the rest. All four
were invisible with one broker, which is the shape to watch for -- a seam that
only has one implementation is only as good as the test that uses two.

That round also found a bug in THIS FILE: `run_mutant` counted only pytest's
FAILED lines, and a defect that breaks a fixture produces ERROR. So a mutant
caught by a fixture's own assertion was reported as caught by nothing. Fixed;
undercounting is the dangerous direction, because a zero is what sends someone
looking for a test that is already there.

**Equivalent mutants are not findings.** Some changes have no observable effect,
so "no test caught it" says nothing. The tool reports the count; deciding whether
a defect is real is the reader's job.

WHY THIS IS IN THE REPO. The method took three attempts to get right, and every
failure presented as an alarming coverage result rather than as a broken harness:

1. `uv run pytest` inside a clone resolves to the ORIGINAL project, so the clone's
   mutated file is never imported.
2. `cp -R` copies `.venv`, whose editable-install `.pth` HARDCODES the original
   repo's `src` -- so even the clone's own interpreter imports the original.
3. On macOS `/tmp` is a symlink to `/private/tmp`, so a guard written to catch
   (1) and (2) by string-prefix rejects correct clones.

Each of those produced "the mutation was caught by nothing" for a defect that was
in fact well covered. The first conclusion drawn from it -- that the Money gate
was guarded by one test -- was wrong; the real answer is eight. So this module
PROVES the mutation is the code pytest imported before it will report a number,
and refuses to report one otherwise. A mutation harness that can silently measure
the wrong tree is worse than none, because its output looks like evidence.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["MUTANTS", "Mutant", "MutationOutcome", "run_mutant", "run_all"]


@dataclass(frozen=True, slots=True)
class Mutant:
    """One defect: a module, a substring to replace, and what it breaks.

    Deliberately a literal find/replace rather than an AST rewrite. The point is
    that a reader can see exactly what changed and judge whether it is a real bug
    -- an AST mutation is harder to read than the invariant it is testing, and
    this file is meant to be argued with.
    """

    key: str
    module: str
    find: str
    replace: str
    #: What a maintainer should conclude if nothing catches this.
    breaks: str


#: The defects surveyed so far. Each was chosen to break a documented invariant
#: rather than to perturb a line -- see the module docstring on equivalence.
MUTANTS: tuple[Mutant, ...] = (
    Mutant(
        key="money-gate",
        module="money.py",
        find="    if len(live) != 1:\n        return None, None",
        replace="    if not live:\n        return None, None",
        breaks="a figure spanning USD, SEK and KRW would claim to be a USD figure",
    ),
    Mutant(
        key="money-abs",
        module="money.py",
        find="        return Money(\n            base=abs(self.base),\n"
             "            native=None if self.native is None else abs(self.native),\n"
             "            currency=self.currency,\n        )",
        replace="        return Money(base=abs(self.base))",
        breaks="taking a magnitude would drop the currency label",
    ),
    Mutant(
        key="episode-flat",
        module="history.py",
        find="    return qty == 0 if isinstance(qty, int) else abs(qty) < _FLAT_EPS",
        replace="    return abs(qty) < 0.5",
        breaks="0.4 shares still held would book a partial close as a closed round trip",
    ),
    Mutant(
        key="episode-attrib",
        module="stats.py",
        find="        if _in_period(e.closed_at, period) and scope.has_episode(e)",
        replace="        if _in_period(e.opened_at, period) and scope.has_episode(e)",
        breaks="a round trip would count in the month it opened, not the month it closed",
    ),
    Mutant(
        key="period-prefix",
        module="stats.py",
        find='    day = _day_of(value)\n    return day is not None and day.startswith(period)',
        replace='    return bool(value) and str(value).startswith(period)',
        breaks="IBKR's compact 20250114 dates would stop matching an ISO period",
    ),
    Mutant(
        key="et-zone",
        module="bars.py",
        find="        return int(naive.replace(tzinfo=MARKET_TZ).timestamp())",
        replace="        return int(naive.replace(tzinfo=UTC).timestamp())",
        breaks="every fill marker would sit four or five hours off its bar",
    ),
    Mutant(
        key="bar-stub",
        module="marketdata.py",
        find="    phase = bars[0].ts % seconds\n"
             "    return [bar for bar in bars if bar.ts % seconds == phase]",
        replace="    return bars",
        breaks="the source's synthetic live bar would be stored as a real one",
    ),
    Mutant(
        key="commission-conv",
        module="ingest.py",
        find="    if base_ccy and commission_ccy and commission_ccy == base_ccy:\n"
             "        return commission",
        replace="    if False:\n        return commission",
        breaks="a commission billed in another currency would be converted at the "
               "instrument's rate, storing an EUR amount 11x too small",
    ),
    Mutant(
        key="qty-truncate",
        module="analysis.py",
        find="            g.quantity += abs(Decimal(str(t.quantity)))",
        replace="            g.quantity += int(abs(t.quantity))",
        breaks="a thousand half-share fills would sum to 0 units, so the per-unit "
               "commission column silently shows a dash",
    ),
    Mutant(
        key="snapshot-sign",
        module="web.py",
        find='        seed_quantity=float(row.get("position") or 0.0),',
        replace='        seed_quantity=abs(float(row.get("position") or 0.0)),',
        breaks="a short snapshot-only position would draw its P&L upside down",
    ),
    Mutant(
        key="payload-shape",
        module="money.py",
        find='        return {"base": self.base, "native": self.native, "ccy": self.currency}',
        replace='        return {k: v for k, v in {"base": self.base, '
                '"native": self.native, "ccy": self.currency}.items() if v is not None}',
        breaks="the page would see a missing property where it tests for null",
    ),
    Mutant(
        key="num-decimal",
        module="serialize.py",
        find="    return float(value)",
        replace="    return value",
        breaks="Decimals would reach the payload, so JSON carries strings",
    ),
    Mutant(
        key="asset-sentinel",
        module="cli.py",
        find='        if raw.strip().upper() == "ALL"',
        replace='        if False',
        breaks="`ingest` would store ZERO trades and exit 0 -- silent total loss",
    ),
    Mutant(
        key="annual-filter",
        module="web.py",
        find='        state["annual"] = [\n'
             "            stats_data(s) for s in annual_stats(conn, "
             "asset_category=asset_category)\n        ]",
        replace='        state["annual"] = [\n'
                "            stats_data(s) for s in annual_stats(conn, "
                "asset_category=asset_category)\n"
                "            if selected is None or s.month.startswith(selected[:4])\n"
                "        ]",
        breaks="the Annual tab would follow a month control it does not display",
    ),
    Mutant(
        key="quarantine",
        module="stats.py",
        find="from optjournal.money import Money, win_rate",
        replace="from optjournal.blackscholes import bs_price\n"
                "from optjournal.money import Money, win_rate",
        breaks="a modelled number would be one call from a headline card",
    ),
    Mutant(
        key="loopback",
        module="web.py",
        find="    if not _is_loopback(host):",
        replace="    if False:",
        breaks="an unauthenticated brokerage dashboard could bind a public interface",
    ),
    Mutant(
        key="credit-gate",
        module="analysis.py",
        find="        if commission > ZERO:\n            g.credit_fills += 1",
        replace="        if commission != ZERO:\n            g.credit_fills += 1",
        breaks="every charged fill would be reported as carrying a commission CREDIT",
    ),
    Mutant(
        key="fee-ccy",
        module="analysis.py",
        find='            fee_ccy = str(getattr(c, "currency", None) or "") or base_currency',
        replace="            fee_ccy = base_currency",
        breaks="313 KRW custody fees would claim to be EUR amounts",
    ),
    Mutant(
        key="fee-kind",
        module="analysis.py",
        find='        if "FEES" in kind:',
        replace='        if "FEES" in kind or "INT" in kind:',
        breaks="broker interest RECEIVED would be booked as a cost. `kind` is an "
               "enum MEMBER NAME, so a loose substring is the live hazard: "
               "BROKERINTRCVD contains 'INT' but not 'INTEREST'",
    ),
    Mutant(
        key="withholding-sign",
        module="analysis.py",
        find="            withheld[key] += abs(amount_base)",
        replace="            withheld[key] += amount_base",
        breaks="withholding arrives negative, so the effective tax rate would invert",
    ),
    Mutant(
        key="wire-enum",
        module="serialize.py",
        find="    inner = getattr(value, \"value\", None)\n"
             "    if isinstance(inner, str) and inner:\n"
             "        return inner",
        replace="    inner = None",
        breaks="the payload would carry 'AssetClass.STOCK' where the page reads 'STK'",
    ),
    Mutant(
        key="table-width",
        module="render.py",
        find="        max(len(str(headers[i])), *(len(r[i]) for r in cells))",
        replace="        max(len(r[i]) for r in cells)",
        breaks="a header longer than its column would overflow and misalign the table",
    ),
    Mutant(
        key="strike-side",
        module="web.py",
        find="            if index == 0:\n                opened_at, sold = stamp, delta_qty < 0",
        replace="            opened_at, sold = (stamp if index == 0 else opened_at), "
                "delta_qty < 0",
        breaks="a closed contract would take its side from the CLOSING fill, "
               "labelling every short you sold as a long you bought",
    ),
    Mutant(
        key="cooldown-order",
        module="flex.py",
        find="    if not force:\n        _check_cooldown(archive_dir, query_id, cooldown_s)\n\n"
             "    token = read_token(account)",
        replace="    token = read_token(account)",
        breaks="the request-budget guard would be gone: every call spends an IBKR "
               "request against a lockout allowance",
    ),
    Mutant(
        key="qty-lossless",
        module="sources.py",
        find="    return i if abs(f - i) < 1e-9 else f",
        replace="    return i",
        breaks="a 0.0007-share fill would be stored as 0 shares",
    ),
    Mutant(
        key="broker-stamp",
        module="ingest.py",
        find='" ON CONFLICT(broker, trade_id) DO NOTHING",\n'
             "            (\n                broker,",
        replace='" ON CONFLICT(broker, trade_id) DO NOTHING",\n'
                "            (\n                DEFAULT_BROKER,",
        breaks="`ingest --broker X` would resolve X's source, read X's statement, "
               "and then file every row under 'ibkr' -- the argument decorative",
    ),
    Mutant(
        key="legs-merge",
        module="db.py",
        find="GROUP BY broker, ib_order_id, conid;",
        replace="GROUP BY ib_order_id, conid;",
        breaks="two brokers' fills for the same order id would SUM into one leg, "
               "reporting a position of -3 as -6",
    ),
    Mutant(
        key="current-book",
        module="db.py",
        find="    WHERE asset_category = 'OPT' AND broker = p.broker",
        replace="    WHERE asset_category = 'OPT'",
        breaks="whichever broker filed most recently would decide what counts as "
               "current for all of them, so a lagging broker's book vanishes",
    ),
    # NO MUTANT for the `securities` and `equity_summaries` keys, and the reason is
    # worth recording rather than leaving as an omission.
    #
    # Both were keyed on IBKR's own identifier (`conid`, `report_date`) and are now
    # `(broker, ...)`. That was a real defect -- both tables UPSERT, so a second
    # broker's row REPLACED the first's -- and it is guarded, by
    # `test_a_second_brokers_rows_are_stored_not_swallowed` and
    # `test_a_broker_overwriting_anothers_contract_definition_is_impossible`, both
    # verified by ablation.
    #
    # It cannot be expressed as a mutant here because SQLite requires an upsert's
    # ON CONFLICT target to match a key EXACTLY. Narrow the DDL and the writer's
    # target no longer matches; narrow the writer and it no longer matches the DDL.
    # Either way the ingest raises "ON CONFLICT clause does not match any PRIMARY
    # KEY", which ~109 tests report -- a loud crash rather than the silent overwrite
    # the defect really was. A mutant that measures a crash tells you nothing about
    # whether the suite understands the invariant, and a 109 in the table would
    # read as strong coverage of something it never tested.
    #
    # The mutable half of this defect class IS covered: `broker-stamp` (the writer
    # ignoring its broker argument) and `legs-merge` (a view grouping without it)
    # are both silent, and both are caught.
    Mutant(
        key="held-scope",
        module="history.py",
        find="            \"      AND broker = p.broker)\",",
        replace="            \"      )\",",
        breaks="a lagging broker's held positions would be invisible to the "
               "open/closed decision, so a position still open reads as CLOSED",
    ),
)


@dataclass(slots=True)
class MutationOutcome:
    """What one mutant did to the suite."""

    mutant: Mutant
    #: "measured", or why no number could be trusted.
    status: str
    failed: int = 0
    tests: tuple[str, ...] = ()
    detail: str = ""

    @property
    def uncaught(self) -> bool:
        """Measured, and nothing failed. Judge equivalence before believing it."""
        return self.status == "measured" and self.failed == 0


_PTH = "_editable_impl_optjournal.pth"

#: Tests that fail for EVERY mutant because the mutation removes the text they
#: look for, not because they noticed the defect. Excluded from the count.
#:
#: This inflated every published figure by one before it was spotted: the Money
#: currency gate was reported as "caught by 8 tests" when the real answer is 7,
#: and a disputed `_num` result as 5 when it is 4. A harness that counts its own
#: guard as a catcher overstates coverage everywhere, uniformly, which is the
#: hardest kind of error to notice -- every number looks plausible.
_SELF_REFERENTIAL = frozenset({
    "tests/test_layering.py::test_every_mutant_pattern_still_matches_its_module",
})


def _pytest(clone: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the CLONE's pytest, with the parent environment stripped.

    `env -u PYTHONPATH -u VIRTUAL_ENV` and the clone's own interpreter, never
    `uv run` -- see the module docstring, trap 1.
    """
    return subprocess.run(
        [str(clone / ".venv" / "bin" / "python"), "-m", "pytest",
         "-q", "--tb=no", "-p", "no:cacheprovider", *args],
        capture_output=True, text=True, cwd=str(clone),
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())},
    )


def _prepare(source: Path, clone: Path) -> None:
    """A clone that imports its OWN source.

    Repointing the editable-install `.pth` is trap 2: without it the clone's
    interpreter imports the original repo, and every mutant reads as uncaught.
    """
    if clone.exists():
        shutil.rmtree(clone)
    shutil.copytree(source, clone, symlinks=True)
    for cache in clone.rglob("__pycache__"):
        if ".venv" not in cache.parts:
            shutil.rmtree(cache, ignore_errors=True)
    shutil.rmtree(clone / ".pytest_cache", ignore_errors=True)
    for pth in (clone / ".venv").rglob(_PTH):
        pth.write_text(f"{clone / 'src'}\n", encoding="utf-8")


def _imports_the_clone(clone: Path) -> bool:
    """Trap 3: compare RESOLVED paths, since /tmp is a symlink to /private/tmp."""
    probe = clone / "tests" / "test_zz_mutation_canary.py"
    probe.write_text(
        "import pathlib\n\n"
        "def test_canary():\n"
        "    import optjournal\n"
        "    got = pathlib.Path(optjournal.__file__).resolve()\n"
        f"    want = pathlib.Path({str(clone)!r}).resolve()\n"
        "    assert want in got.parents, f'loaded {got}, not from {want}'\n",
        encoding="utf-8",
    )
    ok = _pytest(clone, str(probe)).returncode == 0
    probe.unlink(missing_ok=True)
    return ok


def run_mutant(mutant: Mutant, *, source: Path, workdir: Path) -> MutationOutcome:
    """Apply one defect in a fresh clone and report which tests failed."""
    clone = workdir / mutant.key
    _prepare(source, clone)

    if not _imports_the_clone(clone):
        return MutationOutcome(mutant, "path-leak",
                               detail="the clone imported the original tree")

    baseline = _pytest(clone)
    if baseline.returncode != 0:
        tail = (baseline.stdout or "").strip().splitlines()[-1:] or [""]
        return MutationOutcome(mutant, "dirty-baseline", detail=tail[0])

    target = clone / "src" / "optjournal" / mutant.module
    text = target.read_text(encoding="utf-8")
    if mutant.find not in text:
        return MutationOutcome(mutant, "stale-mutant",
                               detail=f"pattern not found in {mutant.module}")
    target.write_text(text.replace(mutant.find, mutant.replace, 1), encoding="utf-8")

    result = _pytest(clone)
    # ERROR as well as FAILED. A defect that breaks a FIXTURE is reported by
    # pytest as an error, not a failure -- and counting only FAILED reported such
    # a mutant as caught by NOTHING while a test was in fact catching it, in the
    # assertion that makes the fixture refuse to build. Found exactly that way:
    # `broker-stamp` read as uncaught because the two-broker fixture asserts the
    # second broker's trades landed, so the mutant errored 3 tests instead of
    # failing them. Undercounting is the dangerous direction here, since the
    # whole point of a zero is to send someone looking for a missing test.
    failed = tuple(
        name
        for name in (
            re.sub(r"\s.*$", "", line.split(" ", 1)[1])
            for line in (result.stdout or "").splitlines()
            if line.startswith(("FAILED ", "ERROR "))
        )
        if name not in _SELF_REFERENTIAL
    )
    shutil.rmtree(clone, ignore_errors=True)
    return MutationOutcome(mutant, "measured", failed=len(failed), tests=failed)


def run_all(
    *, source: Path, workdir: Path, only: tuple[str, ...] = ()
) -> list[MutationOutcome]:
    """Every mutant, or the subset named in `only`."""
    wanted = [m for m in MUTANTS if not only or m.key in only]
    workdir.mkdir(parents=True, exist_ok=True)
    outcomes = []
    for mutant in wanted:
        print(f"  {mutant.key} ...", file=sys.stderr, flush=True)
        outcomes.append(run_mutant(mutant, source=source, workdir=workdir))
    return outcomes


@dataclass(slots=True)
class Report:
    outcomes: list[MutationOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """No mutant went unnoticed, and every measurement was trustworthy."""
        return all(o.status == "measured" and o.failed > 0 for o in self.outcomes)


def format_report(outcomes: list[MutationOutcome]) -> str:
    lines = []
    measured = [o for o in outcomes if o.status == "measured"]
    caught = [o for o in measured if o.failed > 0]
    counts = sorted(o.failed for o in caught)
    median = counts[len(counts) // 2] if counts else 0
    lines.append(
        f"{len(caught)}/{len(measured)} defects caught"
        f"  median {median} test(s), max {max(counts) if counts else 0}"
    )
    for o in outcomes:
        if o.status != "measured":
            lines.append(f"  !! {o.mutant.key:16} {o.status}: {o.detail}")
            continue
        mark = "UNCAUGHT" if o.failed == 0 else f"{o.failed:2} test(s)"
        lines.append(f"  {mark:>10}  {o.mutant.key:16} {o.mutant.breaks}")
        if o.failed == 0:
            lines.append("              ^ judge equivalence: if this is a real bug, "
                         "the suite needs a sentinel here")
        elif o.failed <= 3:
            for name in o.tests:
                lines.append(f"              {name}")
    return "\n".join(lines)
