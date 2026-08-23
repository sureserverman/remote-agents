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
from textual.widgets.option_list import Option

from remote_agents.adapters.tui.context import FEED_LIMIT
from remote_agents.adapters.tui.model import age
from remote_agents.adapters.tui.screens.base import ChoiceScreen
from remote_agents.application.session_views import with_project_names
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


#: The row id for the pane's declared empty state. Disabled, so the cursor never rests on a
#: sentence it can press Enter on.
_EMPTY_FEED_ROW = "notification:none"

#: What every observation row's id starts with. `ChoiceScreen.on_option_list_option_selected`
#: routes an `OptionList` selection to its screen's `choose` by the option's id, so the prefix
#: is the whole of the dispatch -- the same seam the dashboard already tests `session:` on. No
#: new handler, and specifically no app-level one (`screens/base.py:952` says why).
NOTIFICATION_PREFIX = "notification:"


def feed_key(activity: AgentActivity) -> str:
    """A row id naming the observation, not its position.

    Composite of session, kind and `observed_at` rather than the index, because the feed is
    newest-first and grows at the *head*: an index-keyed row means the row under the owner's
    cursor silently becomes a different notification every time any agent reports. The same
    argument `_draw_listing` makes for restoring the sessions list by key.
    """
    return f"{NOTIFICATION_PREFIX}{activity.session_id}:{activity.kind.value}:{activity.observed_at.isoformat()}"


#: How much of an agent's line a row carries before it is cut. The pane is a third of one
#: column, so a row is a glance: enough to recognise the question, never the whole answer.
#: The rest is one keypress away (Stage 3) rather than gone -- DEC-037 keeps the words, and
#: this bounds only what is *drawn*, never what is stored.
DETAIL_WIDTH = 72


def _elide(text: str, width: int | None = DETAIL_WIDTH) -> str:
    """One line, cut at `width`, with an ellipsis when it was cut.

    Cut rather than wrapped, and that is the change. A wrapped detail is what the `Static`
    did: one 400-character answer became six lines and pushed every other observation out of
    a pane that holds twenty. One observation is one row.
    """
    collapsed = " ".join(text.split())
    if width is None or len(collapsed) <= width:
        return collapsed
    return f"{collapsed[: max(1, width - 1)]}…"


def feed_rows(
    activities: tuple[AgentActivity, ...],
    names: dict[str, str] | None = None,
    *,
    limit: int = _FEED_LIMIT,
) -> list[tuple[str, str]]:
    """One `(key, line)` per observation, newest first, bounded.

    `<age> · <identity> · <kind words> — <detail>`, where the identity is the session's own
    rendered name when `names` knows it and the bare session id when it does not.

    **The fallback is a feature, not a guard.** A notification outlives the session it is
    about -- that is what a durable feed is for -- so a row whose session has since been
    reconciled away must still say which one it was. Rendering an empty identity there would
    turn "the agent is waiting for an answer" back into the thing this row exists to stop
    being: a sentence about nobody.
    """
    names = names or {}
    rows = []
    for activity in activities[:limit]:
        words = KIND_WORDS.get(activity.kind, activity.kind.value)
        identity = names.get(str(activity.session_id)) or str(activity.session_id)
        detail = f" — {_elide(activity.detail)}" if activity.detail else ""
        # `_elide` caps the *detail* so one verbose agent cannot crowd out the identity that
        # makes the row readable at all. Cutting the line to the *pane* is not done here --
        # `_draw_feed` hands the widget a `Text` that truncates itself at whatever width it is
        # given, which is the only version that survives a resize.
        rows.append(
            (feed_key(activity), f"{age(activity.observed_at)} · {identity} · {words}{detail}")
        )
    return rows


def feed_lines(
    activities: tuple[AgentActivity, ...],
    names: dict[str, str] | None = None,
    *,
    limit: int = _FEED_LIMIT,
) -> list[str]:
    """One line per observation, newest first, bounded. The text half of `feed_rows`."""
    return [line for _key, line in feed_rows(activities, names, limit=limit)]


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

    async def _session_names(self) -> dict[str, str]:
        """`{session_id: rendered identity}` for every session the store still holds.

        Three deliberate choices, each of which the obvious alternative gets wrong.

        **`list_sessions()` raw, not `listed_sessions()`.** The latter pairs the read with a
        readiness refresh *and* filters to what a list should show, which is exactly ENDED
        removed (DEC-017). A notification outlives its session by design -- that is what a
        durable feed is for -- so the record needed to name a row is routinely one the
        sessions list has correctly stopped showing. Filtering here would break naming on the
        observations most worth reading: the ones about work that finished.

        **No `refresh_readiness`.** It rescans every record and runs a tmux capture per FAILED
        session. This pane repaints every ten seconds on two surfaces, so naming rows through
        the readiness pass would put a periodic tmux workload behind a pane whose whole job is
        to be glanced at. The feed reads; it does not probe.

        **A failure returns an empty index rather than propagating.** The rows then render
        their session-id fallback, which is the same contract the activity read above already
        has: what is drawn is stale, not wrong, and a background read having a bad moment must
        never break the position or empty the pane.
        """
        sessions = self.services.backend.sessions
        if sessions is None:
            return {}
        try:
            records = await sessions.list_sessions()
        except Exception:
            _LOG.exception("the feed could not read sessions to name its rows")
            return {}
        return {
            str(record.session_id): record.display.rendered
            for record in with_project_names(records, self.tui.catalogue)
        }

    async def choose(self, key: str) -> None:
        """Route a notification row here; hand every other key to the screen underneath.

        **On the mixin, not on either screen, and that is the whole point.** Both users list
        `FeedRegion` first in their bases -- `FeedScreen(FeedRegion, ChoiceScreen)` and
        `DashboardScreen(FeedRegion, ProjectsPaneScreen)` -- so this sits in front of both
        screens' own `choose` in the MRO and `super()` continues to whichever one that is. One
        implementation, two surfaces, and nothing in `dashboard.py` that a later change could
        edit on one side only. Stage 3's expansion lands here for the same reason.

        Without the branch a notification key fell through to the dashboard's project half,
        which looks it up in the catalogue, does not find it, and announces "That project is no
        longer available. Refresh and try again." -- a warning about a project, for a row that
        is not one. Reproduced before the fix.

        Selecting a notification does nothing yet; the row is the record and Enter gains its
        meaning in Stage 3. It is a deliberate no-op rather than an omission: the routing and
        what the routing *does* are separable, and the seam is worth landing on its own.
        """
        if key.startswith(NOTIFICATION_PREFIX):
            return
        await super().choose(key)

    @staticmethod
    def _draw_feed(pane: OptionList, rows: list[tuple[str, str]]) -> None:
        """Refill the pane, leaving the cursor on the observation it was on.

        By *key*, never by index. This pane repaints on a 10-second interval and the feed
        grows at the head, so every row the owner was reading shifts down whenever any agent
        reports -- restoring by index would hand them a different notification each tick,
        which is the same defect `SessionsScreen._draw_listing` restores by key to avoid.

        A key that has gone falls back to row 0, the resting position every other list on this
        surface uses (DEC-007). The cursor always rests somewhere.
        """
        held = pane.highlighted
        held_id = (
            pane.get_option_at_index(held).id
            if held is not None and held < pane.option_count
            else None
        )
        pane.clear_options()
        pane.add_options(Option(line, id=key) for key, line in rows)
        if not rows:
            return
        keys = [key for key, _line in rows]
        pane.highlighted = keys.index(held_id) if held_id in keys else 0

    async def _reload_feed(self) -> None:
        """Render the newest observations, or the placeholder — never an exception.

        A failed read leaves what is drawn alone: the rows already on screen are stale, not
        wrong, and a background read having a bad moment must never break the position.
        """
        if self._feed_news is None:
            self._feed_news = FeedNews()
        reader = self.services.backend.activity_feed
        pane = self.query_one("#feed-pane", OptionList)
        if reader is None:
            return
        try:
            activities = await reader()
        except Exception:
            _LOG.exception("the notifications feed could not be read")
            return
        if not activities:
            pane.clear_options()
            # DEC-009's answer, as a row rather than a paragraph -- and disabled, so the
            # cursor cannot come to rest on a sentence and answer Enter with nothing.
            pane.add_option(Option(NO_NOTIFICATIONS, id=_EMPTY_FEED_ROW, disabled=True))
            self._feed_news.arrived(activities)
            return
        self._draw_feed(pane, feed_rows(activities, await self._session_names()))

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
    read_failure_route = "Ctrl+R re-reads this screen."

    #: The same cadence the sessions pane keeps: one glance-level surface, one interval.
    _FEED_AUTO_REFRESH = 10.0

    DEFAULT_CSS = """
    FeedScreen #filter { display: none; }
    FeedScreen #choices { display: none; }
    FeedScreen #feed-pane { height: 1fr; text-wrap: nowrap; text-overflow: ellipsis; }
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
        # The keys are for the list, so the list takes them. `hide_entry` hides `#filter` and
        # `#choices` -- both composed only because `ChoiceScreen`'s machinery queries them by
        # id -- but hiding a widget does not move the focus off it, so the keyboard sat on a
        # `display: none` Input and Down did nothing until the owner pressed Tab. On a pane
        # whose entire content is one scrollable list, that is the scrolling this stage added
        # being one undiscoverable keystroke away.
        #
        # Here and not in `FeedRegion`: the dashboard shares the render and must *not* share
        # this, because there the filter legitimately owns the keyboard for typing a project
        # filter and the feed is three Tabs away by design. Both halves are pinned by tests.
        self.query_one("#feed-pane", OptionList).focus()
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
