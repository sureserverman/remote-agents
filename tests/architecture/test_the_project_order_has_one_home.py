"""Which order a project list is drawn in is decided in `application`, or nowhere.

DEC-012 put both pickers into decayed-recent-use order and DEC-001 keeps that rule out of the
driver adapters; DEC-053 added a second order and DEC-043 says the two surfaces share the
*rule* and keep their own sentence about it. What all four amount to is one sentence: an
adapter may **ask** which order a catalogue is in, and may never **answer**.

**This exists because the gate check that was supposed to enforce it could not.** Stage 5's
gate spelled the rule as

    ! grep -rn 'def _ranked\\|rank_by_recent_use\\|sorted(.*casefold' src/remote_agents/adapters/

and that pattern has all three failure modes this repo has now hit twice. It runs on the day
somebody runs the plan and never again. It matches **prose** -- at the end of Stage 5 its only
two hits were an unrelated pre-existing scan sort and a *comment* naming the function, so the
check was unpassable as written while its substance plainly held. And it names the functions
that existed when it was authored, so it does not include `rank_if_usage_is_reported` -- the
one the adapters actually call -- and an adapter-local ordering under any new name walks past
it. Raised by this stage's goal evaluator; the same argument as
`test_the_session_destroying_verbs_stay_out.py`, which was written when a gate grep matched the
docstring arguing *against* the thing it forbade.

So the rule is expressed against what an adapter would have to *do*, not against names:

1. The three ordering functions are defined exactly once each, and in `application`.
2. No module under `adapters/` sorts a `CatalogProject`. A module that both imports the type
   and calls `sorted` is holding an ordering rule whatever it has called it. `discovery.py`
   sorts too, and is not caught, correctly: it orders raw `DiscoveredProject` scan results so
   `iterdir` cannot make the catalogue non-deterministic, and never sees a `CatalogProject`.
3. The decay is written down once. `0.5 **` in an adapter is the ranking re-implemented.
"""

from __future__ import annotations

import ast
import pathlib

_SOURCE = pathlib.Path(__file__).resolve().parents[2] / "src" / "remote_agents"
_ADAPTERS = _SOURCE / "adapters"
_HOME = _SOURCE / "application" / "project_catalog.py"
_HOME_RELATIVE = "application/project_catalog.py"

_ORDERING_FUNCTIONS = frozenset(
    {"rank_by_recent_use", "order_alphabetically", "rank_if_usage_is_reported"}
)


def _modules(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _definitions(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def test_each_ordering_function_is_defined_once_and_in_the_application_layer() -> None:
    homes: dict[str, list[str]] = {name: [] for name in _ORDERING_FUNCTIONS}
    for path in _modules(_SOURCE):
        for name in _definitions(path) & _ORDERING_FUNCTIONS:
            homes[name].append(str(path.relative_to(_SOURCE)))

    expected = {name: [_HOME_RELATIVE] for name in _ORDERING_FUNCTIONS}
    wrong = {name: where for name, where in homes.items() if where != [_HOME_RELATIVE]}

    assert homes == expected, (
        f"an ordering rule has grown a second home, or moved out of `application`: {wrong}"
    )


def test_no_adapter_sorts_a_catalogue_project() -> None:
    """A module that both knows the type and sorts is answering, not asking."""
    offenders = []
    for path in _modules(_ADAPTERS):
        source = path.read_text(encoding="utf-8")
        if "CatalogProject" not in source:
            continue
        tree = ast.parse(source)
        sorts = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                getattr(node.func, "id", None) == "sorted"
                or getattr(node.func, "attr", None) == "sort"
            )
        ]
        if sorts:
            offenders.append(f"{path.relative_to(_SOURCE)}:{sorts}")

    assert not offenders, (
        "these adapters order a catalogue themselves; the rule belongs in "
        f"`application/project_catalog.py` and the adapter asks for it (DEC-043): {offenders}"
    )


def test_the_decay_is_written_down_exactly_once() -> None:
    """`0.5 **` anywhere but its one home is the half-life re-implemented."""
    elsewhere = [
        str(path.relative_to(_SOURCE))
        for path in _modules(_SOURCE)
        if path != _HOME and "0.5 **" in path.read_text(encoding="utf-8")
    ]

    assert not elsewhere, f"the recency decay has a second implementation: {elsewhere}"
    assert "0.5 **" in _HOME.read_text(encoding="utf-8"), (
        "the decay is no longer where this test believes it lives, so the sweep above is vacuous"
    )
