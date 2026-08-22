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
    tree = ast.parse((_SRC / "bootstrap.py").read_text())
    calls = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"PrivateBotBoundary", "build_private_bot"}
    ]

    assert calls == ["build_private_bot"], f"bootstrap composes the bot as {calls}"


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
