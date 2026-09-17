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


def test_the_root_set_is_the_function_key_row_and_nothing_else() -> None:
    """Eleven root keys, F11 absent, and no prefix forward left over.

    **DEC-041 fixed this at one and is superseded here**, so the number moving is the point of
    the test rather than a thing it tolerates. What the check is *for* has not changed: a root
    binding is a key every agent on this server can never receive, so the set must be exactly
    what was argued for and must not grow quietly afterwards.

    F11 is asserted absent on its own line. It is the terminal's own full-screen toggle almost
    everywhere, and it is the one member of the row that would be taken from something the
    owner cannot see us taking it from.
    """
    from remote_agents.application.console import CONSOLE_BINDINGS
    from remote_agents.ports.console import ConsoleBindingAction, ConsoleKeyTable

    keys = [binding.key for binding in CONSOLE_BINDINGS]
    assert sorted(keys) == sorted([f"F{number}" for number in range(1, 11)] + ["F12"]), (
        f"the root set is not the function-key row minus F11: {keys}"
    )
    assert len(CONSOLE_BINDINGS) == 11, f"the root set is {len(CONSOLE_BINDINGS)} keys: {keys}"
    assert "F11" not in keys, "F11 belongs to the terminal and the console does not take it"
    assert all(binding.table is ConsoleKeyTable.ROOT for binding in CONSOLE_BINDINGS)

    actions = {binding.key: binding.action for binding in CONSOLE_BINDINGS}
    assert actions["F12"] is ConsoleBindingAction.SHOW_PROJECTS, (
        "F12 is the exchange, which is the one console operation tmux cannot perform itself"
    )
    assert all(
        action is ConsoleBindingAction.FORWARD_FUNCTION_KEY
        for key, action in actions.items()
        if key != "F12"
    ), f"a member of the row is not a forward: {actions}"


def test_the_root_row_is_the_surface_s_own_key_table_in_tmux_s_spelling() -> None:
    """The two declarations of one row, asserted against each other.

    The console names its keys in `application/console.py` and the surface names them in
    `adapters/tui/keys.py`, because an application module may not import an adapter — so the
    row is written twice, in two spellings, and nothing about the type system makes them agree.
    That is the drift this asserts away: a key bound at the root that no surface handles is a
    keystroke swallowed on every press, and a key the surface binds that the root set omits
    simply never arrives from inside a displayed agent.

    Compared case-insensitively because the spellings genuinely differ and both are right:
    tmux resolves `F2` and Textual delivers `f2`.
    """
    from remote_agents.adapters.tui.keys import FUNCTION_KEYS
    from remote_agents.application.console import CONSOLE_BINDINGS

    console = {binding.key.lower() for binding in CONSOLE_BINDINGS}
    surface = {entry.key for entry in FUNCTION_KEYS}

    assert console == surface, (
        "the console's root row and the surface's own key table disagree; keys only tmux "
        f"binds: {sorted(console - surface)}; keys only the surface binds: "
        f"{sorted(surface - console)}"
    )


def test_every_binding_states_what_it_costs() -> None:
    """`why` is required of both tables, and the reason differs between them.

    A root key is paid for in the owner's *agents'* keyboards; a prefix key is paid for in the
    owner's memory. Neither is free enough to take without an argument, and this is the field
    a reader consults when asking whether a budget is worth its price.

    **The ten forwards share one `why`, and that is deliberate rather than lazy.** Their cost
    argument really is identical — the same table, the same reservation pass-through, the same
    accepted risk from an uncurated agent — and what differs between them is what each key
    *does*, which is the surface's business and not this module's. Ten near-copies naming ten
    acts would be `application/console.py` holding a second opinion about a table it does not
    own. So the assertion is that every binding carries one, not that every binding carries a
    different one.
    """
    from remote_agents.application.console import CONSOLE_BINDINGS, console_panes_binding

    # The fold key is a *third* declaration, outside the root tuple by design — which made it
    # the one binding whose `why` nothing asserted. It has a good one; this is what keeps it.
    for binding in (*CONSOLE_BINDINGS, console_panes_binding()):
        assert binding.why.strip(), f"{binding.key} is bound with no argument for its cost"


def test_the_composed_console_installs_the_row_and_the_fold_and_nothing_else() -> None:
    """What the *production* composer is actually handed, which nothing else asserts.

    The tuples are pinned in isolation above, and `ConsoleComposer`'s default is
    `CONSOLE_BINDINGS` alone — so every other test in this suite builds a composer that never
    sees the fold key. Delete the `bindings=` argument at the composition root and each of
    them stays green while the console loses a key in production.

    It closes the other half too: the composition root is a route around the declaration
    check, since a `ConsoleBinding(..., ROOT)` appended there carries no key literal for the
    sweep to find and does not lengthen the tuple the set check measures. So the assertion is
    on the *composed* set.
    """
    from remote_agents.application.console import CONSOLE_BINDINGS, console_panes_binding
    from remote_agents.composition.tui import _console_composer
    from remote_agents.ports.console import ConsoleKeyTable

    composed = _console_composer()._bindings
    root = [binding for binding in composed if binding.table is ConsoleKeyTable.ROOT]
    prefix = [binding for binding in composed if binding.table is ConsoleKeyTable.PREFIX]

    assert {binding.key for binding in root} == {binding.key for binding in CONSOLE_BINDINGS}, (
        f"the composed root set is not the declared one: {sorted(b.key for b in root)}"
    )
    # The fold key alone. The eight prefix forwards retired with the Alt layer they served, so
    # anything else appearing here is a key nobody argued for.
    assert {binding.key for binding in prefix} == {console_panes_binding().key}, (
        "the production console's prefix table is not just the fold key: "
        f"{sorted(binding.key for binding in prefix)}"
    )
    assert len(composed) == len(root) + len(prefix), "a binding is in neither key table"


def test_a_console_that_forwards_a_function_key_cannot_be_built_without_the_reservations() -> (
    None
):
    """The omission that would be silent, made loud where it is visible.

    `reserved_keys` defaulting to `{}` is a valid mapping meaning "no agent reserves anything",
    so a composition root that forgot to pass it would build a console that comes up, installs
    every key, and quietly takes OpenCode's F2 from the owner — no exception, no log line, and
    the only symptom a key doing the wrong thing inside one agent. This task's own Tier-1
    review predicted it, which is why the constructor refuses instead.

    Passing `{}` explicitly is still allowed and still means what it says: the refusal is about
    the value never having been considered, not about its being empty.
    """
    import pytest

    from remote_agents.application.console import CONSOLE_BINDINGS, ConsoleComposer

    with pytest.raises(ValueError, match="reserved_keys"):
        ConsoleComposer(
            object(),  # type: ignore[arg-type]
            ("true",),
            pathlib.Path("/tmp"),
            projects_command=("true",),
            bindings=CONSOLE_BINDINGS,
        )

    stated = ConsoleComposer(
        object(),  # type: ignore[arg-type]
        ("true",),
        pathlib.Path("/tmp"),
        projects_command=("true",),
        bindings=CONSOLE_BINDINGS,
        reserved_keys={},
    )
    assert stated._reserved_keys == {}


def test_the_settings_key_is_an_app_binding_and_costs_the_root_budget_nothing() -> None:
    """The one key Task 3.4 added, registered here deliberately -- and it raised nothing.

    **It moved from the dashboard to the app on 2026-09-15 (sub-plan 02, Task 2.3) and this
    test went red, which is worth recording because the red was about the wrong thing.** The
    budget this file guards is tmux's, and nothing about the move touches it -- the two
    console-table assertions below, which are the ones DEC-041 is actually about, never
    wavered. What failed was this test's own incidental claim that the *dashboard* is where the
    key is bound, and that claim is exactly what BL-057 called the defect: the dashboard is one
    pane of four on a console, so three panes had no key for this screen. So the assertion now
    names the app, and the distinction the rest of the docstring draws is unchanged -- an app
    binding is still dispatched by our own process, from a pane tmux already gave the keyboard
    to. A Textual binding of any scope costs `bind-key -n` nothing.

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
    from remote_agents.adapters.tui.app import RemoteAgentsTui
    from remote_agents.adapters.tui.screens.dashboard import SETTINGS_KEY
    from remote_agents.application.console import CONSOLE_BINDINGS, console_panes_binding

    app_keys = {binding.key for binding in RemoteAgentsTui.BINDINGS}
    assert SETTINGS_KEY in app_keys, (
        f"{SETTINGS_KEY!r} opens the Settings position from every pane, so the app is where it "
        f"is bound; the app now binds {sorted(app_keys)}"
    )

    console_keys = {binding.key for binding in CONSOLE_BINDINGS} | {
        console_panes_binding().key
    }
    assert SETTINGS_KEY not in console_keys, (
        f"{SETTINGS_KEY!r} reached a console key table. A root binding takes that key from "
        "every agent on this tmux server and a prefix one spends the owner's memory; this key "
        "is a screen binding in our own process and needs neither."
    )
    assert f"M-{SETTINGS_KEY}" not in console_keys, (
        f"the Settings key was forwarded as a chord. `{SETTINGS_KEY}` acts on no session row, "
        "and the prefix layer that could have carried it retired with the Alt chords."
    )
