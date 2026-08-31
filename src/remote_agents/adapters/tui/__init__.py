"""Local terminal driver adapter for the owner's own host."""

from __future__ import annotations

from remote_agents.ports.frontend_descriptor import FrontendDescriptor

#: The console's three panes, by the name `remote-agents pane <name>` takes.
#:
#: Declared here, in a module that imports no adapter code, because the composition root
#: needs the *names* to build its argument parser while every module that knows what a pane
#: *is* imports Textual — and `serve` must never load the terminal library (a failure in it
#: would then reach the bot). `panes.PANE_SURFACES` is keyed off this tuple rather than
#: repeating it, so the parser's `choices` and the surfaces it routes to cannot drift apart.
PANE_NAMES: tuple[str, ...] = ("projects", "sessions", "limits", "feed")


def _wire(*args: object, **kwargs: object) -> object:
    """Defer the context import for the reason PANE_NAMES states: no Textual at module scope."""
    from remote_agents.adapters.tui.context import TuiContext

    return TuiContext(*args, **kwargs)


#: What this surface is and what it cannot start without (ARCH-03). Console capabilities and
#: conversations stay off the claim: they are host-shaped and absent by design elsewhere.
FRONTEND = FrontendDescriptor(
    name="tui",
    wire=_wire,
    required_capabilities=("sessions", "projects", "refresh_catalogue"),
)
