"""The notifications feed: what an agent was last observed doing, newest first, inert.

The feed has lived as a region inside the combined dashboard since the durable table
landed. Under the three-pane console it is also a surface of its own, in a process of its
own — so what it renders is *shared* between the two rather than written twice: `FeedRegion`
holds the render and the news detector, and both screens mix it in.

A reader of `agent_activity` through the composition's `activity_feed` capability and
nothing else. It never drains the spool, because consuming spool files would starve the
phone's notifications. Text an agent produced reaches a `markup=False` `Static`, so it is
displayed and never interpreted (DEC-014's spirit, DEC-037's carriage of the agent's words).
"""

from __future__ import annotations

import logging

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Footer, Header, Input, OptionList, Static, TextArea

from remote_agents.adapters.tui.context import FEED_LIMIT
from remote_agents.adapters.tui.model import age
from remote_agents.adapters.tui.screens.base import ChoiceScreen
from remote_agents.ports.agent_activity import ActivityKind, AgentActivity

_LOG = logging.getLogger(__name__)

#: The feed's one line when nothing has been observed — its DEC-009 answer.
NO_NOTIFICATIONS = "No notifications yet."

#: A glance, not an archive — the number itself is shared with the reader's LIMIT.
_FEED_LIMIT = FEED_LIMIT

#: One line of owner-facing words per observation kind. Local to this surface on purpose:
#: the bot's sentences live in its own adapter and carry chat conventions (grouping,
#: standing messages) a glanceable feed line has no use for.
KIND_WORDS = {
    ActivityKind.COMPLETED: "the agent has finished its work",
    ActivityKind.LIMIT_REACHED: "the agent hit a usage limit",
    ActivityKind.OUTPUT_LIMIT: "the response hit its output ceiling",
    ActivityKind.NEEDS_ANSWER: "the agent is waiting for an answer",
    ActivityKind.QUIET: "the pane has gone quiet",
}


def feed_lines(activities: tuple[AgentActivity, ...], *, limit: int = _FEED_LIMIT) -> list[str]:
    """One line per observation, newest first, bounded."""
    lines = []
    for activity in activities[:limit]:
        words = KIND_WORDS.get(activity.kind, activity.kind.value)
        detail = f" — {activity.detail}" if activity.detail else ""
        lines.append(f"{age(activity.observed_at)} · {words}{detail}")
    return lines


class FeedNews:
    """Whether the head of a read is news the owner has not been told, or history.

    The first read is history whatever it holds — it renders (or primes an empty pane)
    without flashing. Everything after a primed read that moves the head is news, including
    the first row a fresh database ever gains.
    """

    def __init__(self) -> None:
        self._head: tuple[str, str, object] | None = None
        self._primed = False

    def arrived(self, activities: tuple[AgentActivity, ...]) -> AgentActivity | None:
        """The newest observation if it is news, else None; priming either way."""
        if not activities:
            self._primed = True
            return None
        newest = activities[0]
        head = (newest.session_id, newest.kind.value, newest.observed_at)
        arrived = self._primed and head != self._head
        self._head = head
        self._primed = True
        return newest if arrived else None


class FeedRegion:
    """Renders the durable feed into this screen's `#feed-pane`, and flashes only on news.

    A mixin rather than a base class because its two users differ in what else they are: the
    dashboard is the projects position with regions bolted on, and the feed pane is nothing
    but this. What they must not differ in is the rendering, which is why it lives here.
    """

    #: Per instance, created on first use so neither user has to remember an `__init__` call.
    _feed_news: FeedNews | None = None

    async def _reload_feed(self) -> None:
        """Render the newest observations, or the placeholder — never an exception.

        A failed read leaves what is drawn alone: the rows already on screen are stale, not
        wrong, and a background read having a bad moment must never break the position.
        """
        if self._feed_news is None:
            self._feed_news = FeedNews()
        reader = self.services.activity_feed
        pane = self.query_one("#feed-pane", Static)
        if reader is None:
            return
        try:
            activities = await reader()
        except Exception:
            _LOG.exception("the notifications feed could not be read")
            return
        if not activities:
            pane.update(NO_NOTIFICATIONS)
            self._feed_news.arrived(activities)
            return
        pane.update("\n".join(feed_lines(activities)))

        newest = self._feed_news.arrived(activities)
        flash = self.services.console_flash
        if newest is not None and flash is not None:
            try:
                await flash(KIND_WORDS.get(newest.kind, newest.kind.value))
            except Exception:
                _LOG.exception("the status flash failed; the feed row is the record")


class FeedScreen(FeedRegion, ChoiceScreen):
    """The console's right-bottom pane: the feed and nothing else.

    A `ChoiceScreen` because that is what carries this surface's chrome — the status region,
    the never-empty stack guarantee, `check_action`, the breadcrumb. Its choice list is
    composed and hidden: the machinery in `ChoiceScreen` queries `#filter`, `#choices` and
    `#output` by id and must find exactly what it expects, and a feed is not a list of
    choices.
    """

    #: This pane can be empty, and says so in its own words rather than through the choice
    #: list it does not use (DEC-009).
    empty_state = NO_NOTIFICATIONS

    position = "FEED"
    can_refresh = True
    crumb = "Notifications"
    status = "What the agents on this host were last observed doing."

    #: The same cadence the sessions pane keeps: one glance-level surface, one interval.
    _FEED_AUTO_REFRESH = 10.0

    DEFAULT_CSS = """
    FeedScreen #filter { display: none; }
    FeedScreen #choices { display: none; }
    FeedScreen #feed-pane { height: 1fr; padding: 0 1; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._timer = None

    def compose(self) -> ComposeResult:
        """The base body with the feed in place of the list; same ids, so nothing breaks."""
        yield Header()
        with Vertical(id="body"):
            yield Static(self.status, id="status", markup=False)
            yield Input(placeholder="", id="filter")
            yield OptionList(id="choices", markup=False)
            feed = Static(NO_NOTIFICATIONS, id="feed-pane", markup=False)
            feed.border_title = "Notifications"
            yield feed
            with VerticalScroll(id="output-pane"):
                yield TextArea(
                    "", id="output", read_only=True, soft_wrap=True, highlight_cursor_line=False
                )
        yield Footer()

    async def populate(self) -> None:
        self.hide_entry()
        await self._reload_feed()
        if self._timer is None:
            self._timer = self.set_interval(self._FEED_AUTO_REFRESH, self._auto_reload)

    async def on_reveal(self) -> None:
        await self._reload_feed()

    async def refresh_contents(self) -> None:
        """Ctrl+R re-reads the table, which is the whole of what this position shows."""
        await self._reload_feed()

    def on_screen_suspend(self) -> None:
        if self._timer is not None:
            self._timer.pause()

    def on_screen_resume(self) -> None:
        if self._timer is not None:
            self._timer.resume()

    async def _auto_reload(self) -> None:
        if not self.showing or self.tui.busy:
            return
        await self._reload_feed()
