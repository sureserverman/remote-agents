"""No redraw of a sessions listing decides the cursor by default; each one says what it wants.

The wrong-highlight defect was five separate exits found one at a time, each one measured
through a wrong action rather than caught by a check: `after_command` after a stop,
`redraw_after_failure` after one that raised, `confirm_force`'s abort, `ChoiceScreen.refuse`,
and `refresh_contents` on Ctrl+R. Four were closed by moving the fix to the `on_reveal` funnel;
the fifth was not, because Refresh does not navigate and so never reaches that funnel. The
pattern is the point: the funnel closes every exit arriving by *navigation*, which is a smaller
class than the one the hazard belongs to, and nothing said so until a sixth would have arrived
the same way.

So this does not assert that any particular exit keeps the cursor -- that is what the unit
tests for each one do. It asserts the weaker, checkable property that makes a sixth arrival
visible: **an exit must state its answer**. A call that omits `keep_cursor` is not wrong
because the default is wrong; it is wrong because the author never had to decide, and this
listing is the one where `s` and `c` end a session with no confirmation against the row under
the cursor (DEC-018, DEC-052, DEC-062).

Read by parsing rather than by grepping, for the reason the console key-budget test records:
a gate `grep` passes on the day it is written and reports a defect that is not one the moment a
path filter goes stale. This runs on every commit.
"""

from __future__ import annotations

import ast
import pathlib

_SCREENS = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src"
    / "remote_agents"
    / "adapters"
    / "tui"
    / "screens"
)

#: The cursor answers a `reload` may give. `rest_on_nothing` is `redraw_after_failure`'s, and
#: it is an answer -- "put it nowhere" -- not an omission.
_CURSOR_ANSWERS = frozenset({"keep_cursor", "rest_on_nothing"})


def _tree(name: str) -> ast.Module:
    return ast.parse((_SCREENS / name).read_text(encoding="utf-8"))


def _calls_named(tree: ast.Module, attribute: str) -> list[ast.Call]:
    """Every `<something>.<attribute>(...)` call in the module."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attribute
    ]


def _enclosing_function(tree: ast.Module, call: ast.Call) -> str:
    """The name of the function a call sits in, for an error a reader can act on."""
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            if any(inner is call for inner in ast.walk(node)):
                return node.name
    return "<module>"


def test_every_sessions_reload_states_its_cursor_answer() -> None:
    """A `reload` that does not name one is an exit whose author never decided."""
    tree = _tree("sessions.py")
    silent = [
        _enclosing_function(tree, call)
        for call in _calls_named(tree, "reload")
        if not _CURSOR_ANSWERS & {keyword.arg for keyword in call.keywords if keyword.arg}
    ]

    assert not silent, (
        "these reloads of the sessions listing take the default cursor instead of stating "
        f"one: {sorted(silent)}. Pass keep_cursor= (or rest_on_nothing=) explicitly, even "
        "where the default is what you want -- a first fill has no cursor to keep and should "
        "say so."
    )


def test_every_listing_fill_states_its_highlight() -> None:
    """`_draw_listing` is the funnel every sessions row is drawn through; its exits are three.

    Named exactly rather than counted, so adding a fourth is a change to this list and a reader
    is told which answers already exist: rest on nothing, keep the row the owner held, or the
    first fill's row 0.
    """
    tree = _tree("sessions.py")
    drawing = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_draw_listing"
    )
    silent = [
        ast.unparse(call)
        for call in _calls_named(drawing, "show_choices")  # type: ignore[arg-type]
        # The empty-listing exit draws no rows at all, so it has no highlight to state.
        if call.args
        and not (call.args[0].__class__ is ast.Tuple and not call.args[0].elts)
        and "highlight" not in {keyword.arg for keyword in call.keywords}
    ]

    assert not silent, (
        f"these fills of the sessions listing take the default highlight: {silent}. "
        "`show_choices` defaults to row 0, which on a list that just lost a row is a "
        "different session (DEC-052, DEC-062)."
    )


def test_the_dashboards_sessions_pane_states_its_vanished_row_answer() -> None:
    """The other sessions position, restoring through the shared helper rather than a fill.

    `restore_highlight_by_id` defaults to `when_gone="first"` because the feed wants it, so the
    dashboard's silence would read as the feed's answer on a pane whose rows are sessions.
    """
    tree = _tree("dashboard.py")
    silent = [
        _enclosing_function(tree, call)
        for call in _calls_named(tree, "restore_highlight_by_id")
        if "when_gone" not in {keyword.arg for keyword in call.keywords}
    ]
    # `restore_highlight_by_id` is imported by name, so it is a plain Call, not an Attribute.
    silent += [
        _enclosing_function(tree, node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "restore_highlight_by_id"
        and "when_gone" not in {keyword.arg for keyword in node.keywords}
    ]

    assert not silent, (
        "the dashboard's sessions pane restores its cursor without stating what a vanished "
        f"row does: {sorted(silent)}. The helper's default is the feed's answer (row 0), "
        "which DEC-062 removed from the sessions positions."
    )
