"""Renaming a session is written once, and so is the rule for what a name may be.

**This file adds no behaviour and pins none.** Both surfaces' rename journeys were already
covered end to end before it existed — the bot's four in `tests/e2e/test_telegram_fake_backend.py`
(applies and redraws, refuses a name the rule rejects without calling the store, skip leaves
the session alone, a session that ended under the owner lands on the list) and the local
surface's fourteen in `tests/unit/adapters/tui/test_tui_rename.py` (including both halves of
the repeat guard and the same vanished-session case). What none of them can see is the shape
this sub-plan exists to protect: *how many implementations* there are behind those journeys.
Every one of them would stay green on the day a frontend grew a second one.

The consolidation itself already happened. `75c86b6` — sub-plan 1's "the bot takes a Backend,
and asks nothing by name" — removed `rename = getattr(self.launcher, "rename", None)` and put
the bot on `SessionService.rename`, which is where the local surface already was. So what is
left to do for rename is not a merge but a guard, and the guard is the part that was missing.

**Three things are deliberately not shared, and the sweeps below are written not to demand
them.** Each is a place where one rule serves two surfaces that must still speak for
themselves:

- *How an owner declines.* The bot reads the literal `skip`; the local surface reads an empty
  entry. Giving the bot the blank rule would silently turn a mistyped empty reply into a
  decline, where today it re-asks.
- *How a rejection is worded.* "Use a visible name of up to N characters." against "use a
  visible label of up to N characters". One rule, two sentences, sized for a chat message and
  for a form.
- *How a repeat is dropped.* The bot claims a mutation token (DEC-008, DEC-011); the local
  surface checks `showing` and `tui.busy` (its two windows are different, and its own tests
  say why). Different mechanisms answering DEC-008 on different pumps.

That is the same division `application/stops.py` already records for a vanished record, and
the same one Task 3.1 made for a binary capture: the *decision* is shared, the *sentence* is
the surface's.

**The sweeps look for shapes, not names.** Stage 2 of this sub-plan paid for the other kind:
its guard forbade `def session_row` and `def selectable_area`, saw both, and was blind to the
ENDED filter because that twin had only ever been an anonymous comprehension. A second rename
would not arrive called `rename` — it would arrive as a frontend reaching past the use case to
`set_label`, which is the store verb `SessionService.rename` is built from. So that is what is
swept for, alongside the name.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[3] / "src" / "remote_agents"
_FRONTENDS = (_SRC / "adapters" / "telegram", _SRC / "adapters" / "tui")


def _frontend_sources() -> list[pathlib.Path]:
    """Every module of the two driver adapters, or a failure rather than an empty sweep."""
    for tree in _FRONTENDS:
        assert tree.is_dir(), f"{tree} must exist or these sweeps pass over nothing"
    return sorted(path for tree in _FRONTENDS for path in tree.rglob("*.py"))


def _trees() -> list[tuple[pathlib.Path, ast.Module]]:
    return [(path, ast.parse(path.read_text())) for path in _frontend_sources()]


def _where(path: pathlib.Path, node: ast.AST) -> str:
    return f"{path.relative_to(_SRC.parent.parent)}:{node.lineno}"


def test_no_frontend_defines_a_rename_of_its_own() -> None:
    """`SessionService.rename` is the implementation; a frontend may only call it."""
    offenders = [
        _where(path, node)
        for path, tree in _trees()
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "rename"
    ]

    assert offenders == [], f"rename is implemented outside the application layer: {offenders}"


def test_no_frontend_reaches_past_the_use_case_to_the_store_verb() -> None:
    """The shape a second rename would actually arrive in, having no need of the name.

    `set_label` is what `SessionService.rename` calls once it holds the session lock and has
    re-read the record (DEC-007). A frontend calling it directly would rename a session
    without either, and would do so while every journey test above stayed green.
    """
    offenders = [
        _where(path, node)
        for path, tree in _trees()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "set_label"
    ]

    assert offenders == [], f"a frontend renames without the lock or the re-read: {offenders}"


def test_the_label_rule_is_defined_once_and_lives_in_the_domain() -> None:
    """One `normalize_label`, and it is not in an adapter."""
    definitions = [
        f"{path.relative_to(_SRC.parent.parent)}:{node.lineno}"
        for path in sorted(_SRC.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == "normalize_label"
    ]

    assert definitions == ["src/remote_agents/domain/models.py:22"], definitions


def test_no_frontend_re_derives_a_clause_of_the_label_rule() -> None:
    """The rule's own vocabulary, absent from both frontends because they ask for it instead.

    `isprintable` is the clause that would be re-derived first and noticed last: whitespace
    collapsing and the length bound are visible in a diff, while a re-implemented printability
    check reads as ordinary defensive tidying. `adapters/tmux/profiles.py` uses it too and is
    outside this sweep on purpose — trimming a captured pane line to 160 characters is not the
    label rule, and widening the sweep to catch it would make the guard mean something else.
    """
    offenders = [
        _where(path, node)
        for path, tree in _trees()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "isprintable"
    ]

    assert offenders == [], f"a frontend states the label rule rather than asking: {offenders}"


@pytest.mark.parametrize(
    ("frontend", "expected"),
    [("telegram", 1), ("tui", 1)],
)
def test_each_frontend_calls_the_one_rename_exactly_once(frontend: str, expected: int) -> None:
    """Both surfaces reach it, and neither reaches it twice.

    Parametrized per frontend rather than summed, so losing a surface's only call site fails
    as its own case instead of being absorbed by the other's.
    """
    tree_root = _SRC / "adapters" / frontend
    calls = [
        f"{path.relative_to(_SRC.parent.parent)}:{node.lineno}"
        for path in sorted(tree_root.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "rename"
    ]

    assert len(calls) == expected, f"{frontend} rename call sites: {calls}"
