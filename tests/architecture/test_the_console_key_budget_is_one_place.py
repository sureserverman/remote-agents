"""The console's root keys are declared in one place and installed from one function.

Every root binding (`bind-key -n`) is a key **every agent on this server can never receive**,
in every session, for as long as it is bound. That makes the size of this set a decision
rather than an implementation detail, and a decision is only reviewable if it lives somewhere
a reader can find it.

Two properties, and neither is provable by reading one file:

* **One builder.** `codec.console_binding_args` must be the only place a `bind-key` argv is
  constructed. Anywhere else, a key could be taken without passing the validation, the socket
  scoping, or the escaping that builder carries.
* **One declaration.** `application/console.CONSOLE_BINDINGS` must be the only place a key
  *string* is chosen. A second list would be a second budget, and nothing would notice.

This exists because the Stage 2 gate check for it was a `grep` whose path filter went stale
the moment Task 2.1 moved the argv from the gateway into the codec — a check that passed for
a day and then reported a defect that was not one. A gate check runs on the day somebody runs
the plan; this runs on every commit.
"""

from __future__ import annotations

import ast
import pathlib

_SOURCE = pathlib.Path(__file__).resolve().parents[2] / "src" / "remote_agents"


#: The tmux verbs the fold sends. Argv literals, not prose — the distinction this file exists
#: for: the Stage 2 gate check that swept for these is a `grep`, so it matches the comments
#: that *explain* the layout as readily as the code that builds it, and it reports a defect
#: that is not one. What it means is asserted here instead, where it runs on every commit.
_FOLD_VERBS = ("resize-pane", "select-pane")


def _modules_with_an_argv_literal(*wanted: str) -> set[str]:
    """Every module using one of these strings as a value, ignoring docstrings."""
    found = set()
    for path in _SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            node.body[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef | ast.ClassDef | ast.Module)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in wanted and node not in docstrings:
                found.add(str(path.relative_to(_SOURCE)))
    return found


def test_only_the_codec_builds_the_verbs_the_fold_sends() -> None:
    """`resize-pane` and `select-pane` are argv, and DEC-001 says argv is built in one place.

    The fold reaches four live pane processes and a displayed agent, so a resize aimed from
    somewhere that never learned the console's targeting is a wedged console rather than an
    untidy one — the reason the plan gated on this at all. Asserted as *values* rather than by
    grep so a comment naming a verb stays a comment.
    """
    building = _modules_with_an_argv_literal(*_FOLD_VERBS)

    assert building <= {"adapters/tmux/codec.py"}, (
        f"{sorted(building)} assemble tmux pane geometry themselves. Every tmux argv in this "
        "tree is codec-built (DEC-001): the codec validates the target, scopes it to our "
        "socket, and is the one place a pane id is turned into an exact target."
    )


def _modules_building_a_bind_key() -> set[str]:
    """Every module with `bind-key` as an argv string literal, ignoring prose."""
    found = set()
    for path in _SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            node.body[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef | ast.ClassDef | ast.Module)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and node.value == "bind-key"
                and node not in docstrings
            ):
                found.add(str(path.relative_to(_SOURCE)))
    return found


def test_exactly_one_module_builds_a_root_binding() -> None:
    building = _modules_building_a_bind_key()

    assert building == {"adapters/tmux/codec.py"}, (
        f"`bind-key` argv is built in {sorted(building)}. Every root binding takes a key from "
        "every agent on this server forever, so the argv is built in one place that validates "
        "the key, scopes it to our socket, and escapes what tmux would otherwise expand."
    )


def test_the_key_budget_is_declared_in_one_place() -> None:
    """The keys themselves, not the argv — a second list would be a second budget."""
    from remote_agents.application.console import CONSOLE_BINDINGS

    declared = {binding.key for binding in CONSOLE_BINDINGS}
    assert declared, "the budget is empty; nothing would install a route back"

    module = (_SOURCE / "application" / "console.py").read_text(encoding="utf-8")
    for key in declared:
        assert module.count(f'"{key}"') >= 1, f"{key} is not declared where the budget lives"

    # And nothing outside that declaration hands a key to the installer.
    installer_arguments = set()
    for path in _SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "install_console_binding"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                installer_arguments.add(node.args[0].value)
    assert installer_arguments == set(), (
        f"a key literal {sorted(installer_arguments)} is passed straight to the installer, "
        "bypassing CONSOLE_BINDINGS — which is where the budget is supposed to be decided."
    )


def test_the_root_budget_is_still_one_key_and_the_prefix_layer_costs_none() -> None:
    """The budget is about `bind-key -n`, and Stage 4 added eight keys without touching it.

    **The distinction this asserts is the whole reason eight new keys were affordable.** A root
    binding is a key every agent on this server can never receive; a prefix binding costs an
    agent nothing, because tmux intercepts the prefix in the *client* before any key reaches a
    pane — DEC-041's own finding, and the reason it could fix the root budget at one while
    saying prefix-table bindings are free.

    So the assertion is not "the console binds few keys". It is that the *root* set is still
    exactly one, and that every forwarding chord went into the prefix table where it belongs.
    A forwarding key that landed in the root table would take eight keys from every agent on
    the server, silently, against a budget of one.
    """
    from remote_agents.adapters.tui.screens.sessions import CHORD_KEYS
    from remote_agents.application.console import CONSOLE_BINDINGS, console_prefix_bindings
    from remote_agents.ports.console import ConsoleBindingAction, ConsoleKeyTable

    assert len(CONSOLE_BINDINGS) == 1, (
        f"the root key budget is one (DEC-041) and is now {len(CONSOLE_BINDINGS)}: "
        f"{[binding.key for binding in CONSOLE_BINDINGS]}"
    )
    assert all(binding.table is ConsoleKeyTable.ROOT for binding in CONSOLE_BINDINGS)

    prefix = console_prefix_bindings(CHORD_KEYS)
    assert {binding.key for binding in prefix} == {f"M-{key}" for key in CHORD_KEYS}, (
        "the prefix layer is not the chord vocabulary, so a chord exists that cannot be "
        "reached from inside a displayed agent"
    )
    assert all(binding.table is ConsoleKeyTable.PREFIX for binding in prefix), (
        "a forwarding chord is bound in the root table, which takes that key from every agent "
        "on this server — the cost DEC-041 fixed at one key total"
    )
    assert all(binding.action is ConsoleBindingAction.FORWARD_TO_SESSIONS for binding in prefix)


def test_every_binding_states_what_it_costs() -> None:
    """`why` is required of both tables, and the reason differs between them.

    A root key is paid for in the owner's *agents'* keyboards; a prefix key is paid for in the
    owner's memory. Neither is free enough to take without an argument, and this is the field
    the plan's gate reads when it asks whether a budget is worth its price.
    """
    from remote_agents.adapters.tui.screens.sessions import CHORD_KEYS
    from remote_agents.application.console import (
        CONSOLE_BINDINGS,
        console_panes_binding,
        console_prefix_bindings,
    )

    # The fold key is a *third* declaration, outside both tuples by design — which made it the
    # one binding whose `why` nothing asserted. It has a good one; this is what keeps it.
    for binding in (
        *CONSOLE_BINDINGS,
        *console_prefix_bindings(CHORD_KEYS),
        console_panes_binding(),
    ):
        assert binding.why.strip(), f"{binding.key} is bound with no argument for its cost"


def test_the_composed_console_installs_the_prefix_layer_and_no_second_root_key() -> None:
    """What the *production* composer is actually handed, which nothing else asserts.

    The two tuples are pinned in isolation above, and `ConsoleComposer`'s default is
    `CONSOLE_BINDINGS` alone — so every other test in this suite builds a composer that never
    sees the prefix layer. Delete the `bindings=` argument at the composition root and each of
    them stays green while the Stage 4 goal is silently unmet in production: the chords would
    work everywhere except the one position they were added for.

    It closes the other half too. DEC-041 names `len(CONSOLE_BINDINGS) == 1` as the guard that
    stops the root budget growing quietly, and the composition root is now a route around it —
    a `ConsoleBinding(..., ROOT)` appended there carries no key literal for the declaration
    check to find and does not lengthen the tuple the budget check measures. So the assertion
    is on the *composed* set: exactly one root key, and one prefix key per chord.
    """
    from remote_agents.adapters.tui.screens.sessions import CHORD_KEYS
    from remote_agents.application.console import console_panes_binding
    from remote_agents.composition.tui import _console_composer
    from remote_agents.ports.console import ConsoleKeyTable

    composed = _console_composer()._bindings
    root = [binding for binding in composed if binding.table is ConsoleKeyTable.ROOT]
    prefix = [binding for binding in composed if binding.table is ConsoleKeyTable.PREFIX]

    assert len(root) == 1, (
        f"the composed console takes {len(root)} root keys, and the budget is one (DEC-041): "
        f"{[binding.key for binding in root]}"
    )
    # The chords, plus the fold key — which is a *third* declaration, deliberately outside
    # `CONSOLE_BINDINGS` and outside the chord layer. Named here rather than allowed by a
    # subset check: a prefix key still costs the owner's memory, so an unannounced one
    # appearing in the composed set is exactly what this asserts against.
    assert {binding.key for binding in prefix} == {f"M-{key}" for key in CHORD_KEYS} | {
        console_panes_binding().key
    }, (
        "the production console does not install the prefix layer, so either the Alt chords "
        "do not reach the one position they were added for — inside a displayed agent — or "
        "the fold key does not reach the console at all"
    )
    assert len(composed) == len(root) + len(prefix), "a binding is in neither key table"


def test_the_settings_key_is_a_screen_binding_and_costs_the_root_budget_nothing() -> None:
    """The one key Task 3.4 added, registered here deliberately -- and it raised nothing.

    **The plan's file list for that task said "budget raised by one, deliberately", and that
    expectation was wrong in a way worth recording rather than quietly satisfying.** The budget
    this file is named for is `bind-key -n`: keys taken from every agent on the tmux server, for
    as long as they are bound, which is why DEC-041 fixed it at one and why the count is pinned
    above. A Settings key on `DashboardScreen` is not one of those. It is a Textual screen
    binding inside our own process, dispatched by our own app from a pane tmux already gave the
    keyboard to -- the same distinction `action_show_projects_pane` and `HOST_PAIR_KEY` each
    record for `p` and `P`. Incrementing the number in this file to accommodate it would have
    been the opposite of what the number is for: it would have reported a root key taken from
    every agent on the host, and none was.

    So what is registered deliberately is the *distinction*, asserted rather than trusted to
    the comment that states it (DEC-010 -- assert the property, do not widen a grep). Three
    things have to hold, and the third is the one no existing check covers: the key is bound on
    the screen, it is absent from the root table, and it is absent from the prefix table too. A
    key in either console table would be a key the owner's agents lose, and the prefix one is
    the easier mistake to make because it is the table that is described as free.
    """
    from remote_agents.adapters.tui.screens.dashboard import SETTINGS_KEY, DashboardScreen
    from remote_agents.adapters.tui.screens.sessions import CHORD_KEYS
    from remote_agents.application.console import CONSOLE_BINDINGS, console_prefix_bindings

    screen_keys = {binding.key for binding in DashboardScreen.BINDINGS}
    assert SETTINGS_KEY in screen_keys, (
        f"{SETTINGS_KEY!r} opens the Settings position, and the dashboard is where it is bound; "
        f"the screen now binds {sorted(screen_keys)}"
    )

    console_keys = {binding.key for binding in CONSOLE_BINDINGS} | {
        binding.key for binding in console_prefix_bindings(CHORD_KEYS)
    }
    assert SETTINGS_KEY not in console_keys, (
        f"{SETTINGS_KEY!r} reached a console key table. A root binding takes that key from "
        "every agent on this tmux server and a prefix one spends the owner's memory; this key "
        "is a screen binding in our own process and needs neither."
    )
    assert f"M-{SETTINGS_KEY}" not in console_keys, (
        f"the Settings key was also forwarded as a chord. `{SETTINGS_KEY}` acts on no session "
        "row, so there is nothing for the Alt layer to carry it to."
    )
