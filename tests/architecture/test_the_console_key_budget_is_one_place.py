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
