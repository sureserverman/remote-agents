"""The resting position: projects on the left, sessions and notifications on the right.

`DashboardScreen` subclasses the projects picker rather than replacing it, so the filter,
its debounce, the catalogue refresh, and the never-empty stack guarantee are inherited —
what this module adds is the shape: a right-hand column showing the running sessions
(reloaded on reveal and on the same 10-second cadence the sessions list uses, which also
keeps console tabs reconciled, since `load_sessions` is the sync choke point) and the
notifications feed pane (a placeholder until the durable activity feed lands).

The two lists share the base class's one row-selection handler; session rows carry a
namespaced key (`session:<id>`) so `choose` can route without a second dispatch chain.
"""

from __future__ import annotations

import logging

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.timer import Timer
from textual.widgets import Footer, Header, Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from remote_agents.adapters.tui.model import session_row
from remote_agents.adapters.tui.screens.launch import ProjectsScreen

_LOG = logging.getLogger(__name__)

_SESSION_KEY_PREFIX = "session:"
_SESSIONS_AUTO_REFRESH = 10.0
#: The sessions pane's one line when nothing runs — DEC-009's answer for this pane.
_NO_SESSIONS = "No sessions are running."


class DashboardScreen(ProjectsScreen):
    """Three panes, one resting position; everything the projects picker was, plus sight."""

    position = "DASHBOARD"

    DEFAULT_CSS = """
    DashboardScreen #dashboard-panes { height: 1fr; }
    DashboardScreen #dashboard-left { width: 3fr; }
    DashboardScreen #dashboard-right { width: 2fr; }
    DashboardScreen #sessions-pane { height: 2fr; border: round $secondary; }
    DashboardScreen #feed-pane { height: 1fr; border: round $secondary; padding: 0 1; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._sessions_timer: Timer | None = None
        self._reloading_sessions = False

    def compose(self) -> ComposeResult:
        """The base body, re-arranged: same ids, so every inherited method still lands.

        `#status`, `#filter`, `#choices`, and `#output` keep their names and their bases'
        widget classes — the machinery in `ChoiceScreen` queries them by id and must find
        exactly what it expects. Only their arrangement is new.
        """
        yield Header()
        with Vertical(id="body"):
            yield Static(self.status, id="status", markup=False)
            with Horizontal(id="dashboard-panes"):
                with Vertical(id="dashboard-left"):
                    yield Input(placeholder=self.filter_placeholder or "", id="filter")
                    yield OptionList(id="choices", markup=False)
                with Vertical(id="dashboard-right"):
                    sessions = OptionList(id="sessions-pane", markup=False)
                    sessions.border_title = "Sessions"
                    yield sessions
                    feed = Static("No notifications yet.", id="feed-pane", markup=False)
                    feed.border_title = "Notifications"
                    yield feed
            with VerticalScroll(id="output-pane"):
                yield TextArea(
                    "", id="output", read_only=True, soft_wrap=True, highlight_cursor_line=False
                )
        yield Footer()

    async def populate(self) -> None:
        await super().populate()
        if self.services.open_in_console is not None:
            # Task 3.3's promise: the jump-home key is documented where the owner rests.
            self.sub_title = "F12 returns to this dashboard"
        await self._reload_sessions_pane()
        self._sessions_timer = self.set_interval(
            _SESSIONS_AUTO_REFRESH, self._auto_reload_sessions
        )

    async def on_reveal(self) -> None:
        await super().on_reveal()
        await self._reload_sessions_pane()

    def on_screen_suspend(self) -> None:
        if self._sessions_timer is not None:
            self._sessions_timer.pause()

    def on_screen_resume(self) -> None:
        if self._sessions_timer is not None:
            self._sessions_timer.resume()

    async def _auto_reload_sessions(self) -> None:
        if not self.showing or self.tui.busy or self._reloading_sessions:
            return
        await self._reload_sessions_pane()

    async def _reload_sessions_pane(self) -> None:
        """Redraw the sessions pane from a fresh read, keeping the cursor on its row.

        Failure leaves the pane as it was: the rows already drawn are stale, not wrong, and
        the resting position must never break because a background read had a bad moment.
        """
        if self._reloading_sessions:
            return
        self._reloading_sessions = True
        try:
            records = await self.tui.load_sessions()
        except Exception:
            _LOG.exception("the dashboard sessions pane could not be reloaded")
            return
        finally:
            self._reloading_sessions = False
        pane = self.query_one("#sessions-pane", OptionList)
        held = pane.highlighted
        held_id = (
            pane.get_option_at_index(held).id
            if held is not None and pane.option_count > held
            else None
        )
        pane.clear_options()
        if not records:
            pane.add_option(Option(_NO_SESSIONS, id="empty", disabled=True))
            return
        for record in records:
            pane.add_option(
                Option(
                    session_row(record),
                    id=f"{_SESSION_KEY_PREFIX}{record.session_id}",
                )
            )
        if held_id is not None:
            for index in range(pane.option_count):
                if pane.get_option_at_index(index).id == held_id:
                    pane.highlighted = index
                    break
