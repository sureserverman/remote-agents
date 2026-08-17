"""Every keyboard-carrying bot screen closes with the navigation bar, structurally.

DEC-032 claims the bar is universal "by construction": `PrivateBotBoundary._message` is the
one place a screen's closing row is built, and the two renders that deliberately carry no bar
bypass it by calling `presenters.render_message` directly. That claim is only as good as the
set of direct callers — a third one added later would be a screen that had silently escaped
the bar, and it would look exactly like the two legitimate ones.

Until now the guard was a **manual grep in a gate checklist** (`grep -c 'render_message(' == 4`).
That catches it once, on the day someone runs it. The behavioural tests do not close the gap
either: they drive a *sample* of screen families through the boundary, and `_every_screen`'s
own docstring lists the families it excludes, so a new bypassed screen is exactly the thing a
sample cannot see.

So the enumeration lives here instead, as a test that fails on **any** change to the map of
call sites — a new module calling `render_message`, or an existing one calling it one more
time. (It first read "fails on the fourth caller", which described a weaker earlier form that
pinned module names and a single count, and would have let `notifications.py` grow a second
barless render untouched.) It is deliberately about *call sites* rather than about rendered
output: the defect it guards is a screen that never routes through `_message` at all, which no
assertion about screens that do can reach.
"""

from __future__ import annotations

import ast
import pathlib

_ADAPTERS = pathlib.Path(__file__).resolve().parents[2] / "src" / "remote_agents" / "adapters"
_TELEGRAM = _ADAPTERS / "telegram"

#: Every `render_message` call in the package, as `module.py` -> (count, why).
#:
#: The **counts** are the point, not just the module names. An earlier version of this file
#: enumerated permitted modules and pinned only `service.py`'s count, which left
#: `notifications.py` free to grow a second barless render — passing both tests while the
#: manual `grep -c == 4` it replaced would have caught it (5 ≠ 4). A test that replaces a
#: sweep must not be weaker than the sweep.
#:
#: Both bypasses predate this plan: they were already calling `render_message` directly, which
#: is why DEC-032 records the carve-outs as structural rather than as a suppression flag added
#: for them. Changing a number here is a deliberate decision to ship another barless render,
#: and belongs in the decision register rather than in whatever edit made this test fail.
_EXPECTED_CALLS = {
    "notifications.py": (1, "an activity notification is a message, not a screen (DEC-031)"),
    "service.py": (
        2,
        "`_message` itself, which is where the bar is appended, plus the pending screen, "
        "which drops its keyboard so a wait cannot be pressed into a second launch",
    ),
}


def _direct_callers() -> dict[str, int]:
    """Count `render_message(...)` calls per module.

    `presenters.py` does not appear: its `def render_message` is a definition, not an
    `ast.Call`, so nothing there matches. (An earlier revision subtracted it defensively and
    claimed to be "excluding its own definition" — both the subtraction and the sentence were
    dead, and are gone rather than left to imply a guard that was never running.)
    """
    found: dict[str, int] = {}
    for path in sorted(_TELEGRAM.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name == "render_message":
                calls += 1
        if calls:
            found[path.name] = calls
    return found


def test_every_render_message_call_in_the_package_is_an_accounted_for_one() -> None:
    """The whole map, not just its keys — a new call in an already-listed module is the escape.

    Asserted as one dict comparison rather than as a membership check plus one pinned count,
    because those two together still let `notifications.py` grow a second barless render. The
    grep this replaces counted every call in the package, and a structural test that replaces
    a sweep has to be at least as strong as the sweep.
    """
    expected = {module: count for module, (count, _why) in _EXPECTED_CALLS.items()}

    assert _direct_callers() == expected, (
        "the set of direct presenters.render_message calls changed. Each one draws a screen "
        "that carries NO navigation bar, so a new or moved call is a screen that has escaped "
        "PrivateBotBoundary._message. Route it through _message, or — if a barless render is "
        "genuinely intended — update _EXPECTED_CALLS with its reason and record the decision. "
        f"Expected {expected}, found {_direct_callers()}."
    )


def test_the_bar_is_built_in_exactly_one_place() -> None:
    """`_message` is the sole builder of a closing row, which is what makes the bar universal.

    Kept separate from the map above so the failure *reads* differently: this one says "some
    other method in the boundary started composing its own screen", which is how the bar stops
    being universal without any single change looking wrong.
    """
    calls = _direct_callers().get("service.py", 0)

    assert calls == 2, (
        f"service.py calls render_message {calls} times, expected 2 (_message itself, and the "
        "pending screen). A new direct call is a screen built outside the one place the "
        "navigation bar is appended."
    )
