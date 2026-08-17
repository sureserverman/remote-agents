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

So the enumeration lives here instead, as a test that fails on the fourth caller. It is
deliberately about *call sites* rather than about rendered output: the defect it guards is a
screen that never routes through `_message` at all, which no assertion about screens that do
can reach.
"""

from __future__ import annotations

import ast
import pathlib

_TELEGRAM = pathlib.Path(__file__).resolve().parents[2] / "src" / "remote_agents" / "adapters" / "telegram"

#: The renders that legitimately carry no navigation bar, as `module.py` -> why.
#:
#: Both predate this plan: they were already calling `render_message` directly, which is why
#: DEC-032 records the carve-outs as structural rather than as a suppression flag added for
#: them. A new entry here is a deliberate decision to ship a barless screen and should be
#: argued for in the decision register, not added to make this test pass.
_PERMITTED_BYPASSES = {
    "notifications.py": "an activity notification is a message, not a screen (DEC-031)",
    "service.py": "the pending screen drops its keyboard so a wait cannot be pressed twice",
}


def _direct_callers() -> dict[str, int]:
    """Count `render_message(...)` calls per module, excluding its own definition."""
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


def test_only_the_two_permitted_renders_bypass_the_navigation_bar() -> None:
    callers = _direct_callers()

    unexpected = set(callers) - set(_PERMITTED_BYPASSES) - {"presenters.py"}
    assert not unexpected, (
        f"{sorted(unexpected)} call presenters.render_message directly, so the screens they "
        "draw carry no navigation bar. Route them through PrivateBotBoundary._message, or — "
        "if a barless render is genuinely intended — add the module to _PERMITTED_BYPASSES "
        "with its reason and record the decision."
    )


def test_the_bar_is_built_in_exactly_one_place() -> None:
    """`_message` is the sole builder of a closing row, which is what makes the bar universal.

    Pinned by counting `render_message` calls inside `service.py`: one is `_message` itself,
    and one is the pending screen. A third would mean some other method in the boundary had
    started composing its own screen, which is how the bar stops being universal without any
    single change looking wrong.
    """
    calls = _direct_callers().get("service.py", 0)

    assert calls == 2, (
        f"service.py calls render_message {calls} times, expected 2 (_message itself, and the "
        "pending screen). A new direct call is a screen built outside the one place the "
        "navigation bar is appended."
    )
