"""DEC-025's rule, checked as a rule rather than guarded at runtime.

`ask_to_confirm` pushes a modal and suspends the caller until it is answered. Nothing
guarantees it ever is: the modal can be popped for reasons that have nothing to do with the
owner's decision — a navigation that unwinds the stack, an error path that resets the screen,
a second entry point arriving mid-flight. When that happens the `await` is never satisfied and
never fails. It waits, holding whatever the caller was holding.

**The reason this has never bitten anyone is not that the code prevents it.** Every
confirmation in the tree is asked from a screen's own handler, and a screen handler runs on
the message pump — so while it is suspended the pump is not delivering the events that would
pop the modal out from under it. The protection is a side effect of *where the calls happen to
be made from*. Move one call off the pump and it is gone, silently, with no test failing and
no error raised.

DEC-025 records the owner's decision on what to do about that: **no timeout and no
cancellation path.** A timeout would invent a new failure mode on the destructive path and
give it no good answer — a timed-out force-stop confirmation can neither proceed (nobody
confirmed) nor cancel (the owner may be mid-decision), so it would replace a hang nobody has
hit with an ambiguity everybody would. The decision's accepted cost is stated plainly: the
hang stays unreachable *by convention* rather than by construction.

**So this file tests the convention.** Not a runtime guard — that would be the construction
DEC-025 declined, and adopting one here would supersede the decision rather than implement it.
What this does instead is make the convention fail loudly at the moment someone breaks it,
which is the one thing a document alone cannot do. The register's first accepted cost — "the
safeguard is a document, and documents are read by people who go looking" — is what this
narrows: a bad caller now has to get past a red test, not merely past a paragraph nobody
opened.

The forbidden callers are DEC-025's own list, in its own words: *"a worker, a timer, a
background task, a message pump callback, a global binding"*.

**Stated limits, because a check that overstates its coverage is worse than one that admits
its scope.** This is a static reachability sweep over the TUI adapter's source. It cannot see
a call made through a variable holding a bound method, through `getattr`, or from outside this
package. It proves that no *lexically visible* path from a forbidden caller reaches
`ask_to_confirm`; it does not prove the hang is unreachable. That is exactly what DEC-025 says
it is buying, and no more.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ADAPTER = Path(__file__).resolve().parents[2] / "src" / "remote_agents" / "adapters" / "tui"

#: The entry point every destructive confirmation goes through.
_GUARDED = "ask_to_confirm"


def _modules() -> list[tuple[Path, ast.Module]]:
    return sorted(
        ((path, ast.parse(path.read_text())) for path in _ADAPTER.rglob("*.py")),
        key=lambda pair: pair[0].as_posix(),
    )


def _functions(tree: ast.Module):
    """Every function in the module, with the class it is defined on (or `None`)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    yield child, node
        elif isinstance(node, ast.Module):
            for child in node.body:
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    yield child, None


def _called_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Every method or function name this body calls, by bare name.

    Bare names rather than resolved targets, deliberately: the question is whether a path
    exists at all, and over-approximating callers is the safe direction for a check whose
    failure mode is missing one.
    """
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Attribute):
                names.add(target.attr)
            elif isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _reaching() -> dict[str, tuple[str, str | None]]:
    """Every function with a lexical path to `ask_to_confirm`, mapped to where it lives."""
    bodies: dict[str, tuple[ast.FunctionDef | ast.AsyncFunctionDef, str, str | None]] = {}
    for path, tree in _modules():
        for fn, cls in _functions(tree):
            bodies[fn.name] = (fn, path.name, cls.name if cls else None)

    reaching = {_GUARDED}
    changed = True
    while changed:
        changed = False
        for name, (fn, _, _) in bodies.items():
            if name not in reaching and _called_names(fn) & reaching:
                reaching.add(name)
                changed = True
    return {name: (bodies[name][1], bodies[name][2]) for name in reaching if name in bodies}


def _scheduled_callbacks() -> set[str]:
    """Names handed to a timer, an interval or a deferred call anywhere in the adapter.

    These are DEC-025's "a timer, a background task": a callback invoked off the pump, whose
    suspension therefore does not hold back the events that could pop a modal.
    """
    schedulers = {"set_interval", "set_timer", "call_later", "call_after_refresh", "run_worker"}
    scheduled: set[str] = set()
    for _, tree in _modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            if not (isinstance(target, ast.Attribute) and target.attr in schedulers):
                continue
            for argument in node.args:
                if isinstance(argument, ast.Attribute):
                    scheduled.add(argument.attr)
                elif isinstance(argument, ast.Name):
                    scheduled.add(argument.id)
    return scheduled


def _worker_decorated() -> set[str]:
    """Functions carrying Textual's `@work` decorator — DEC-025's "a worker"."""
    workers: set[str] = set()
    for _, tree in _modules():
        for fn, _ in _functions(tree):
            for decorator in fn.decorator_list:
                node = decorator.func if isinstance(decorator, ast.Call) else decorator
                name = (
                    node.attr
                    if isinstance(node, ast.Attribute)
                    else node.id
                    if isinstance(node, ast.Name)
                    else ""
                )
                if name == "work":
                    workers.add(fn.name)
    return workers


def test_the_rule_is_written_where_the_hazard_is() -> None:
    """DEC-025's safeguard is a document, so the document has to exist and be findable.

    The sweep below is what makes the rule fail loudly; this is what makes it *explicable*
    when it does. A red test that sends a reader to a paragraph which was never written is a
    worse outcome than either alone.
    """
    confirm = (_ADAPTER / "screens" / "confirm.py").read_text()
    assert "only ever asked from a screen handler" in confirm, (
        "DEC-025's rule is not stated in confirm.py. The decision's whole position is that the "
        "constraint is written down next to the code rather than enforced at runtime, so the "
        "sentence is the deliverable, not decoration."
    )


@pytest.mark.parametrize("forbidden", ["worker", "timer", "global binding"])
def test_no_confirmation_is_reachable_from_a_caller_dec_025_forbids(forbidden: str) -> None:
    """The rule itself: nothing off the message pump may reach `ask_to_confirm`.

    Parametrized by the kind of caller DEC-025 names so a failure says *which* rule was
    broken, rather than reporting one undifferentiated set.
    """
    reaching = _reaching()
    if forbidden == "worker":
        offenders = {name: where for name, where in reaching.items() if name in _worker_decorated()}
        # `_ask` is the one legitimate worker in the chain and is the mechanism itself: it
        # exists precisely to give `push_screen_wait` the worker context it requires, and it
        # is awaited by `ask_to_confirm` rather than the other way round.
        offenders.pop("_ask", None)
        rule = "a worker's suspension does not hold the pump, so the modal can be popped"
    elif forbidden == "timer":
        offenders = {
            name: where for name, where in reaching.items() if name in _scheduled_callbacks()
        }
        rule = "a timer or deferred callback runs in its own task, not on the pump"
    else:
        offenders = {
            name: where
            for name, where in reaching.items()
            if name.startswith("action_") and where[0] == "app.py"
        }
        rule = "a global binding runs on the app's pump, which does not block the screen's"

    assert not offenders, (
        f"DEC-025 forbids asking a confirmation from {forbidden}, and these now reach "
        f"`{_GUARDED}`: {sorted(offenders)}. Because {rule}, the await would never be "
        f"satisfied and never fail — it would hang, holding whatever its caller held. "
        f"DEC-025 declined a timeout deliberately, so there is no runtime net under this. "
        f"Move the call onto a screen handler, or take the decision to the owner."
    )


def test_every_confirmation_is_asked_from_a_screen() -> None:
    """The positive half: the callers that do exist are all screen methods.

    The checks above forbid the known-bad callers. This asserts the shape that makes the
    convention true in the first place — every direct caller of `ask_to_confirm` is a method
    on a screen, in the screens package, which is what puts it on the pump.
    """
    direct: list[tuple[str, str, str | None]] = []
    for path, tree in _modules():
        for fn, cls in _functions(tree):
            if _GUARDED in _called_names(fn) and fn.name != _GUARDED:
                direct.append((fn.name, path.name, cls.name if cls else None))

    assert direct, (
        f"no caller of `{_GUARDED}` was found at all, so this file is checking nothing. "
        f"Either the entry point was renamed or the sweep is broken."
    )
    for name, module, cls in direct:
        assert cls is not None and cls.endswith("Screen"), (
            f"{module}:{name} asks a confirmation but is not a method on a screen "
            f"(class={cls}). DEC-025's protection is that the caller runs on the screen's "
            f"message pump; a free function or a non-screen class has no such guarantee."
        )
