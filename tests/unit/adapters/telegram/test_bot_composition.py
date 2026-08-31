"""The bot's collaborators are wired at the composition root, not by the boundary itself.

`__post_init__` used to build a `StopController`, a `LiveView` and an `ActivityNotifier`
out of whatever the boundary happened to have been given. Everything they needed was
already there, which is what made it convenient and what made it wrong: a composition root
that wanted a different live view, or none, had nowhere to say so, and the one object in
this codebase whose whole job is to decide how the pieces fit had no say in three of them.

`build_private_bot` is that place now. It is also, deliberately, the only supported way to
get a working boundary — the three fields are `init=False` and the factory fills them, so
a bare `PrivateBotBoundary(...)` is a boundary nobody has wired yet.

One of the three genuinely cannot be built first. `ActivityNotifier` takes `display` and
`finished`, which are boundary methods, so the factory constructs the boundary and then
attaches it. That cycle is real rather than incidental — naming a session for a
notification needs the catalogue the boundary is holding — and the factory is where it is
paid for once, in the open, instead of being hidden inside the object it entangles.
"""

from __future__ import annotations

import ast
import pathlib

from backends import backend_for

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.live_view import ChatViewStore, LiveView
from remote_agents.adapters.telegram.notifications import StandingNotificationStore
from remote_agents.adapters.telegram.service import PrivateBotBoundary, build_private_bot

_SRC = pathlib.Path(__file__).resolve().parents[4] / "src" / "remote_agents"

OWNER = 7
CHAT = 11


def _post_init() -> ast.FunctionDef:
    tree = ast.parse((_SRC / "adapters" / "telegram" / "service.py").read_text())
    boundary = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "PrivateBotBoundary"
    )
    return next(
        node
        for node in boundary.body
        if isinstance(node, ast.FunctionDef) and node.name == "__post_init__"
    )


def test_post_init_builds_no_collaborator() -> None:
    """Parsed, because the claim is about where the constructor call is written."""
    built = sorted(
        node.func.id
        for node in ast.walk(_post_init())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"StopController", "LiveView", "ActivityNotifier"}
    )

    assert built == [], f"__post_init__ still composes {built}; that is the root's job"


def test_the_composition_root_goes_through_the_factory() -> None:
    """`bootstrap` must not construct the boundary raw — it would get an unwired one."""
    roots = [_SRC / "bootstrap.py", *sorted((_SRC / "composition").glob("*.py"))]
    calls = [
        node.func.id
        for path in roots
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"PrivateBotBoundary", "build_private_bot"}
    ]

    assert calls == ["build_private_bot"], f"the composition roots compose the bot as {calls}"


def test_the_factory_wires_all_three_over_the_ports_the_boundary_got() -> None:
    """One callback store, not three — the collaborators and the boundary share it.

    This is the substance `__post_init__` used to guarantee by construction, and the thing
    most easily lost by moving the wiring out: a factory that built its own
    `CallbackStateStore` for the notifier would work, would pass every screen test, and
    would drop every button the boundary had minted.
    """
    callbacks = CallbackStateStore()

    bot = build_private_bot(OWNER, CHAT, backend=backend_for(), callbacks=callbacks)

    assert bot.callbacks is callbacks
    assert bot.stops._callbacks is callbacks  # noqa: SLF001
    assert bot.view._callbacks is callbacks  # noqa: SLF001
    assert bot.notifier._callbacks is callbacks  # noqa: SLF001
    assert bot.notifier._view is bot.view  # noqa: SLF001


def test_the_in_memory_port_fakes_still_default_as_they_did() -> None:
    """A boundary built with no ports is an in-memory one, exactly as before the move."""
    bot = build_private_bot(OWNER, CHAT, backend=backend_for())

    assert isinstance(bot.callbacks, CallbackStateStore)
    assert isinstance(bot.anchors, ChatViewStore)
    assert isinstance(bot.standing, StandingNotificationStore)


def test_an_injected_collaborator_is_used_rather_than_rebuilt() -> None:
    """The point of moving the wiring out: the root can now decide, which it could not."""
    anchors = ChatViewStore()
    view = LiveView(chat_id=CHAT, callbacks=CallbackStateStore(), anchors=anchors)

    bot = build_private_bot(OWNER, CHAT, backend=backend_for(), view=view)

    assert bot.view is view
    assert bot.notifier._view is view, "the notifier was given a different view"  # noqa: SLF001


def test_a_raw_boundary_is_unwired_rather_than_half_wired() -> None:
    """Says out loud what `init=False` means here, so nobody reads a bare one as ready.

    The alternative — defaulting the three — is what would make this dangerous: a boundary
    that silently works with collaborators the root never chose is the situation this task
    removed.
    """
    raw = PrivateBotBoundary(OWNER, CHAT, backend=backend_for())

    for absent in ("stops", "view", "notifier"):
        assert not hasattr(raw, absent), f"{absent} was wired without the factory"


def test_the_factory_is_the_only_place_src_constructs_a_boundary() -> None:
    """The generalisation of `test_the_composition_root_goes_through_the_factory`.

    That one watches `bootstrap.py`, because that is where the plan said the composition
    root lives. It is not where the bug was. `run_private_bot` carries its own
    `boundary: PrivateBotBoundary | None = None` default and built a bare one to fill it, so
    moving the collaborators out of `__post_init__` left that path constructing a boundary
    with no notifier and dereferencing `boundary.notifier` six lines later. Nothing in the
    suite calls `run_private_bot`, and neither of the Stage 3 gate's sweeps could see it: it
    is not a `getattr` probe and it is not in `bootstrap.py`.

    So the rule is stated over the whole source tree rather than over the one file that
    happened to be named. Any *other* module that grows a default boundary — or a second
    entry point, or a convenience constructor — fails here on the day it is written, which
    is the only moment the fix is cheap.
    """
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        factory = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "build_private_bot"
            ),
            None,
        )
        permitted = {id(node) for node in ast.walk(factory)} if factory else set()
        offenders += [
            f"{path.relative_to(_SRC)}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "PrivateBotBoundary"
            and id(node) not in permitted
        ]

    assert offenders == [], (
        "these construct a boundary without wiring its collaborators, so the first use of "
        f"`.stops`, `.view` or `.notifier` raises: {offenders}. Call build_private_bot."
    )


def _run_private_bot() -> ast.AsyncFunctionDef:
    tree = ast.parse((_SRC / "adapters" / "telegram" / "service.py").read_text())
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_private_bot"
    )


def test_the_bot_handles_updates_sequentially() -> None:
    """`concurrent_updates(False)` is a correctness constraint, not a throughput setting.

    A render mints its keyboard **unbound** and binds it once Telegram answers, and
    `bind_pending` adopts every unbound token in the chat. With two renders in flight at
    once there is no way to tell whose tokens are whose, so one screen's buttons can be
    adopted by the other's message -- and the buttons on this bot include force stop. The
    failure is a destructive action on the session the owner was not looking at.

    Until this test, the whole defence was a comment addressed to whoever might raise the
    setting for throughput. `run_private_bot` is called by **no test in the suite** (BL-006),
    so the value reaching `ApplicationBuilder` was never observed by anything; the comment
    was the guard. This is the one clause of BL-006's gap cheap enough to close without the
    `ApplicationBuilder` fake that the rest of it needs -- the argument is written down, so
    the literal is worth pinning even though the call itself stays unexecuted.

    **Absence is a failure, not a pass.** python-telegram-bot's own default is sequential
    today, so deleting the call would keep the behaviour and lose the statement of it -- and
    the next major version is free to change a default nobody is asserting. The same reason
    the setting is written out in the first place.

    **A non-literal argument is a failure too.** `concurrent_updates(flag)` puts the value
    somewhere this test cannot read, and a guard that cannot see its subject reports green
    over it. That is the DEC-010 failure mode, and the safe direction is to refuse.

    Stated limit: this parses the literal at the call site. It does not run the builder, so
    it cannot see a value overridden afterwards through some other path -- closing that is
    BL-006's harness, not this test.
    """
    calls = [
        node
        for node in ast.walk(_run_private_bot())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "concurrent_updates"
    ]

    assert len(calls) == 1, (
        f"expected exactly one `concurrent_updates(...)` in run_private_bot, found {len(calls)}. "
        "Sequential update handling is load-bearing: two renders in flight let `bind_pending` "
        "adopt one screen's buttons onto the other's message, and the buttons include force stop."
    )

    (argument,) = calls[0].args
    assert isinstance(argument, ast.Constant), (
        f"`concurrent_updates(...)` is passed `{ast.unparse(argument)}`, which this check cannot "
        "read. Pass the literal `False` so the constraint is visible where it is set."
    )
    assert argument.value is False, (
        "`concurrent_updates(True)` lets two renders be in flight at once, and `bind_pending` "
        "adopts every unbound token in the chat -- so one screen's buttons can be adopted by "
        "the other's message, force stop among them. If throughput really needs this, the "
        "token binding has to stop being chat-scoped first."
    )
