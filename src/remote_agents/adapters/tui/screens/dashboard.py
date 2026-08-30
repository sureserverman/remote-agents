"""The resting position: projects on the left, sessions and notifications on the right.

Two screens live here. `ProjectsPaneScreen` is the projects position with the Launch-or-Resume
chooser in front of the wizard — the console's **left pane** in its entirety, and the base the
dashboard builds on. `DashboardScreen` is that plus the two right-hand regions, which is what
a bare terminal running `remote-agents tui` still shows.

`DashboardScreen` subclasses the projects picker rather than replacing it, so the filter,
its debounce, the catalogue refresh, and the never-empty stack guarantee are inherited —
what this module adds is the shape: a right-hand column showing the running sessions
(reloaded on reveal and on the same 10-second cadence the sessions list uses, which is also
where the console notices what the other writer did, since `load_sessions` is the sync choke
point) and the notifications feed pane, which reads the durable activity table.

The two lists share the base class's one row-selection handler; session rows carry a
namespaced key (`session:<id>`) so `choose` can route without a second dispatch chain.

DEC-009 note: the sessions pane's and feed pane's empty states are declared and tested
here, in this module's own tests — not by `test_empty_states.py`'s exhaustiveness sweep,
which walks `ChoiceScreen.empty_state` across the registry and therefore covers only the
dashboard's *projects* list. The sweep is exhaustive over positions, not over this
screen's auxiliary panes.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import replace
from math import ceil

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.timer import Timer
from textual.widgets import Footer, Header, Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from remote_agents.adapters.tui.model import _BACK, LaunchSelection, session_row
from remote_agents.adapters.tui.screens.base import (
    NEVER_EMPTY,
    ChoiceScreen,
    held_option_id,
    restore_highlight_by_id,
)
from remote_agents.adapters.tui.screens.feed import (
    _EMPTY_FEED_ROW,
    NO_NOTIFICATIONS,
    FeedRegion,
)
from remote_agents.adapters.tui.screens.launch import ProfilesScreen, ProjectsScreen
from remote_agents.adapters.tui.screens.resume import advance_to_resume_profiles
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.session_views import limit_lines

_LOG = logging.getLogger(__name__)

_SESSION_KEY_PREFIX = "session:"
_SESSIONS_AUTO_REFRESH = 10.0
#: The sessions pane's one line when nothing runs — DEC-009's answer for this pane.
_NO_SESSIONS = "No sessions are running."

NO_LIMITS = "No agent limits reported."
"""DEC-009's answer for the limits pane, in the vocabulary the readers already use.

Reached three ways that are one sentence to the owner: a host that wired no reader, a read
that raised, and every installed agent publishing nothing (`opencode` and `cursor-agent`
always, `codex` on a host that has not run it, `claude` when its borrowed cache went stale).
Distinguishing them here would report on this project's plumbing rather than on the owner's
plan.
"""

_EMPTY_LIMITS_ROW = "limits:none"
"""A stable id for the empty row, so a redraw can tell it from an agent's row."""

_LIMITS_ROW_PREFIX = "limits:"


class ProjectsPaneScreen(ProjectsScreen):
    """The projects position with the chooser in front of the wizard — the console's left pane.

    The projects picker on its own sends a chosen project straight into the agent list. This
    screen is the one that asks the Launch-or-Resume question first (DEC-033: navigation over
    flows both surfaces already have, never a new wizard step), and it exists as its own class
    because two surfaces need exactly that behavior: the console's **left pane**, which is its
    whole content, and the combined dashboard, which adds the two right-hand regions on top.

    `DashboardScreen` therefore subclasses *this* rather than `ProjectsScreen`. The alternative
    — each surface routing a chosen project itself — is one behavior written twice, and the two
    copies would only have to disagree once.
    """

    def __init__(self) -> None:
        super().__init__()
        #: What the console's start-only repair could not put right, held so it can be
        #: restated after every redraw — see `_restate_blocked`.
        self._blocked: tuple[str, ...] = ()

    async def populate(self) -> None:
        await super().populate()
        self._report_console_recovery()

    def render_projects(self, query: str = "", *, keep_focus: bool = False) -> None:
        super().render_projects(query, keep_focus=keep_focus)
        self._restate_blocked()

    def _report_console_recovery(self) -> None:
        """Tell the owner what the console's start-time repair did, and what it could not.

        The two halves get different destinations because they are different facts, and this
        surface already has a rule for each. `moved` is something that already happened and is
        finished — a confirmation, which `announce` puts in a toast. `blocked` is what needs a
        *person*; anything the owner must keep belongs in the status line, which is the rule
        the attach command is already handled by.

        Before this, neither reached them at all: `moved` was logged at INFO with no logging
        configured anywhere in `src/`, and `blocked` was printed to stderr in the instant
        before Textual took the alternate screen.
        """
        report = self.services.console_recovery
        if report is None:
            return
        for note in report.moved:
            self.tui.announce(f"The console was restored: {note}", severity="information")
        self._blocked = tuple(report.blocked)
        self._restate_blocked()

    def _restate_blocked(self) -> None:
        """Put the blocked notes back after a redraw, because the condition still holds.

        `render_projects` writes the ordinary status on every fill, so a note set once would
        be gone by the first refresh — and unlike a failed read, this is not a stale fact that
        a redraw supersedes. Nothing in this process is going to fix it.
        """
        if not self._blocked:
            return
        # An f-string rather than a concatenation, and the difference is not style: the
        # status-region sweep reads this call's first argument out of the AST to check that a
        # severity-coloured status carries words of its own, and a `BinOp` is a shape it
        # cannot read — so it reports it as colour with no words, correctly.
        notes = " · ".join(self._blocked)
        self.set_status(
            f"The console could not be fully restored: {notes}",
            severity="warning",
        )

    #: This pane is its own process's resting position, so escape here is inert and there is
    #: no other position in the process to return to.
    read_failure_route = "Ctrl+R re-reads this screen."

    async def choose(self, key: str) -> None:
        """A project row opens the chooser.

        The selection is deliberately not touched here: only Launch commits to a fresh one, so
        backing out of the chooser must leave nothing behind. Opening is not a stop, so the
        resting-cursor discipline (DEC-007) is untouched — no row here mutates anything.
        """
        project = next((item for item in self.tui.catalogue if item.opaque_id == key), None)
        if project is None:
            self.announce(
                "That project is no longer available. Refresh and try again.", severity="warning"
            )
            return
        await self.advance_to(ProjectChooserScreen(project))


class DashboardScreen(FeedRegion, ProjectsPaneScreen):
    """Three panes, one resting position; everything the projects picker was, plus sight."""

    position = "DASHBOARD"

    BINDINGS = [
        # Hidden from the footer: the bar is shared with every inherited binding and the
        # key only means something while the sessions pane holds a highlighted row — the
        # sessions pane's border title advertises it instead, where it is true.
        Binding("d", "session_detail", "Session detail", show=False),
    ]

    DEFAULT_CSS = """
    DashboardScreen #dashboard-panes { height: 1fr; }
    DashboardScreen #dashboard-left { width: 3fr; }
    DashboardScreen #dashboard-right { width: 2fr; }
    DashboardScreen #sessions-pane { height: 2fr; border: round $secondary; }
    DashboardScreen #limits-pane { max-height: 40%; border: round $secondary; }
    DashboardScreen #feed-pane {
        height: 1fr; border: round $secondary;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._sessions_timer: Timer | None = None
        self._reloading_sessions = False
        self._resumed_before = False

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
                    sessions.border_title = "Sessions — enter opens, d for detail"
                    yield sessions
                    # Between the sessions and the notifications, which is where the owner
                    # asked for it on 2026-08-29. Seeded with its empty state at compose time
                    # for the reason `#feed-pane` below is: `_reload_limits` returns early on
                    # an absent capability and on a raising read, so an unseeded pane would
                    # answer both with an empty box instead of the sentence DEC-009 requires.
                    #
                    # Every row is disabled. The pane reports a fact and offers no action, so a
                    # cursor resting here would answer Enter with silence -- which reads as a
                    # broken key rather than as a pane that never had anything to open.
                    limits = OptionList(
                        Option(NO_LIMITS, id=_EMPTY_LIMITS_ROW, disabled=True),
                        id="limits-pane",
                        markup=False,
                    )
                    limits.border_title = "Agent limits"
                    yield limits
                    # Seeded with its empty state at compose time, not left blank. `_reload_feed`
                    # returns early when the capability is absent or the read raises, and a
                    # `Static` used to carry this sentence as its initial content -- so an
                    # unseeded list would answer both of those with an empty box instead of the
                    # sentence DEC-009 requires this pane to declare.
                    feed = OptionList(
                        Option(NO_NOTIFICATIONS, id=_EMPTY_FEED_ROW, disabled=True),
                        id="feed-pane",
                        markup=False,
                    )
                    feed.border_title = "Notifications — enter expands"
                    yield feed
            with VerticalScroll(id="output-pane"):
                yield TextArea(
                    "", id="output", read_only=True, soft_wrap=True, highlight_cursor_line=False
                )
        yield Footer()

    async def choose(self, key: str) -> None:
        """A session row opens the session itself; anything else is a project row.

        A session row routes through the app's one open seam, so hosting decides what
        opening means — an exchange, or the exec handoff — and this screen never has to
        know. The project half is `ProjectsPaneScreen.choose`, unchanged and shared with the
        console's left pane rather than copied into it.
        """
        if key.startswith(_SESSION_KEY_PREFIX):
            await self.tui._open_or_leave(key.removeprefix(_SESSION_KEY_PREFIX))
            return
        await super().choose(key)

    async def action_session_detail(self) -> None:
        """`d` on the highlighted session row opens today's detail screen unchanged.

        Every stop, inspect, rename, and Remote Control affordance lives there, so the
        dashboard narrows nothing DEC-007's full control plane promised — opening is the
        fast path, the detail is one key away.
        """
        pane = self.query_one("#sessions-pane", OptionList)
        index = pane.highlighted
        if index is None or pane.option_count <= index:
            return
        key = pane.get_option_at_index(index).id
        if key is None or not key.startswith(_SESSION_KEY_PREFIX):
            return
        await self.tui.show_detail(key.removeprefix(_SESSION_KEY_PREFIX))

    async def populate(self) -> None:
        await super().populate()
        if self.services.open_in_console is not None:
            # The console's one root key, documented where the owner rests. It said "F12
            # returns to this dashboard", which was true of the tab model and is not true
            # now: F12 brings the console's **projects pane** back to the left slot, and
            # this combined dashboard is a different surface that a console never runs.
            # Under console hosting it is still worth naming, because the key is bound on
            # this server and will act whatever surface is looking at it.
            self.sub_title = "F12 shows the console's projects pane"
        await self._reload_sessions_pane()
        self._sessions_timer = self.set_interval(_SESSIONS_AUTO_REFRESH, self._auto_reload_sessions)

    async def on_reveal(self) -> None:
        await super().on_reveal()
        await self._reload_sessions_pane()

    def on_screen_suspend(self) -> None:
        if self._sessions_timer is not None:
            self._sessions_timer.pause()

    def on_screen_resume(self) -> None:
        if self._sessions_timer is not None:
            self._sessions_timer.resume()
        # The first resume is the screen's own activation at mount, where populate() has
        # just made (or is about to make) the first fill; scheduling another read there
        # doubled every startup — measured by the flaky-store test's read budget.
        if not self._resumed_before:
            self._resumed_before = True
            return
        # Resuming is also the moment the pane is most likely stale: the flow that just
        # popped may have launched or stopped a session, and the return path lands here
        # without passing the reveal hook. Waiting out the timer left the owner reading
        # "No sessions are running." for up to ten seconds beside a session that
        # already existed — measured live by the Stage 4 gate evaluator.
        self.call_later(self._reload_sessions_pane)

    async def _auto_reload_sessions(self) -> None:
        if not self.showing or self.tui.busy or self._reloading_sessions:
            return
        await self._reload_sessions_pane()

    async def _reload_limits(self) -> None:
        """Redraw the account-wide limits, or leave whatever is drawn exactly as it is.

        Failure leaves the pane alone, which is the same contract `_reload_sessions_pane` and
        `_reload_feed` state: the rows already drawn are stale, not wrong, and a background read
        having a bad moment must never blank a pane the owner is reading.

        The rows come from `limit_lines`, so this pane and the bot's block are one decision
        rendered twice rather than two renderers that agree today (DEC-043). What stays here is
        placement and the disabled-row rule -- the surface's half.
        """
        reader = self.services.backend.limits
        if reader is None:
            return
        try:
            entries = await reader()
        except Exception:
            _LOG.exception("the agent limits pane could not be reloaded")
            return
        lines = limit_lines(entries)
        pane = self.query_one("#limits-pane", OptionList)
        pane.clear_options()
        if not lines:
            # A *successful* read that found nothing is not the same event as a read that
            # raised, and this used to treat them alike -- it returned early, leaving the last
            # figures drawn. That is the routine state, not an exotic one: Claude's borrowed
            # cache is fenced at thirty minutes, so half an hour of idleness pinned a stale
            # percentage on screen permanently, countdown and all, with `(resets in 3h)` still
            # reading 3h four hours later. Worse, the bot correctly dropped its block at the
            # same instant, so the two surfaces asserted different things about one account --
            # the exact divergence sharing `limit_lines` exists to prevent.
            pane.add_option(Option(NO_LIMITS, id=_EMPTY_LIMITS_ROW, disabled=True))
            _fit_to_content(pane, (NO_LIMITS,))
            return
        for index, line in enumerate(lines):
            pane.add_option(Option(line, id=f"{_LIMITS_ROW_PREFIX}{index}", disabled=True))
        _fit_to_content(pane, lines)

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
        # The feed and the limits pane ride the same cadence: one schedule, three panes, so
        # the right column cannot drift out of step with itself.
        await self._reload_limits()
        # The feed rides the same cadence: one schedule, two panes. Its failure mode is
        # the placeholder, never a broken resting position. Known blind spot, inherited
        # from the pane's own suspend behavior: while a flow screen is pushed above the
        # dashboard the timer is paused, so no flash fires until the flow pops — the same
        # trade the sessions pane's staleness note records.
        await self._reload_feed()
        pane = self.query_one("#sessions-pane", OptionList)
        held_id = held_option_id(pane)
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
        # The cursor always rests somewhere (DEC-007's discipline, as show_choices keeps
        # it for every #choices list): on the row it held if that row survived the
        # rebuild, else on the first row — a pane advertising "enter opens" with no
        # highlighted row makes both keys silent no-ops until an arrow press.
        restore_highlight_by_id(
            pane, held_id, [f"{_SESSION_KEY_PREFIX}{record.session_id}" for record in records]
        )


def _fit_to_content(pane: OptionList, lines: Iterable[str]) -> None:
    """Give the pane exactly the rows its wrapped text needs, and no more.

    `height: auto` does not track an `OptionList`'s content here -- measured at several sizes,
    the pane held the same height with nothing drawn and with eight rows, so `max-height` alone
    decided it and the box sat mostly blank while taking rows from the two panes either side.
    Four readers is the production ceiling, so the blank-heavy render was the normal case, not
    an edge.

    Counting **wrapped** rows rather than lines is the part that has to be right. Sizing to
    `len(lines)` looked correct on a wide terminal and cut the continuation of every wrapped
    line on a narrow one -- which took the borrowed-cache stamp off the screen at 70 columns,
    the one thing DEC-061 requires presentation to say out loud. The width is read off the pane
    the way `_continuation_rows` reads the feed's, and is 0 until the first layout, where
    leaving the height alone lets `max-height` cover a frame nobody has seen yet.
    """
    rows = tuple(lines)

    def measure() -> None:
        width = pane.content_size.width
        if width <= 0:
            return
        pane.styles.height = sum(max(1, ceil(len(line) / width)) for line in rows) + 2

    # Deferred, because the first draw happens inside `populate()` -- before the pane has been
    # laid out, so its width is still 0 and a measurement taken there silently does nothing.
    # That is not a rare frame: it is the view the owner opens on, and it left the pane at its
    # `max-height` until the ten-second tick came round and re-measured it.
    pane.call_after_refresh(measure)


class ProjectChooserScreen(ChoiceScreen):
    """Launch new, or reopen saved — one question per project, then the existing flows.

    Navigation over flows both surfaces already have, never a new wizard step (DEC-033):
    Launch restarts the launch wizard with a fresh selection exactly as the projects
    picker always did, and Resume enters the same guarded capability fetch the resume
    flow's own project picker uses. A host with no conversations service never shows
    Resume at all — a dead-end entry is worse than an absent one (DEC-009's spirit).
    """

    #: Launch is always offered, so this position cannot be empty by construction.
    empty_state = NEVER_EMPTY
    position = "PROJECT_CHOOSER"
    status = "Launch a new session, or reopen a saved conversation."

    def __init__(self, project: CatalogProject) -> None:
        super().__init__()
        self.project = project

    @property
    def crumb(self) -> str:
        """The project just chosen — the one fact the trail cannot already carry."""
        return f"{self.project.area}/{self.project.name}"

    async def populate(self) -> None:
        self.hide_entry()
        entries: tuple[tuple[str, str], ...] = (("launch", "Launch a new session"),)
        if self.services.backend.conversations is not None:
            entries = (*entries, ("resume", "Resume a conversation"))
        self.show_choices((*entries, (_BACK, "Back")))

    async def choose(self, key: str) -> None:
        if key == _BACK:
            await self.tui.go_back()
            return
        if key == "launch":
            # A fresh selection rather than a patched one, exactly as the projects picker
            # committed it: an agent left from an abandoned pass must not survive.
            self.tui.selection = replace(LaunchSelection(), project=self.project)
            await self.advance_to(ProfilesScreen())
            return
        if key == "resume":
            await advance_to_resume_profiles(self, self.project)
