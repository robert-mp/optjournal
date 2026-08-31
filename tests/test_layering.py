"""The import rules the README states, asserted over the real import graph.

Two of those rules had no test. `test_analysis.py` and `test_money.py` each pin
their own module's leaf status, which is the pattern this generalises -- but the
two rules the README calls load-bearing for the journal's credibility were
documented only in prose:

* **The modelled-number quarantine.** Every figure the accounting layers report
  is broker-stated. `blackscholes.py` breaks that on purpose and is confined to
  the replay panel, so no headline number, calendar day or annual row can be
  traced to a model. "Nothing counts until the position is flat" is worth little
  if a modelled figure can reach the same card, and the only thing standing
  between the two is which modules may import it.

* **Imports point one way.** A cycle would not merely be untidy: this package is
  loaded by a cron through a by-path shim under an interpreter with no py_ibkr,
  so an import that doubles back is how a module acquires a dependency the real
  runtime does not have.

Both are checked with `ast` over the source rather than by importing, so a rule
is enforced even for a module the suite never imports.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from conftest import ROOT

PACKAGE = ROOT / "src" / "optjournal"

#: Modules that may import `blackscholes`. `replay` IS the modelled layer -- the
#: band, the marks and the effective delta the panel captions as modelled -- and
#: `demo` prices its synthetic contracts, which exist only in a database that
#: refuses to hold a real statement.
#:
#: `bars` was here while it held both halves, and dropping it is the point of the
#: split rather than a side effect: this set is module-granular, so it could not
#: distinguish "the replay layer models" from "the module that also owns the
#: manifest and the upsert models". Nothing about a bar window needs a price
#: model, and now nothing in `bars.py` can reach one.
MAY_MODEL = {"replay", "demo"}

#: Modules that must import nothing from the package. Each is a value type or
#: pure arithmetic that any layer may hold without acquiring a direction.
#:
#: `vol` and `trend` are the watchlist's two arithmetic leaves, and they are here
#: rather than in `MAY_MODEL` on purpose. Realised vol is a standard deviation of
#: log returns and B-Xtrender is two exponential means, a difference and Wilder's
#: RSI -- all of it arithmetic over broker-stated closes, nothing solved -- so the
#: watchlist can grow derived columns without the quarantine growing with it.
#: `trend.py` is the most recent module that could have been used to argue for
#: widening `MAY_MODEL`, and the count above is still two.
#:
#: `iv` is the third watchlist leaf and the one that looks like it belongs in
#: `MAY_MODEL` and does not. It carries an IMPLIED volatility, which `vol.py`'s
#: docstring says is unreachable here -- but what is unreachable is an implied vol
#: this journal would SOLVE. `iv.py` solves nothing: it reads three numbers CBOE
#: publishes (a 30-day implied vol and the high and low of that same series over
#: the trailing year) and divides. The quarantine is a rule against modelling, not
#: against the word "implied", so the count above is still two.
#:
#: `analysis` is not here: it holds `notes`, which is itself a leaf. That is the
#: point of a leaf -- any layer may hold one without acquiring a direction -- and
#: the rule it needs, reading IBKR note codes as whole tokens, is shared with
#: `history` on the far side of the graph. `IMPORTS_LEAVES_ONLY` states the
#: weaker property that still holds: it depends on nothing that reads a database.
LEAVES = {"money", "notes", "blackscholes", "clock", "config", "marketdata",
          "compat", "fills", "events", "vol", "trend", "iv", "locks", "logs"}

#: Modules that may import leaves and nothing else. Weaker than `LEAVES` and
#: load-bearing for the same reason: `analysis.py` is pure statement mathematics,
#: testable against a hand-built statement, and it stays that way only while
#: everything it imports is a value type. The specific temptation is `stats`,
#: whose single-currency gate it would like for its per-currency ledgers --
#: `serialize` applies that instead, being the layer that already holds both.
#:
#: `campaigns` is here rather than in `LEAVES` because a campaign's outcome IS a
#: `Money` -- it cannot answer "did this decision win" without one. It holds no
#: episode type either: everything it reads off an episode is duck-typed, which
#: is what keeps every case in it testable against a literal instead of a
#: database, and what stops the win rate's own rule from depending on `history`.
IMPORTS_LEAVES_ONLY = {"analysis", "campaigns"}


def _internal_imports(path: Path) -> set[str]:
    """The `optjournal.*` modules `path` imports, by bare module name."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "optjournal"
        ):
            found.add((node.module or "").split(".")[-1])
        elif isinstance(node, ast.Import):
            found |= {
                a.name.split(".")[-1]
                for a in node.names
                if a.name.startswith("optjournal")
            }
    return found


def _graph() -> dict[str, set[str]]:
    modules = {p.stem: p for p in PACKAGE.glob("*.py") if p.stem != "__init__"}
    return {
        name: {dep for dep in _internal_imports(path) if dep in modules}
        for name, path in modules.items()
    }


def test_only_the_replay_layer_may_import_blackscholes():
    """The quarantine that keeps every reported figure broker-stated.

    A serializer, a stats module or a renderer importing this would put a
    modelled number one call away from a headline card, and nothing on the page
    would say so -- the exact failure the panel's "modelled" caption exists to
    prevent.
    """
    offenders = sorted(
        name for name, deps in _graph().items()
        if "blackscholes" in deps and name not in MAY_MODEL
    )
    assert not offenders, (
        f"{offenders} import blackscholes. Modelled numbers reach the replay "
        "panel only, through bars.py -- see the README's 'Modelled numbers'. If a "
        "figure genuinely needs a model, it needs a caption saying so first."
    )


@pytest.mark.parametrize("module", sorted(LEAVES))
def test_a_leaf_imports_nothing_from_the_package(module):
    """Every layer may hold a leaf, so a leaf may hold nothing.

    Parametrized rather than one test per module because the rule is identical
    and the list is the interesting part: adding a module here is how a new
    value type declares itself a leaf.
    """
    deps = sorted(_internal_imports(PACKAGE / f"{module}.py"))
    assert not deps, (
        f"{module}.py imports {deps}, but it is a leaf: every layer holds one, so "
        "it can depend on nothing. Apply the shared rule in whichever module "
        "already imports both, or extract it into a leaf they can both hold."
    )


@pytest.mark.parametrize("module", sorted(IMPORTS_LEAVES_ONLY))
def test_a_leaf_holder_imports_only_leaves(module):
    """Pure arithmetic may hold value types, and nothing that reads a database.

    The property being defended is testability against a hand-built input: the
    moment one of these imports a layer that opens SQLite or parses XML, a test
    for it needs a fixture rather than a literal, and that is how a module of
    plain mathematics acquires a runtime.
    """
    held = sorted(_internal_imports(PACKAGE / f"{module}.py"))
    assert set(held) <= LEAVES, (
        f"{module}.py imports {sorted(set(held) - LEAVES)}, which are not leaves. "
        "It may hold value types only -- anything else gives it a dependency on a "
        "layer that reads a database, and costs it the property that it can be "
        "tested against a literal."
    )


def test_the_import_graph_is_acyclic():
    """A cycle costs more here than tidiness.

    The cron loads this package's CLI through a by-path shim under MeshClaw's
    interpreter, which has no py_ibkr, so an import doubling back is how a module
    picks up a dependency the real runtime lacks. Reported as the actual path,
    since "there is a cycle" is not actionable.
    """
    graph = _graph()
    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(graph, WHITE)
    cycles: list[list[str]] = []

    def walk(node: str, path: list[str]) -> None:
        colour[node] = GREY
        for dep in sorted(graph[node]):
            if colour[dep] == GREY:
                cycles.append([*path, node, dep])
            elif colour[dep] == WHITE:
                walk(dep, [*path, node])
        colour[node] = BLACK

    for module in sorted(graph):
        if colour[module] == WHITE:
            walk(module, [])

    assert not cycles, "import cycles: " + "; ".join(" -> ".join(c) for c in cycles)


def test_the_journal_holds_only_the_database():
    """The irreplaceable table's module may not depend on the derivable ones.

    `journal_entries` is the only table in this database that a re-ingest cannot
    rebuild, and everything it attaches to -- campaigns, episodes, statistics --
    IS rebuilt, on every ingest. So the dependency runs one way only: the layer
    that derives may read the journal, and the journal may not read the layer
    that derives. `orphans()` is handed the live anchors for exactly this reason,
    where computing them itself would have been shorter.

    Not `IMPORTS_LEAVES_ONLY`, because `db` is not a leaf. The property here is
    narrower and about direction rather than purity: `journal.py` can open
    SQLite, and must not know how a campaign is assembled.
    """
    held = sorted(_internal_imports(PACKAGE / "journal.py"))
    assert set(held) <= {"db", *LEAVES}, (
        f"journal.py imports {sorted(set(held) - {'db', *LEAVES})}. The one table "
        "that cannot be re-derived must not depend on the layers that are: pass "
        "what it needs in, as `orphans()` does with the live anchors."
    )


def test_the_leaf_list_names_only_real_modules():
    """A stale entry here would silently stop enforcing anything."""
    missing = sorted(
        name for name in LEAVES | MAY_MODEL if not (PACKAGE / f"{name}.py").is_file()
    )
    assert not missing, f"these are not modules: {missing}"


# --- the broker seam speaks one vocabulary ------------------------------------

#: Words that are a specific broker's, not the trade's. A field NAME on a seam
#: shape may not contain one: the whole point of `fills.py` is that a second
#: broker fills these fields from its own vocabulary, so a field called `conid`
#: asks a broker that has never heard the word to populate it.
#:
#: `conid` is IBKR's. It stayed on the seam until PLAN.md task 8 step 2, then moved
#: to `contract_id` -- the DATABASE columns are still `conid`, deliberately, and
#: that asymmetry lives in `ingest.py` alone. This test guards the seam side only.
VENDOR_WORDS = ("conid", "ibkr", "ib_", "fifo_pnl", "flex")


def test_no_seam_field_carries_a_brokers_own_vocabulary():
    """The invariant `fills.py` exists for, asserted rather than described.

    Nothing else enforces it. The docstring says the fields cannot carry the first
    broker's words, and for four months `conid` did -- with the reason written down
    beside it, which is a comment, not a guard. A field added in a hurry to a
    frozen dataclass is exactly how the next one arrives, and it would arrive
    silently: the suite would pass, the payload would be fine, and the seam would
    quietly be IBKR-shaped again.

    Read from the source with `ast`, so it holds for shapes no test constructs --
    which is all of them: only `sources.py` builds these.
    """
    tree = ast.parse((PACKAGE / "fills.py").read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.AnnAssign) or not isinstance(
                stmt.target, ast.Name
            ):
                continue
            field = stmt.target.id
            for word in VENDOR_WORDS:
                if word in field.lower():
                    offenders.append(f"{node.name}.{field} contains {word!r}")

    assert not offenders, (
        "broker-specific vocabulary on the seam: " + "; ".join(offenders) + ". "
        "These shapes are what a SECOND broker fills in; a field named after the "
        "first broker's word asks it for something it has no name for. Rename to "
        "the trading term and translate in ingest.py, which is where the database "
        "column names already differ."
    )


def test_ingest_is_the_only_place_the_two_vocabularies_meet():
    """The seam says `contract_id`, the schema says `conid`, and that is contained.

    Task 8 steps 3-5 (the schema and the payload) are deliberately not done -- see
    PLAN.md. What makes deferring them safe rather than merely cheap is that the
    mismatch is confined to `ingest.py`, whose SQL names the column while its
    values read the attribute. If a second module starts translating, the rename
    stops being a schema change and becomes a hunt.

    Passes trivially today. It is here to fail on the commit that would spread it.
    """
    translating = []
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name in {"ingest.py", "fills.py", "sources.py"}:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            # `x.contract_id` outside the seam means someone else is holding a
            # seam shape, and the only module that should is ingest.
            if isinstance(node, ast.Attribute) and node.attr in (
                "contract_id", "underlying_contract_id"
            ):
                translating.append(f"{path.name}:{node.lineno}")

    assert not translating, (
        f"seam attributes read outside ingest.py at {translating}. The seam-to-"
        f"schema translation is meant to live in one file so the eventual column "
        f"rename stays a schema change; see PLAN.md task 8."
    )


# --- the mutation harness itself ---------------------------------------------


def test_every_mutant_pattern_still_matches_its_module():
    """A mutant whose `find` no longer appears mutates nothing.

    This is the harness's own silent-failure mode, and the one that matters most:
    refactor the code a mutant targets and it stops testing anything, while
    `optjournal mutate` keeps printing a reassuring "caught" line for every
    OTHER defect. `run_mutant` reports `stale-mutant` at runtime, but only for a
    mutant someone actually runs -- this fails in the ordinary suite.

    Found by using it: the commission mutant originally patched the CALL SITE of
    `_commission_base`, so the unit test on the rule itself could not see it. It
    reported 1 test where the truth was 2, which reads as thinner coverage than
    exists.
    """
    from optjournal.mutate import MUTANTS

    stale = []
    for mutant in MUTANTS:
        source = (PACKAGE / mutant.module).read_text(encoding="utf-8")
        if mutant.find not in source:
            stale.append(f"{mutant.key} (pattern absent from {mutant.module})")
    assert not stale, (
        "these mutants no longer match their target and would mutate nothing: "
        f"{stale}. Update the pattern or drop the mutant."
    )


def test_no_mutant_is_a_no_op():
    """`find` and `replace` must differ, or the mutant proves nothing.

    A mutant that replaces text with itself reports "caught by 0 tests" for a
    defect that was never injected -- the worst possible output, because it looks
    like an unguarded invariant.
    """
    from optjournal.mutate import MUTANTS

    noops = [m.key for m in MUTANTS if m.find == m.replace]
    assert not noops, f"these mutants change nothing: {noops}"
