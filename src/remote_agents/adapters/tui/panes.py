"""One Textual app per tmux pane — the console's surface is three processes, not three widgets.

A Textual app owns a terminal, and the console's design needs three *tmux* panes side by
side, so the combined dashboard cannot simply be re-laid-out: the left pane (projects, and
the pane every exchange swaps), the right-top pane (sessions, and the only pane still on
screen once an agent occupies the left slot) and the right-bottom pane (the feed) each run
as their own process over their own per-operation lease (DEC-035). Three writers over one
SQLite file is not a new story — the bot and the surface already made two.

Each surface is a `RemoteAgentsTui` subclass that differs only in its default screen, so
every service method the combined dashboard already has — launching, stopping, the confirm
modals, the busy interlock, `in_thread` — is *inherited* rather than re-implemented three
times. What a pane owns is which position it rests on.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from textual.screen import Screen

from remote_agents.adapters.tui import PANE_NAMES
from remote_agents.adapters.tui.app import RemoteAgentsTui
from remote_agents.adapters.tui.context import TuiContext
from remote_agents.adapters.tui.model import AttachRequest
from remote_agents.adapters.tui.screens.feed import FeedScreen
from remote_agents.adapters.tui.screens.dashboard import ProjectsPaneScreen
from remote_agents.adapters.tui.screens.sessions import SessionsScreen


class ProjectsPane(RemoteAgentsTui):
    """The left pane: the projects catalogue, and the pane an exchange swaps out."""

    def get_default_screen(self) -> Screen[None]:
        return ProjectsPaneScreen()


class SessionsPane(RemoteAgentsTui):
    """The right-top pane: every managed session, and where a session is opened from.

    The sessions pane is the swap controller deliberately — it is the one pane that stays
    visible while an agent occupies the left slot, so it is the only place the owner can
    reach back from.
    """

    def get_default_screen(self) -> Screen[None]:
        return SessionsScreen()


class FeedPane(RemoteAgentsTui):
    """The right-bottom pane: the durable notifications feed, newest first (DEC-037)."""

    def get_default_screen(self) -> Screen[None]:
        return FeedScreen()


#: The console's panes, by the name `remote-agents pane <name>` takes. Keyed off `PANE_NAMES`
#: rather than repeating it, so the parser's `choices` and the surfaces they route to cannot
#: drift. Three names and three *distinct* surfaces: one class answering to three keys would
#: route perfectly and render the same pane three times.
PANE_SURFACES: Mapping[str, type[RemoteAgentsTui]] = dict(
    zip(PANE_NAMES, (ProjectsPane, SessionsPane, FeedPane), strict=True)
)


def run_pane_surface(
    name: str,
    context: TuiContext,
    *,
    runner: Callable[[type[RemoteAgentsTui], TuiContext], AttachRequest | None] | None = None,
) -> AttachRequest | None:
    """Run one pane surface and return what the caller must attach to, if anything.

    The `runner` seam mirrors `run_local_terminal`'s: it exists so a test can drive the
    routing without a terminal, and for no other reason.
    """
    surface = PANE_SURFACES.get(name)
    if surface is None:
        raise ValueError(f"unknown console pane: {name}")
    if runner is not None:
        return runner(surface, context)
    return surface(context).run()
