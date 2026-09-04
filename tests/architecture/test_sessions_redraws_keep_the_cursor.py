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

_TUI = pathlib.Path(__file__).resolve().parents[2] / "src" / "remote_agents" / "adapters" / "tui"
_SCREENS = _TUI / "screens"

#: The cursor answers a `reload` may give. `rest_on_nothing` is `redraw_after_failure`'s, and
#: it is an answer -- "put it nowhere" -- not an omission.
_CURSOR_ANSWERS = frozenset({"keep_cursor", "rest_on_nothing"})

#: Both of `restore_highlight_by_id`'s cursor parameters. Neither default is right for a
#: sessions pane: `when_gone` defaults to the feed's row-0 fallback, and `was_populated`
#: defaults to True, which reads a first fill as a vanished row.
_REQUIRED_RESTORE_ARGUMENTS = frozenset({"when_gone", "was_populated"})


def _tree(name: str) -> ast.Module:
    return ast.parse((_SCREENS / name).read_text(encoding="utf-8"))


def _every_tui_module() -> dict[str, ast.Module]:
    """Every module under `adapters/tui`, keyed by a path a failure message can name.

    The whole tree and not the two screen modules this file was first written against. A
    sixth redraw exit was living in `app.py` the day this test was written — `show_sessions`
    reloading in place when `ctrl+s` is pressed on the screen it names — and the sweep that
    should have found it grepped `self.reload(` across `sessions.py` and `dashboard.py`. Wrong
    spelling, wrong file, and a check built to make the next arrival visible could not see the
    one already there. A sweep is only as good as its enumeration, so this one enumerates the
    package.
    """
    return {
        str(path.relative_to(_TUI)): ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(_TUI.rglob("*.py"))
    }


def _calls_named(tree: ast.Module, attribute: str) -> list[ast.Call]:
    """Every `<something>.<attribute>(...)` call in the module."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attribute
    ]


def _sessions_position_bodies(tree: ast.Module) -> list[ast.ClassDef]:
    """The class bodies that *are* a sessions listing, by name and by base class.

    `show_choices` is how every screen in this package draws, so sweeping it module-wide would
    flag the wizard and the detail screen, whose row-0 defaults are correct. The predicate is
    the position, not the file.
    """
    named = {"SessionsScreen", "SessionsPaneScreen", "DashboardScreen"}
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and (
            node.name in named
            or any(isinstance(base, ast.Name) and base.id in named for base in node.bases)
        )
    ]


def _enclosing_function(tree: ast.Module, call: ast.Call) -> str:
    """The name of the function a call sits in, for an error a reader can act on."""
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            if any(inner is call for inner in ast.walk(node)):
                return node.name
    return "<module>"


def test_every_sessions_reload_states_its_cursor_answer() -> None:
    """A `reload` that does not name one is an exit whose author never decided.

    Swept across the whole `adapters/tui` package, and by attribute name rather than by
    receiver: `self.reload(...)` and `screen.reload(...)` are the same exit, and only one of
    them was in scope the first time this was written.
    """
    silent = [
        f"{module}::{_enclosing_function(tree, call)}"
        for module, tree in _every_tui_module().items()
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
    # Every `show_choices` reaching a sessions listing, not only the ones inside
    # `_draw_listing`. Scoping to that one function was the third boundary of the same shape
    # as the sweep that missed `app.py::show_sessions`: a future "filter the listing" method
    # calling `self.show_choices(rows)` beside it would default the highlight with all four
    # checks green. `SessionsScreen` and its subclasses are the position; `SessionDetailScreen`
    # and the wizard screens in the same module are not, and their defaults are correct.
    silent = [
        f"{module}::{_enclosing_function(tree, call)}: {ast.unparse(call)[:60]}"
        for module, tree in _every_tui_module().items()
        for holder in _sessions_position_bodies(tree)
        for call in _calls_named(holder, "show_choices")  # type: ignore[arg-type]
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

    **Both parameters, not just the interesting one.** `was_populated` defaults to `True`,
    which is the hazardous value: a caller that passes `when_gone="none"` alone reproduces
    exactly the mount-time defect that flag was added to fix — the pane opening with no cursor
    at all, because a first fill has no held row either. That was a real regression, caught by
    five committed snapshots rather than by a check, which is why it gets a check.
    """
    # Swept across the package and in both spellings — imported by name it is an `ast.Name`,
    # reached through a module it is an `ast.Attribute` — so neither a move to another module
    # nor a change of import style silently drops the check.
    # Sessions positions only. The feed calls this helper too and correctly takes both
    # defaults -- `when_gone="first"` *is* its answer, because a notification ages out on its
    # own schedule and its pane binds nothing destructive. A sweep that demanded every caller
    # state both would be demanding the feed restate the default as though it were a decision,
    # which is the opposite of what this file asks for. (Found by widening the sweep: it went
    # red on `feed.py` immediately, which is the check working and the predicate being wrong.)
    calls = [
        call
        for _module, tree in _every_tui_module().items()
        for holder in _sessions_position_bodies(tree)
        for call in _calls_named(holder, "restore_highlight_by_id")  # type: ignore[arg-type]
        + [
            node
            for node in ast.walk(holder)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "restore_highlight_by_id"
        ]
    ]

    # Fails **closed**. Written against `dashboard.py` alone, an empty call list made `silent`
    # empty and the assertion passed vacuously: renaming the helper or moving the pane's
    # drawing elsewhere would have retired this check without anyone deciding to.
    assert calls, (
        "no sessions position calls `restore_highlight_by_id` any more. Either the helper was "
        "renamed, or the pane's drawing moved to a class this sweep does not recognise as a "
        "sessions position -- both retire this check, and both need a reader, not a green tick."
    )
    silent = [
        # `- named` and not `and named`: a call passing no keywords at all is the most silent
        # call there is, and an `and` here would short-circuit it out of its own check.
        sorted(missing)
        for call in calls
        if (missing := _REQUIRED_RESTORE_ARGUMENTS - {kw.arg for kw in call.keywords})
    ]

    assert not silent, (
        "the dashboard's sessions pane restores its cursor without stating the whole answer: "
        f"{sorted(silent)}. `when_gone` defaults to the feed's answer (row 0), which DEC-062 "
        "removed from the sessions positions; `was_populated` defaults to True, which reads a "
        "first fill as a vanished row and opens the pane with no cursor."
    )


def test_every_listing_redraw_is_reached_through_a_checked_call() -> None:
    """`_draw_listing` is called directly as well as through `reload`, and both are exits.

    The `reload` sweep above cannot see a direct call, and there are two (`after_command` and
    `redraw_after_failure`'s neighbours). They are the same class of redraw exit this file
    exists to catch, so they state their answer the same way.
    """
    silent = [
        f"{module}::{_enclosing_function(tree, call)}"
        for module, tree in _every_tui_module().items()
        for call in _calls_named(tree, "_draw_listing")
        if not _CURSOR_ANSWERS & {keyword.arg for keyword in call.keywords if keyword.arg}
    ]

    assert not silent, (
        f"these direct redraws of the sessions listing take the default cursor: {sorted(silent)}. "
        "Pass keep_cursor= or rest_on_nothing= explicitly, as every other exit does."
    )


def _is_trivially_none(function: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    """Whether a resolver is the base class's "this screen has no sessions list" answer."""
    body = [
        node
        for node in function.body
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    ]
    return (
        len(body) == 1 and isinstance(body[0], ast.Return) and ast.unparse(body[0]) == "return None"
    )


def test_a_screen_that_resolves_a_session_declares_that_it_owns_a_cursor() -> None:
    """`highlighted_session` and `owns_session_cursor` are two halves of one statement.

    `RemoteAgentsTui.selected_session` asks `owns_session_cursor` which of two answers a
    keypress gets: this screen's own cursor, or the console's published selection. A position
    that draws sessions and defines the resolver but forgets the flag answers from the
    *sessions pane's* row while its own cursor sits somewhere else — an unconfirmed `s` against
    a session the owner can see is not the one they highlighted.

    Written as a check rather than trusted because the Stage 1 gate paid for the lesson twice:
    a predicate that recognises today's positions is silent about tomorrow's, and both times
    the gap was at exactly this kind of boundary. `ChoiceScreen` is the base and declares both,
    which is the pairing rather than an exception to it.
    """
    unpaired = []
    for module, tree in _every_tui_module().items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            resolver = next(
                (
                    child
                    for child in node.body
                    if isinstance(child, ast.AsyncFunctionDef | ast.FunctionDef)
                    and child.name == "highlighted_session"
                ),
                None,
            )
            # `AsyncFunctionDef` too: matching only the sync spelling let an `async def`
            # override slip past. A body that is only a docstring and `return None` is the
            # base's "this screen has no sessions list", not a position claiming one, so it is
            # not asked for the flag.
            defines_resolver = resolver is not None and not _is_trivially_none(resolver)
            declares_flag = any(
                isinstance(child, ast.AnnAssign | ast.Assign)
                and "owns_session_cursor" in ast.unparse(child)
                for child in node.body
            )
            if defines_resolver and not declares_flag:
                unpaired.append(f"{module}::{node.name}")

    assert not unpaired, (
        f"these screens resolve a session from their own list without declaring that they own "
        f"a cursor: {sorted(unpaired)}. Set `owns_session_cursor = True`, or "
        "`selected_session` will answer them from another pane's selection."
    )


#: The only functions allowed to publish a console selection, and what each is for.
#:
#: `_publish_selection` is the funnel every publication goes through and the one place the
#: capability is reached; the other three are its callers. A fourth caller is not forbidden
#: because publishing is dangerous — it is forbidden because the set of moments that *mean*
#: "the owner chose this" is small and closed, and a publication from anywhere else is the
#: surface asserting a choice nobody made.
_PUBLISHERS = frozenset(
    {
        # The slot: takes a value, coalesces, and asks for a write.
        "_publish_selection",
        # The writer: the *only* function that reaches `console_publish_selection`, holding the
        # lock that makes "last value wins" true. A second reacher would be a second
        # unsynchronised writer, which is the defect the lock exists for.
        "_write_selection",
        # The three moments that mean the owner chose something.
        "_draw_listing",
        "on_option_list_option_highlighted",
        "on_unmount",
    }
)


def test_the_selection_is_published_from_a_closed_set_of_moments() -> None:
    """Three callers and one funnel, enumerated by parsing rather than by grepping a line.

    The gate check this replaces was `! grep -rn 'publish_selection(' ... | grep -v '<names>'`,
    which cannot pass for any correct implementation: it filters *lines* by the names of
    *enclosing functions*, and a call line never contains the name of the function it sits in.
    That is the same grep-versus-parse fragility this file was created for, one check later, so
    the repair is the same one.
    """
    offenders = []
    for module, tree in _every_tui_module().items():
        for holder in (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        ):
            if holder.name in _PUBLISHERS:
                continue
            for node in ast.walk(holder):
                rendered = ast.unparse(node)
                # Two spellings, and the second is the one the first version could not see.
                # Calling the hook is the ordinary route; reaching `console_publish_selection`
                # **directly** bypasses the slot, the lock and the coalescing entirely, so such
                # a site is not merely a fourth publisher but a second unsynchronised writer.
                # The docstring claimed `_publish_selection` was "the one place the capability
                # is reached" and nothing checked it -- the clause that should have was dead,
                # gated on names that were already exempt. Found by a gate evaluator.
                if isinstance(node, ast.Call) and rendered.startswith("self._publish_selection("):
                    offenders.append(f"{module}::{holder.name} (calls the hook)")
                elif isinstance(node, ast.Attribute) and rendered.endswith(
                    "console_publish_selection"
                ):
                    offenders.append(f"{module}::{holder.name} (reaches the capability)")

    assert not offenders, (
        f"these publish a console selection from outside the closed set: {sorted(offenders)}. "
        f"Publication means the owner chose a session; the moments that mean that are "
        f"{sorted(_PUBLISHERS)} and nothing else."
    )
