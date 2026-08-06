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

#: Modules that may import `blackscholes`. `bars` owns the modelled series the
#: replay panel draws; `demo` prices its synthetic contracts, which exist only
#: in a database that refuses to hold a real statement.
MAY_MODEL = {"bars", "demo"}

#: Modules that must import nothing from the package. Each is a value type or
#: pure arithmetic that any layer may hold without acquiring a direction.
LEAVES = {"money", "analysis", "blackscholes", "config", "marketdata", "compat", "fills"}


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
        "already imports both."
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


def test_the_leaf_list_names_only_real_modules():
    """A stale entry here would silently stop enforcing anything."""
    missing = sorted(
        name for name in LEAVES | MAY_MODEL if not (PACKAGE / f"{name}.py").is_file()
    )
    assert not missing, f"these are not modules: {missing}"


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
