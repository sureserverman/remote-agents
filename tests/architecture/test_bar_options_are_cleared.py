"""Every publisher of a console status-bar option also clears it (sub-plan 2 Task 2.3, DEC-105).

The bar reads three facts panes publish: whether a session row is selected, whether a text
entry holds the keyboard, and the Remote Control words. A pane that sets one and never clears
it leaves the bar stating the fact after the pane that knew it is gone -- stuck on `esc
cancels`, or on a Remote Control reading nobody refreshes. That is a property of the set of
publishers, so it is swept rather than tested per publisher.

**The rule, as a command over the parsed source.** A module that reads any publisher
capability off `TuiContext` must also contain a call passing the literal `None` to a function
that reaches that capability -- directly, or through `self.` methods of the same module that
do. What it cannot see, stated rather than hidden: *when* the unset runs. That it runs on the
way out is each publisher's own test (unmount, escape, submit, a raising screen).
"""

from __future__ import annotations

import ast
from pathlib import Path

#: The `TuiContext` capabilities that write a status-bar option.
PUBLISHERS = frozenset(
    {
        "console_publish_session_selected",
        "console_publish_typing",
        "console_publish_remote_control",
    }
)

_TUI = Path("src/remote_agents/adapters/tui")


def _functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


def _reads_publisher(node: ast.AST) -> set[str]:
    return {
        sub.attr
        for sub in ast.walk(node)
        if isinstance(sub, ast.Attribute) and sub.attr in PUBLISHERS
    }


def _self_calls(node: ast.AST) -> set[str]:
    return {
        sub.func.attr
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Attribute)
        and isinstance(sub.func.value, ast.Name)
        and sub.func.value.id == "self"
    }


def uncleared_publishers(source: str) -> set[str]:
    """The publisher capabilities this module reads and never reaches with a `None`."""
    tree = ast.parse(source)
    read = _reads_publisher(tree)
    if not read:
        return set()
    functions = _functions(tree)
    # Which capabilities each function reaches, closed over `self.` calls within the module.
    reaches = {function.name: _reads_publisher(function) for function in functions}
    calls = {function.name: _self_calls(function) for function in functions}
    changed = True
    while changed:
        changed = False
        for name, callees in calls.items():
            for callee in callees:
                extra = reaches.get(callee, set()) - reaches[name]
                if extra:
                    reaches[name] |= extra
                    changed = True
    cleared: set[str] = set()
    for function in functions:
        # Locals bound straight from a capability: `flag = self.services.console_publish_...`.
        bound = {
            target.id: _reads_publisher(assign.value)
            for assign in ast.walk(function)
            if isinstance(assign, ast.Assign)
            for target in assign.targets
            if isinstance(target, ast.Name) and _reads_publisher(assign.value)
        }
        for call in ast.walk(function):
            if not isinstance(call, ast.Call):
                continue
            if not any(isinstance(a, ast.Constant) and a.value is None for a in call.args):
                continue
            callee = call.func
            if isinstance(callee, ast.Name) and callee.id in bound:
                cleared |= bound[callee.id]
            elif isinstance(callee, ast.Attribute):
                cleared |= reaches.get(callee.attr, set())
                cleared |= _reads_publisher(callee) & PUBLISHERS
    return read - cleared


def test_bar_options_are_cleared_by_every_module_that_publishes_them() -> None:
    publishing = {}
    offenders = {}
    for path in sorted(_TUI.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if _reads_publisher(ast.parse(source)):
            publishing[str(path)] = _reads_publisher(ast.parse(source))
        missing = uncleared_publishers(source)
        if missing:
            offenders[str(path)] = sorted(missing)

    assert publishing, "the sweep found no publisher at all, so it proves nothing"
    assert set().union(*publishing.values()) == PUBLISHERS
    assert not offenders, f"these modules set a bar option and never unset it: {offenders}"


def test_bar_options_are_cleared_sweep_catches_a_setter_with_no_unset() -> None:
    """The sweep's own mutant: a module that only ever publishes a value is flagged."""
    source = """
class Pane:
    async def show(self, value):
        flag = self.services.console_publish_typing
        await flag(True)
"""
    assert uncleared_publishers(source) == {"console_publish_typing"}


def test_bar_options_are_cleared_sweep_accepts_an_unset_through_a_helper() -> None:
    source = """
class Pane:
    async def _write(self, value):
        await self.services.console_publish_typing(value)

    async def leave(self):
        await self._write(None)
"""
    assert uncleared_publishers(source) == set()
