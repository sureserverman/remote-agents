"""Live private-bot polling boundary with exact owner/chat authorization."""

from __future__ import annotations

import asyncio
import io
import logging
import signal
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from html import escape
from math import ceil

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeChat,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.inspection import inspect_capture
from remote_agents.adapters.telegram.live_view import ChatViewStore, LiveView
from remote_agents.adapters.telegram.notifications import (
    NOTIFIED_DETAIL_ACTION as _NOTIFIED_DETAIL,
)
from remote_agents.adapters.telegram.notifications import (
    ActivityNotifier,
    StandingNotificationStore,
)
from remote_agents.adapters.telegram.presenters import (
    Button,
    RenderedMessage,
    render_message,
    uniform_keyboard,
)
from remote_agents.adapters.telegram.stops import CONFIRMED_FORCE, StopController
from remote_agents.application.backend import Backend
from remote_agents.application.commands import (
    AnswerTrustCommand,
    InspectQuery,
    LaunchCommand,
    RemoteControlCommand,
    ResumeCommand,
)
from remote_agents.application.conversations import (
    ConversationCatalogueQuery,
    resume_available,
)
from remote_agents.application.errors import ProjectCreationError, SessionNotFoundError
from remote_agents.application.host_remote_control import (
    HOST_REMOTE_CONTROL_LABELS,
    HOST_REMOTE_CONTROL_TITLE,
    HostRemoteControlCommand,
    PairCommand,
    host_remote_control_directions,
    pair_available,
)
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_admin import CreateProjectCommand
from remote_agents.application.project_catalog import (
    CatalogProject,
    paginate_catalogue,
    rank_if_usage_is_reported,
    search_catalogue,
)
from remote_agents.application.resume_flow import RESUME_PAGE_SIZE, resume_capable
from remote_agents.application.session_actions import (
    ACTION_LABELS,
    CLEANUP,
    FORCE,
    GRACEFUL,
    REMOTE_CONTROL_LABELS,
    StopFailure,
    available_actions,
    explain_state,
    notifiable,
    pane_is_attachable,
    remote_control_available,
    remote_control_directions,
    state_word,
    trust_available,
)
from remote_agents.application.session_views import (
    StateGroup,
    group_counts,
    group_emoji,
    limit_rows,
    listed_sessions,
    only_listed,
    selectable_area,
    session_identity,
    session_lines,
    session_row,
    session_row_parts,
    state_emoji,
    usage_lines,
    with_project_names,
)
from remote_agents.application.stops import execute_stop
from remote_agents.config import TelegramSecrets
from remote_agents.domain.conversations import ConversationReference
from remote_agents.domain.models import (
    OrphanProvenance,
    ProfileId,
    ProjectId,
    SessionId,
    SessionRecord,
    SessionState,
    normalize_label,
)
from remote_agents.domain.projects import ProjectIdentity
from remote_agents.domain.remote_control import (
    HostConnection,
    HostRemoteControlStatus,
    RemoteControlState,
)
from remote_agents.domain.trust import TRUST_ANSWERABLE, TrustState
from remote_agents.ports.agent_usage import ContextWindow
from remote_agents.ports.callback_state import CallbackStatePort
from remote_agents.ports.chat_view import ChatViewPort
from remote_agents.ports.standing_notification import StandingNotificationPort
from remote_agents.ports.terminal import TerminalTargetMissing

__all__ = [
    "PrivateBotBoundary",
    "build_private_bot",
    "run_private_bot",
    "session_lines",
    "session_row",
]
"""`session_row` and `session_lines` are re-exported, not used: `tests/contract/
test_session_row_parity.py` imports both row renderers through this surface to assert that the
bot has no copy of its own, and an import that is merely resolvable is the whole of that claim."""

_BOT_DESCRIPTION = "Private control for curated local agent sessions."
_BOT_SHORT_DESCRIPTION = "Private local agent-session control"
_LOG = logging.getLogger(__name__)
_OWNER_COMMANDS = (
    BotCommand("launch", "Launch a curated agent"),
    BotCommand("resume", "Resume a saved conversation"),
    BotCommand("sessions", "View managed sessions"),
    BotCommand("help", "Show available actions"),
)
_HOST_REMOTE_COMMAND = BotCommand("remote", HOST_REMOTE_CONTROL_TITLE)
"""The one command this bot lists conditionally, named by `application` rather than here.

The description is the shared title and not a literal, for the reason the direction labels
are shared: an owner who already knows the pane toggle would read a bare "Remote Control" as
that one, and the two must not be able to drift apart in this file (DEC-007).

Conditional because `/resume`'s asymmetry does not apply. Resume is listed on every host and
answers "unavailable" where it is not wired, which is a sentence rather than a dead end; this
one is offered only where a provider declared the capability, because a host with no `codex`
has no relay to enrol with, and an entry whose only possible answer is no is worse than none.
"""


def owner_commands(backend: Backend) -> tuple[BotCommand, ...]:
    """The command menu this composition should publish, given what it actually wired."""
    if backend.host_remote_control is None:
        return _OWNER_COMMANDS
    return _OWNER_COMMANDS + (_HOST_REMOTE_COMMAND,)


_GUIDED_TEXT_ENTRY = {
    "launch.search": (
        "Reply with a project name. Send Cancel or Back to leave this step.",
        "Project name",
    ),
    "resume.search": (
        "Reply with a project name. Send Cancel or Back to leave this step.",
        "Project name",
    ),
    "project.name": (
        "Reply with the new project name. Send Cancel or Back to leave this step.",
        "New project name",
    ),
    "session.rename": (
        "Reply with a name for this session. Send Skip to clear it, or Cancel to leave it.",
        "Session name",
    ),
}
_ENTRY_INSTRUCTIONS = {
    "launch.search": "Reply below with a project name.",
    "resume.search": "Reply below with a project name.",
    "project.name": "Reply below with the new project name.",
    "session.rename": "Reply below with a name for this session.",
}
_SEARCH_ACTIONS = {"launch.search": "launch", "resume.search": "resume"}
_TEXT_ENTRY_ACTIONS = frozenset(
    {"launch.search", "resume.search", "project.area", "session.rename"}
)
"""The actions that open a guided step, and so the only ones that may leave a box open."""


@dataclass(frozen=True, slots=True)
class _ProjectPicker:
    """The callbacks and wording one project list needs, so both flows share a renderer.

    Launch and resume both begin by choosing a project from the same catalogue, and only
    the action each button carries differs. Resume had its own renderer that emitted one
    button per project for the whole catalogue with no paging and no search: 95 rows
    against launch's 14 on this host, and Telegram refuses a keyboard past 100 buttons, so
    it would have stopped rendering entirely a few projects later.
    """

    select: str
    page: str
    search: str
    title: str
    instruction: str
    creates_projects: bool = False
    """Whether this flow may offer Add Project beside Search.

    Launch only. You cannot resume a prior conversation in a project that does not exist
    yet, so offering it there would be a route to a guaranteed dead end -- and since both
    flows share one renderer, that is exactly the regression sharing invites.
    """


_PROJECT_PICKERS = {
    "launch": _ProjectPicker(
        select="launch.project",
        page="launch.page",
        search="launch.search",
        title="Projects",
        instruction="Select a project to launch.",
        creates_projects=True,
    ),
    "resume": _ProjectPicker(
        select="resume.project",
        page="resume.projects",
        search="resume.search",
        title="Resume",
        instruction="Select the project for the prior conversation.",
    ),
}


@dataclass(frozen=True, slots=True)
class _TextEntry:
    """One guided text step: what is being asked, what for, and where the input box is.

    The input box's message id lives here rather than in a second dictionary keyed the same
    way, because it has exactly the same lifetime as the request — created when the step
    opens, dead when the step is answered or abandoned.

    Process-local, like the request itself. **Accepted cost, stated plainly because it is
    the one hole left in this stage's invariant:** a restart between opening a step and
    answering it forgets both what was asked and where the box is, and the Bot API gives no
    way to enumerate a chat, so nothing can find that box again. It stays until the owner
    removes it by hand. Every in-process way of abandoning a step — any command, any button
    that navigates away, opening another step — does take it with it; only a restart in
    that window does not. Closing it properly means giving this id the same durable home
    the anchor already has, which is a schema change this stage did not carry.
    """

    action: str
    entity_id: str
    input_message_id: int = 0


_PENDING_NOTICES = {
    "graceful": "Stopping the session — waiting for the agent to exit…",
    "cleanup": "Cleaning up the session…",
    CONFIRMED_FORCE: "Force stopping the session…",
    "launch.profile": "Launching — waiting for the agent to become ready…",
    "resume.confirm": "Resuming — waiting for the agent to become ready…",
}
"""The actions that make the owner wait, and what to show them while they do.

Each of these reaches a terminal and then polls it: a launch waits for its profile's
readiness marker, a graceful stop waits for the pane to exit, and both are bounded by the
same startup timeout — twenty seconds in the deployed composition. Everything absent from
this table answers from the store or from one tmux call, fast enough that a notice would
flash and be gone.
"""

_SESSION_ENDING_ACTIONS = frozenset({GRACEFUL, CLEANUP, CONFIRMED_FORCE})
"""The actions after which a session is no longer one this service speaks first about.

The same three members as `_LIST_LANDING_ACTIONS` below and deliberately a separate name: that
one is about which *screen* to draw next, this one about which messages have stopped being
true. Bare `FORCE` is absent from both, and for the same reason -- it draws the confirmation,
so nothing has happened yet.

What it triggers is about timing and nothing more. `ActivityNotifier.retire_finished` runs on
the delivery pass regardless, because the local console ends sessions in another process and
this handler never hears about those; sweeping here as well is what makes the owner watch their
own stop take its notification with it, rather than find it gone half a minute later.
"""

_LIST_LANDING_ACTIONS = frozenset({GRACEFUL, CLEANUP, CONFIRMED_FORCE})
"""The actions that draw the **session list** rather than a screen about their own session.

`_release_attachment` is told what the next screen is about, and every other action can
answer that with the entity it carries. These cannot: they carry a session id and then land
somewhere that is not about it, so a captured document would be retained on behalf of a
session the owner can no longer see. Unconfirmed `FORCE` is deliberately absent — it draws
the confirmation, which *is* about that session.
"""


_GROUP_TITLES: dict[StateGroup, str] = {
    StateGroup.ACTIVE: "ACTIVE",
    StateGroup.IN_TRANSITION: "IN TRANSITION",
    StateGroup.NEEDS_ATTENTION: "NEEDS ATTENTION",
    StateGroup.PRESERVED: "PRESERVED",
}
"""What each bucket is headed on the sessions list. The bucket is the shared decision
(`session_views.StateGroup`); the heading is this surface's sentence (DEC-043)."""

_ACTION_EMOJI: dict[str, str] = {
    GRACEFUL: "\u23f9",  # ⏹
    CLEANUP: "\U0001f9f9",  # 🧹
    FORCE: "\u26d4",  # ⛔
}
"""The mark in front of each stop button. `ACTION_LABELS` stays the shared word; the mark is
the bot's, because Telegram has no separator and shape plus a glyph is what tells a stop from a
read on a keyboard. `action_button_label` is the one composer, and `unmarked` its inverse."""

_INSPECT_EMOJI = "\U0001f4c4"  # 📄
_RENAME_EMOJI = "\u270f\ufe0f"  # ✏️
_ATTACH_EMOJI = "\U0001f4ce"  # 📎
_REMOTE_EMOJI = "\U0001f4e1"  # 📡
_BACK_TO_SESSIONS = "\u2039 Back to sessions"  # ‹ Back to sessions

_REMOTE_CONTROL_WORDS: dict[RemoteControlState, str] = {
    RemoteControlState.ACTIVE: "on",
    RemoteControlState.INACTIVE: "off",
}
"""How the detail's `remote` fact line reads the last observation. Anything else is `unknown`."""

_HOST_CONNECTION_WORDS: dict[HostConnection, str] = {
    HostConnection.CONNECTED: "on",
    HostConnection.CONNECTING: "on, still connecting",
    HostConnection.DISABLED: "off",
    HostConnection.DAEMON_ABSENT: "unknown, no daemon is running",
    HostConnection.ERRORED: "on, but the link is broken",
    HostConnection.UNREACHABLE: "unknown, codex never replied",
}
"""The one-line reading of this machine, for the status line both screens share.

Six words for six connections, and no two the same. The two pairs that are easiest to
collapse are the two that must not be:

* `DAEMON_ABSENT` is not `off`. The domain says why in its own words -- the enrollment
  outlives the process that serves it, so a host whose flag is on with its daemon down is one
  daemon start from reachable, and "off" is the direction of wrongness an owner acts on by
  not acting.
* `UNREACHABLE` is not `ERRORED`. One is the daemon reporting its own broken link; the other
  is this project never having reached `codex` at all.

The words themselves are this surface's (DEC-043) -- only the title and the direction labels
come from `application`, because those are what the two surfaces must spell identically.
"""

_HOST_CONNECTION_EXPLANATIONS: dict[HostConnection, str] = {
    HostConnection.CONNECTED: "This machine is enrolled, and a paired phone can reach it.",
    HostConnection.CONNECTING: (
        "The setting has taken and the link to the relay is still settling."
    ),
    HostConnection.DISABLED: "This machine is not enrolled, so no phone can reach it.",
    HostConnection.DAEMON_ABSENT: (
        "The codex daemon is not running here, so nothing can say whether this machine is "
        "enrolled. The setting outlives the daemon, so this is not the same as being off."
    ),
    HostConnection.ERRORED: (
        "The codex daemon answered, and reported that its own link to the relay is broken."
    ),
    HostConnection.UNREACHABLE: (
        "This machine could not talk to codex at all, so nothing was read and nothing is "
        "known -- codex may not be installed here."
    ),
}
"""The sentence under the reading, which is where a non-expert learns what to do next.

`UNREACHABLE` names the missing program on purpose. Before this member existed the surface
rendered "errored" for a host where `codex` was simply not installed, and an owner could
press the toggle forever without ever being told that.
"""

_HOST_DIRECTION_CAUTIONS: dict[RemoteControlState, str] = {
    RemoteControlState.ACTIVE: (
        "This enrols the whole machine with the relay, so a paired phone can drive every "
        "codex session on it."
    ),
    RemoteControlState.INACTIVE: (
        "This unenrols the whole machine, so a paired phone stops reaching any codex session on it."
    ),
}
"""What the confirmation warns about: the subject is the machine, not one pane.

The pane toggle's confirmation says "this uses only the verified Claude interaction", which is
a statement about a keystroke into one session. Nothing about that reassurance is true here,
and reusing its shape would have been the whole mistake this screen exists to avoid.
"""


_FACT_LABEL_WIDTH = 8
"""`context`, `remote`, `pane` padded to eight columns, so the values in the detail's fact block
start in one column."""


def action_button_label(action: str) -> str:
    """`⏹ Stop and close` -- the shared label behind this surface's mark."""
    return f"{_ACTION_EMOJI[action]} {ACTION_LABELS[action]}"


def unmarked(label: str) -> str:
    """Recover the shared label from a button this surface marked, for anything decoding one.

    The inverse of `action_button_label` and of the read-action marks, and safe on a label that
    carries none: a mark is exactly one leading token followed by a space, and no shared label
    begins with one.
    """
    head, separator, rest = label.partition(" ")
    marks = {*_ACTION_EMOJI.values(), _INSPECT_EMOJI, _RENAME_EMOJI, _ATTACH_EMOJI, _REMOTE_EMOJI}
    return rest if separator and head in marks else label


@dataclass(slots=True)
class PrivateBotBoundary:
    """Authorize the one configured private chat before handling any supported action."""

    owner_user_id: int
    owner_chat_id: int
    backend: Backend = field(default_factory=Backend)
    """Every use case this bot may drive, as the composition root assembled them (ARCH-B1).

    Four fields used to stand here instead — `launcher`, `creator`, `conversations`,
    `capture` — and the first two were declared as a bare optional `object`, which is a slot
    rather than a type. Nothing named what a launcher was, so five places asked it by
    `getattr` whether it could rename, inspect, copy an attach command, read a trust state,
    report project usage; and a composition root that forgot one of those got the same
    silence as a host that genuinely had none.

    Absence is still representable, because it is still real: `Backend()` is a host that
    wired nothing, and `help_command` lists only what this composition actually carries. The
    difference is that the absence now has a name and a declared field, instead of being the
    answer a probe gives when it cannot find a method.

    `profiles` is a field here rather than on `Backend` for a reason that has since been
    answered: `Backend.profiles` held the domain `ProfileCompatibility` and each surface
    narrowed it separately, which is how a version probe that merely timed out once took the
    local surface down. `compose_backend` now narrows once into
    `application.profiles.ProfileAvailability`, and this field is seeded straight from
    `backend.profiles`.
    """
    profiles: tuple[ProfileAvailability, ...] = ()
    catalogue: tuple[CatalogProject, ...] = field(init=False)
    """The catalogue as currently drawn, seeded from the backend and re-ranked in place.

    Not a `Backend` field read through: `Backend` is frozen because a process composes it
    once, while this is what the last refresh produced. `_refresh_catalogue` writes it off
    the event loop; every screen reads it.
    """
    callbacks: CallbackStatePort = field(default_factory=CallbackStateStore)
    anchors: ChatViewPort = field(default_factory=ChatViewStore)
    standing: StandingNotificationPort = field(default_factory=StandingNotificationStore)
    """Which message each session's notification is, so a restart amends it rather than
    sending a second one beside it. Defaulted to the in-memory sibling for the same reason
    `callbacks` and `anchors` are; the service is handed the durable store.
    """
    stops: StopController = field(init=False)
    view: LiveView = field(init=False)
    notifier: ActivityNotifier = field(init=False)
    """The three collaborators, filled by `build_private_bot` rather than by this class.

    `init=False` and genuinely unset until the factory runs, which is the honest state: a
    bare `PrivateBotBoundary(...)` is a boundary nobody has wired. Defaulting them instead
    would be worse than either — it would give a composition root that never chose a live
    view one that silently works, which is exactly the situation moving the wiring out was
    meant to end.
    """
    _awaiting_text: dict[tuple[int, int], _TextEntry] = field(default_factory=dict)
    _attachment: tuple[str, int] | None = None
    _project_views: dict[str, tuple[CatalogProject, ...]] = field(default_factory=dict)
    _flow: str | None = None
    """Which of the bar's three flows the screen being drawn belongs to, or None for neither.

    Read only by `_message`, to mark the tab the owner is standing in. Telegram will not
    style a pressed button, so without a marker three identical rows on a dozen screens stop
    saying anything about where you are.

    Process-local render state, like `_sessions_page` and for the same reason: it describes
    the screen currently drawn, and a restart has no screen to describe. A press that
    outlives one falls back to an unmarked bar, which is cosmetic and self-correcting on
    the next render.

    One write outside a render would be a race, and the reason there cannot be one is not
    local to this class: `run_private_bot` builds the application with
    `concurrent_updates(False)`, so updates are handled one at a time. That is already
    load-bearing for token binding — the comment there says so — and this field now rests
    on it too. A change made for throughput would give two interleaved presses one shared
    marker, which is the cheap half of what it would break. Both arguments were comments
    only until `test_the_bot_handles_updates_sequentially` pinned the literal.
    """
    _sessions_page: int = 1
    """The page number the sessions list is currently drawn at, so Back can return to it.

    Written by `_sessions_reply` — the one place that renders the list — and read only by the
    session detail, whose Back used to point at `sessions.open` and so dropped the owner on
    page 1 whatever page they had opened the row from. That was survivable while Refresh
    existed to re-read a page in place; removing Refresh made it the only way back, so it has
    to land where the owner was.

    Deliberately *what is on screen* rather than *where the owner has been*. A stop renders
    the list at page 1, and this becomes 1 with it, because the detail's Back must agree with
    the list the owner is actually looking at.

    Process-local, like `_project_views`, and for the same reason: it describes a render, and
    a restart has no render to describe. A detail button that outlives one falls back to
    page 1, which is exactly where Back went before this existed.
    """
    project_page_size: int = 10
    session_page_size: int = 8

    def __post_init__(self) -> None:
        # Seeded, not aliased. `Backend.catalogue` is the snapshot the process composed
        # with; this is the one on screen, and `_refresh_catalogue` replaces it.
        self.catalogue = self.backend.catalogue

    async def refresh_catalogue(self) -> None:
        """Re-read the projects so one created at runtime becomes selectable immediately.

        The registry read and development-root walk run off the event loop, so refreshing
        never stalls unrelated Telegram interactions or tmux polling.

        The recency ranking is applied **here**, once, rather than in either picker. Launch,
        Resume and search all render `self.catalogue` — the two pickers share `_projects_reply`
        and search filters the same tuple — so ordering it at the source reaches all three
        without a ranking call per rendered row, and without either picker knowing that a
        ranking exists. It is also why a session launched during the run changes the next
        render's order: the usage read happens on the refresh that follows it.

        Called when a picker **opens** — `launch.open`, `resume.open`, `/launch` — and not on
        a timer. `self.catalogue` is read by those two screens and by search, so re-reading it
        where it is about to be rendered makes it fresh exactly when that matters, at a moment
        the owner is already waiting for a screen to draw. Before this it was refreshed only by
        `nav.refresh` and by the end of project creation, which left one real gap: a project
        created outside the bot stayed invisible until the owner thought to press Refresh.

        Deliberately not called from `launch.page` or `resume.page`. Paging must not re-read —
        this clears `_project_views` and re-ranks, so a refresh under a thumb would reshuffle
        the list being paged through. Opening is the boundary where a new order is expected.
        """
        if self.backend.refresh_catalogue is None:
            return
        catalogue = await asyncio.to_thread(self.backend.refresh_catalogue)
        # `now` is read here, at the one place with a reason to know the time; the wrapper
        # and the ranking behind it both stay pure. The rule itself moved to
        # `application/project_catalog.py` when the local surface started asking it too
        # (DEC-043) -- this adapter asks the shared rule and keeps its own sentence.
        self.catalogue = await rank_if_usage_is_reported(
            catalogue, self.backend.sessions, datetime.now(UTC)
        )
        self._project_views.clear()

    def permits(self, update: Update) -> bool:
        user = update.effective_user
        chat = update.effective_chat
        return (
            user is not None
            and chat is not None
            and user.id == self.owner_user_id
            and chat.id == self.owner_chat_id
            and chat.type == "private"
        )

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Land on the sessions list, which is what this bot is for between launches.

        Unconditionally, rather than "sessions if anything is running, else launch". A
        landing screen that moves with state is one nobody can build muscle memory for, and
        it would only save a press on a cold start -- the rarest arrival there is. The bar
        puts Launch one press from an empty list.

        Sessions rather than Launch because the frequencies are not close: a session is
        launched once and looked at many times, the counts Home used to carry are counts of
        sessions, and a notification summons the owner *about a session* -- so landing
        anywhere else would make the summons and the landing disagree.
        """
        del context
        if not self.permits(update) or update.effective_message is None:
            return
        self._flow = "sessions"
        await self._answer_command(
            update.effective_message, _reply_arguments(await self._sessions_reply())
        )

    async def _answer_command(self, message, arguments: dict[str, object]) -> None:
        """Draw a command's answer into the live view and take the command back out of the chat.

        Render first, delete second. If the render fails, the owner still has the message
        they sent and can see that nothing answered it; the other order would consume the
        command and leave the chat silent about what happened to it. A delete that fails
        after a successful render is the harmless direction — the screen is right and one
        stale command line survives — which is why `discard` swallows it.

        A command used to `reply_text`, which is what made the chat a transcript: four
        commands meant four screens, and since Stage 1 every one of them kept working
        buttons.

        Takes the message rather than the update because every caller has already narrowed
        it, and a second check here would be a branch no test could ever reach.
        """
        bot = message.get_bot()
        await self.view.render(bot, arguments)
        await self._release_attachment(bot, None)
        await self._abandon_entry(bot)
        await self.view.discard(bot, message.message_id)

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Accept bounded local catalogue search or a session label while explicitly requested.

        Every path here ends the same way: the answer is drawn into the live view, and the
        question and the answer both leave the chat. What used to happen instead is that
        each reply added two more messages — the owner's, and a fresh screen — so a search
        that took three attempts left seven things behind it.
        """
        del context
        if not self.permits(update) or update.effective_message is None:
            return
        entry = self._awaiting_text.get(self._entry_key)
        if entry is None:
            return
        message = update.effective_message
        bot = message.get_bot()
        value = message.text or ""
        if value.casefold() in {"cancel", "back"}:
            # Back to the screen the step was opened from, not out of the flow entirely.
            # The instruction offers these two words to leave "this step" — *this step*, and
            # a word that walks the owner out of the whole flow is not the word they were
            # offered. It used to answer with Home, which was defensible while Home was the
            # root of everything; the sessions list is the root of nothing here.
            self._flow = _flow_of(entry.action)
            await self._finish_entry(bot, entry, message, await self._entry_landing(entry))
            return
        if entry.action in _SEARCH_ACTIONS:
            projects = search_catalogue(self.catalogue, value)
            if not projects:
                await self._ask_again(bot, entry, message, "No projects found. Try another name.")
                return
            # The search returns to the flow it was opened from, so a project picked here
            # resumes a conversation rather than silently starting a fresh session.
            await self._finish_entry(
                bot,
                entry,
                message,
                _reply_arguments(
                    self._projects_reply(
                        projects, view_id="search", flow=_SEARCH_ACTIONS[entry.action]
                    )
                ),
            )
            return
        if entry.action == "project.name":
            try:
                identity = ProjectIdentity(area=entry.entity_id, name=value.strip())
            except ValueError as error:
                await self._ask_again(bot, entry, message, str(error))
                return
            await self._finish_entry(
                bot, entry, message, _reply_arguments(self._project_review_reply(identity))
            )
            return
        if entry.action == "session.rename":
            # A host with no session use case cannot rename, and must say so rather than
            # raise mid-step: raising here leaves the input box open and every later reply
            # re-raising, which is exactly the failure Stage 2's Critical was. This used to
            # ask the launcher by name whether it could rename at all — a question that
            # could not distinguish that host from a composition root that forgot to wire
            # one.
            sessions = self.backend.sessions
            if sessions is None:
                await self._finish_entry(
                    bot, entry, message, _reply_arguments(self._message("Renaming is unavailable."))
                )
                return
            # "Skip" leaves the session as it is and closes the step. Clearing a name is a
            # different intent from declining to set one, and the store supports it
            # (`set_label(None)`) — but no screen offers it yet, so a step that quietly
            # cleared on Skip would be the only way to lose a name and would do it by
            # accident.
            if value.casefold() == "skip":
                await self._finish_entry(
                    bot, entry, message, _reply_arguments(await self._detail_reply(entry.entity_id))
                )
                return
            try:
                label = normalize_label(value, max_length=self.backend.max_label_length)
            except ValueError:
                await self._ask_again(
                    bot,
                    entry,
                    message,
                    f"Use a visible name of up to {self.backend.max_label_length} characters.",
                )
                return
            try:
                await sessions.rename(SessionId.parse(entry.entity_id), label)
            except (SessionNotFoundError, KeyError):
                # The session ended under the owner while the box was open. Its detail screen
                # is gone too, so the list is the only honest place to land.
                #
                # Both types, and neither is redundant: `SessionService.rename` raises
                # `SessionNotFoundError` from its own `_require_session`, while the store port
                # raises `KeyError`. They are **siblings** under `LookupError`, not one a
                # subclass of the other, so catching only `KeyError` caught nothing that this
                # path can actually raise — which is how this branch shipped as dead code
                # behind a green test whose double raised the wrong type.
                await self._finish_entry(
                    bot,
                    entry,
                    message,
                    _reply_arguments(
                        await self._sessions_reply(notice="That session is no longer available.")
                    ),
                )
                return
            await self._finish_entry(
                bot,
                entry,
                message,
                _reply_arguments(await self._detail_reply(entry.entity_id)),
            )
            return
        # Every remaining text step returns above. A step that reaches here is one whose
        # action was added to `_TEXT_ENTRY_ACTIONS` without a branch to answer it, which would
        # otherwise consume the owner's reply and draw nothing.
        raise AssertionError(f"no text handler for {entry.action!r}")

    @property
    def _entry_key(self) -> tuple[int, int]:
        return (self.owner_user_id, self.owner_chat_id)

    async def _entry_landing(self, entry: _TextEntry) -> dict[str, object]:
        """Where abandoning a guided step puts the owner: the screen that opened it.

        One place rather than four call sites, because "Cancel" and "Back" arrive by typed
        text and by button and must agree about where they go.
        """
        if entry.action == "session.rename":
            return _reply_arguments(await self._detail_reply(entry.entity_id))
        if entry.action == "project.name":
            # `project.name` is what the *step* is called; `project.area` is the button that
            # opens it. Abandoning the name returns to the area picker that asked for it.
            return _reply_arguments(await self._project_areas_reply())
        flow = _SEARCH_ACTIONS.get(entry.action, "launch")
        if flow == "resume":
            return _reply_arguments(self._resume_projects_reply())
        return _reply_arguments(self._projects_reply(self.catalogue, view_id="all"))

    async def _finish_entry(self, bot, entry: _TextEntry, message, arguments) -> None:
        """Draw the answer, then take the question and the answer out of the chat.

        Render first for the same reason a command does: if the screen cannot be drawn, the
        owner keeps what they typed and can see that nothing came of it.
        """
        await self.view.render(bot, arguments)
        self._awaiting_text.pop(self._entry_key, None)
        await self._release_attachment(bot, None)
        await self._clear_entry(bot, entry, message)

    async def _ask_again(self, bot, entry: _TextEntry, message, notice: str) -> None:
        """Refuse a value and ask again, without leaving the refusal or the old question.

        A rejected attempt is still a consumed input — it was read, judged, and answered —
        so it goes, and the box it replied to goes with it. What replaces them is one new
        box, so three failed attempts cost the chat exactly what one does.

        Ask before clearing, the same order `_finish_entry` uses and for the same reason. If
        the new box cannot be sent, the owner keeps the old one and what they typed, and can
        try again; clearing first would leave them with no way to answer a step the service
        still believes is open. The cost is that both boxes exist for one call, which nobody
        can see.
        """
        asked = await self.view.send_apart(bot, self._guided_text_reply(entry.action, notice))
        await self._clear_entry(bot, entry, message)
        self._awaiting_text[self._entry_key] = replace(entry, input_message_id=asked)

    async def _abandon_entry(self, bot) -> None:
        """Take an unanswered question out of the chat when the owner moves on.

        The input box is the one bot message deliberately outside the live view, so it is
        the one thing a redraw cannot replace: navigating away used to leave it sitting
        under the new screen, still accepting input for a step nobody was in — and its only
        record, a single slot, was then overwritten by the next step or cleared by the next
        command, so nothing could ever remove it again. Two abandoned searches and the chat
        is a transcript, which is the state this stage exists to remove.
        """
        entry = self._awaiting_text.pop(self._entry_key, None)
        if entry is not None and entry.input_message_id:
            await self.view.discard(bot, entry.input_message_id)

    async def _clear_entry(self, bot, entry: _TextEntry, message) -> None:
        """Take the owner's answer, and the box it replied to, back out of the chat."""
        await self.view.discard(bot, message.message_id)
        if entry.input_message_id:
            await self.view.discard(bot, entry.input_message_id)

    async def launch_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if self.permits(update) and update.effective_message is not None:
            self._flow = "launch"
            await self.refresh_catalogue()
            await self._answer_command(
                update.effective_message,
                _reply_arguments(self._projects_reply(self.catalogue, view_id="all")),
            )

    async def resume_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Open the resume project picker, so the menu mirrors the bar rather than half of it.

        Listed unconditionally while the bar's Resume button is conditional, which is a
        deliberate asymmetry: a keyboard can omit a button, and Telegram's command menu is
        set once for the chat rather than per screen. A composition with no conversation
        service answers with the same "Resume is unavailable." the rest of that flow gives,
        which is a sentence rather than a dead end.
        """
        del context
        if self.permits(update) and update.effective_message is not None:
            self._flow = "resume"
            if self.backend.conversations is None:
                await self._answer_command(
                    update.effective_message,
                    _reply_arguments(self._message("Resume is unavailable.")),
                )
                return
            await self.refresh_catalogue()
            await self._answer_command(
                update.effective_message, _reply_arguments(self._resume_projects_reply())
            )

    async def sessions_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if self.permits(update) and update.effective_message is not None:
            self._flow = "sessions"
            await self._answer_command(
                update.effective_message, _reply_arguments(await self._sessions_reply())
            )

    async def remote_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Open this machine's own Remote Control -- the one screen about no session at all.

        `self._flow` is cleared rather than set: the navigation bar has three destinations and
        this is not one of them, so marking a tab would tell the owner they are standing
        somewhere they are not.
        """
        del context
        if self.permits(update) and update.effective_message is not None:
            self._flow = None
            await self._answer_command(
                update.effective_message,
                _reply_arguments(await self._host_remote_control_reply()),
            )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Explain the actions this deployment actually offers, and leave a way on.

        Help used to answer with two lines of plain text and no keyboard, which made it the
        one screen in the bot that went nowhere, and it named two of the four destinations
        the bot then had. What is listed here is what this composition was wired with, so a
        bot without resume or project creation does not advertise them.
        """
        del context
        if not self.permits(update) or update.effective_message is None:
            return
        self._flow = None
        lines = [
            "<b>Remote agents</b>",
            "",
            "<b>Launch</b> starts a curated agent in a project.",
        ]
        if self.backend.conversations is not None:
            lines.append("<b>Resume</b> continues a saved conversation in a new session.")
        lines.append(
            "<b>Sessions</b> lists what is running. Open one to read its output, copy an "
            "attach command, rename it, or stop it."
        )
        if self.backend.projects is not None:
            lines.append("<b>Add Project</b> registers a new project to launch into.")
        if self.backend.host_remote_control is not None:
            lines.append(
                f"<b>{escape(HOST_REMOTE_CONTROL_TITLE)}</b> reports whether this machine is "
                "enrolled with the relay, and turns that setting on or off for the whole "
                "machine rather than for one session."
            )
        lines += [
            "",
            f"<b>{ACTION_LABELS[GRACEFUL]}</b> asks the agent to exit on its own terms and "
            "then removes its pane, ending the session in one step — read the output first "
            "if you want it.",
            f"<b>{ACTION_LABELS[FORCE]}</b> kills a session that cannot exit, and asks for "
            "confirmation before it does.",
        ]
        await self._answer_command(
            update.effective_message, _reply_arguments(self._message("\n".join(lines)))
        )

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Acknowledge and refresh only callbacks issued to this exact private chat."""
        del context
        if not self.permits(update) or update.callback_query is None:
            return
        query = update.callback_query
        owner_id = self.owner_user_id
        chat_id = self.owner_chat_id
        # Never 0: that is UNBOUND, and resolving against it would match every token minted
        # for a screen that has not been delivered yet -- an authorization comparison whose
        # fallback collides with a sentinel. -1 matches nothing, which is the honest answer
        # for a press whose message the API did not give us.
        message_id = query.message.message_id if query.message is not None else -1
        state = self.callbacks.resolve(
            query.data or "", owner_id=owner_id, chat_id=chat_id, message_id=message_id
        )
        # A callback query in this chat can only have come from an inline keyboard this bot
        # sent, and `permits` has already established the chat. So the message it was
        # pressed on is a screen of ours — enough to recover an anchor a composition never
        # recorded.
        #
        # It only ever fills an *absent* anchor and never moves a recorded one. Adopting the
        # pressed message unconditionally would walk the live view backwards onto an older
        # screen — which is wrong whatever else is in the chat, so the rule outlives the
        # transitional reason it was first written for.
        #
        # One message of ours is deliberately **not** a screen, and it is the reason this now
        # resolves first rather than adopting blind. A notification is sent apart from the live
        # view; adopting it would make the next render edit the session detail *over* the
        # notification, so the message the runbook promises survives pruning would instead be
        # consumed by it. The exemption travels in the token's action rather than in a set held
        # in this process, because the vulnerable state — a chat with no recorded anchor and a
        # notification already in it — is exactly what a restored database leaves behind, and a
        # process-local set is empty precisely then.
        if state is None or state.action != _NOTIFIED_DETAIL:
            self.view.adopt(message_id)
        notified = state is not None and state.action == _NOTIFIED_DETAIL
        if notified:
            # Normalized once, here, so no downstream branch has to know the distinction
            # exists: it is about where the press came *from*, not about what it does.
            state = replace(state, action="session.detail")
        if state is None:
            # A press this screen cannot account for: the button belongs to a keyboard this
            # message no longer carries. Nothing expired — the token was pruned when the
            # screen that drew it was replaced — so this is a race between a thumb and a
            # redraw, not an error, and it gets a toast rather than the modal alert the
            # expiry used to raise. The words say what happened without claiming a deadline
            # that no longer exists.
            await query.answer("That screen has moved on.")
            self._flow = "sessions"
            await self._render(query, _reply_arguments(await self._sessions_reply()))
            return
        # Set once, before any branch below draws: inspect, a text step, the pending screen
        # and `_reply_for` all render, and a flow set inside only one of them would leave the
        # bar unmarked on the others.
        self._flow = _flow_of(state.action)
        pending = self._pending_notice(state.action)
        await query.answer(pending)
        try:
            # Whatever this press draws, it answers "is that session still on screen". Inside
            # the try, so an unexpected failure lands on the recovery screen below rather
            # than leaving a cleared spinner and nothing drawn.
            showing = None if state.action in _LIST_LANDING_ACTIONS else state.entity_id
            await self._release_attachment(query.get_bot(), showing)
            if state.action not in _TEXT_ENTRY_ACTIONS:
                await self._abandon_entry(query.get_bot())
            if state.action == "session.inspect":
                await self._send_inspection(query, state.entity_id)
                return
            if state.action in _TEXT_ENTRY_ACTIONS:
                await self._begin_guided_text_entry(query, state.action, state.entity_id)
                return
            if pending is not None:
                # Telegram clears the button spinner as soon as the query is answered, which
                # has to happen immediately — so a stop that polls a pane for its whole
                # timeout would otherwise leave the previous screen sitting there, unchanged
                # and unexplained, for up to twenty seconds. Show the wait, and drop the
                # keyboard while it runs so the same button cannot be pressed twice.
                # `retire=False`: the token being processed is bound to this very message,
                # so a retiring render here would prune the action out from under itself.
                await self._render(query, _reply_arguments(render_message(pending)), retire=False)
            await self._render(
                query,
                await self._reply_for(
                    state.action,
                    state.entity_id,
                    token=query.data or "",
                    message_id=message_id,
                ),
            )
            if notified:
                # The notification has been acted on, so it is an answered question of ours --
                # the second category `discard` permits. Leaving it turned the chat into a
                # pile of alerts the owner had already dealt with, each still offering the
                # button they had just pressed, and each pushing the menu further up.
                # Pruned first: the message is going, and a token outliving its message is the
                # dead-button state this store exists to make impossible.
                self.callbacks.prune_for_message(chat_id, message_id)
                await self.view.discard(query.get_bot(), message_id)
                # The notifier holds this message as the session's standing one and would
                # otherwise edit what the line above deleted. Told rather than left to find
                # out, which it can -- an uneditable message is replaced -- but only after
                # paying for the refused call, and only on the next pass.
                #
                # Told *which* message, because the one pressed is not always the one this
                # session currently owns: a notification sent before the standing record was
                # durable, or one whose button could not be minted, outlives any record of
                # itself. See `ActivityNotifier.forget`.
                self.notifier.forget(state.entity_id, message_id)
            if state.action in _SESSION_ENDING_ACTIONS:
                await self.notifier.retire_finished()
        except Exception:
            if pending is None:
                raise
            # The pending screen carries no buttons, so failing after it is drawn would
            # strand the owner on a dead message. Put them back on something they can act on.
            _LOG.exception("callback action failed while its pending notice was on screen")
            await self._render(
                query,
                _reply_arguments(
                    await self._sessions_reply(
                        notice="That action did not complete, and the session was left as it is."
                    )
                ),
            )

    async def _render(self, query, arguments: dict[str, object], *, retire: bool = True) -> None:
        """Draw a screen into this chat's live view, whichever message that currently is.

        Addressed by anchor rather than by the message the press came from. Those are the
        same message in the ordinary case, and deliberately not the same one after a re-send
        — the chat has one screen, so a render's target is a property of the chat rather
        than of whatever update happened to trigger it.

        `LiveView` owns the edit-then-prune-then-bind order and the no-op guard; what is
        left here is telling it which bot to speak through.
        """
        await self.view.render(query.get_bot(), arguments, retire=retire)

    async def _reply_for(
        self, action: str, entity_id: str, *, token: str = "", message_id: int = 0
    ) -> dict[str, object]:
        if action in {"nav.home", "nav.refresh"}:
            self._flow = "sessions"
            # `nav.refresh` no longer has a button. It stays handled because a token outlives
            # the deploy that stopped drawing it: tokens live in SQLite and are valid for the
            # message they were drawn on rather than for a clock, so a Home screen rendered
            # before the upgrade still carries a live Refresh. Answering it with the sessions
            # list is what that button now means; dropping the case would make it a dead button,
            # which is the one state the callback store exists to prevent. Both now answer
            # with the sessions list, which is what Home became.
            return _reply_arguments(await self._sessions_reply())
        if action == "resume.confirm":
            return await self._resume_reply(entity_id, token, message_id)
        if action == "remote.confirm":
            return await self._remote_control_reply(entity_id, token, message_id)
        if action == "project.confirm":
            return await self._project_reply(entity_id, token, message_id)
        if action in {"graceful", "cleanup", "force", CONFIRMED_FORCE}:
            return await self._stop_reply(action, token, message_id)
        if action == "project.open":
            return _reply_arguments(await self._project_areas_reply())
        if action == "launch.open":
            await self.refresh_catalogue()
            return _reply_arguments(self._projects_reply(self.catalogue, view_id="all"))
        if action == "launch.page":
            return _reply_arguments(self._project_page_reply(entity_id))
        if action == "launch.project":
            return _reply_arguments(self._profiles_reply(entity_id))
        if action == "launch.profile":
            return await self._launch_reply(entity_id, token, message_id)
        if action == "sessions.open":
            return _reply_arguments(await self._sessions_reply())
        if action == "sessions.page":
            return _reply_arguments(await self._sessions_reply(_page_number(entity_id)))
        if action == "resume.select":
            # Retired with the review screen, and kept rather than dropped for the reason
            # `nav.refresh` is kept: tokens live in SQLite and are valid for their message
            # rather than for a clock (DEC-011), so a conversation list drawn before this
            # deploy still has rows carrying it. Answering with what changed beats the
            # generic "no longer available", which reads as though the conversation went.
            self._flow = "resume"
            return _reply_arguments(
                self._message(
                    "Resuming no longer has a review step — choosing a conversation starts "
                    "it. Open Resume and choose it again."
                )
            )
        if action == "resume.open":
            await self.refresh_catalogue()
            return _reply_arguments(self._resume_projects_reply())
        if action == "resume.projects":
            return _reply_arguments(self._project_page_reply(entity_id, flow="resume"))
        if action == "resume.project":
            return _reply_arguments(await self._resume_profiles_reply(entity_id))
        if action in {"resume.profile", "resume.page"}:
            return _reply_arguments(await self._resume_catalogue_reply(entity_id))
        if action == "session.detail":
            return _reply_arguments(await self._detail_reply(entity_id, message_id))
        if action == "session.attach":
            return _reply_arguments(await self._attach_reply(entity_id))
        if action == "remote.control":
            return _reply_arguments(await self._remote_control_confirm_reply(entity_id))
        # The host's three, kept under their own prefix rather than beside the pane's. The
        # subjects differ, so a shared prefix would put one screen's Cancel on the other's
        # entity id -- and `_session_scope` would then read a direction as a session.
        if action == "host.remote.open":
            return _reply_arguments(await self._host_remote_control_reply())
        if action == "host.remote":
            return _reply_arguments(await self._host_remote_control_confirm_reply(entity_id))
        if action == "host.remote.confirm":
            return await self._host_remote_control_act_reply(entity_id, token, message_id)
        if action == "host.remote.pair":
            return await self._host_pair_reply(token, message_id)
        if action == "session.trust":
            return await self._trust_reply(entity_id, token, message_id)
        if action == "session.inspect":
            return _reply_arguments(await self._inspect_reply(entity_id))
        return _reply_arguments(self._message("That action is no longer available."))

    async def _launch_reply(self, entity_id: str, token: str, message_id: int) -> dict[str, object]:
        if self.backend.sessions is None:
            return _reply_arguments(self._message("Launching is unavailable."))
        project_id, profile_id = _split_launch(entity_id)
        # Re-derived before the claim, not after. The confirmation screen checked both when it
        # drew the button, and a button now outlives the screen that drew it by any amount of
        # time -- so claiming first would burn the one-shot on a project that has since left
        # the catalogue and leave a FAILED row behind with no way to retry.
        if not any(project.opaque_id == project_id for project in self.catalogue):
            return _reply_arguments(self._message("The project is no longer available."))
        if not any(
            profile.profile_id == profile_id and profile.available for profile in self.profiles
        ):
            return _reply_arguments(self._message("That agent is unavailable."))
        if not self.callbacks.claim_mutation(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            message_id=message_id,
        ):
            return _reply_arguments(self._message("That action has already run."))
        record = await self.backend.sessions.launch(
            LaunchCommand(
                ProjectId(project_id),
                ProfileId(profile_id),
                token,
                # No label at launch: choosing the agent starts the session, and naming it is
                # a later, optional act from the session's own menu (Task 2.3).
                None,
            )
        )
        if record is None:
            return _reply_arguments(self._message("Session launch requested."))
        if record.state is SessionState.FAILED:
            return _reply_arguments(
                self._message(
                    f"<b>Session did not become ready</b>\n{escape(record.display.rendered)}\n"
                    "Workspace trust is never approved remotely. Resolve any trust or startup "
                    "check locally, then open Sessions to recheck.",
                    # Details only. "Sessions" and "Launch another" were the ways on before
                    # a permanent way on existed, and both now name a destination the bar
                    # carries on the next row -- "Launch another" beside a *marked* Launch.
                    (
                        (
                            Button(
                                "Details",
                                self._callback("session.detail", str(record.session_id)),
                            ),
                        ),
                    ),
                )
            )
        return _reply_arguments(
            self._message(
                f"<b>Session created</b>\n{escape(record.display.rendered)}\nState: {record.state}",
                ((Button("Inspect", self._callback("session.detail", str(record.session_id))),),),
            )
        )

    async def _resume_reply(
        self, reference_value: str, token: str, message_id: int
    ) -> dict[str, object]:
        if self.backend.sessions is None or self.backend.conversations is None:
            return _reply_arguments(self._message("Resuming is unavailable."))
        # Everything re-derivable is re-derived **before** the claim, which is the ordering
        # `_launch_reply` records and the ordering this path did not have: claiming first
        # burns the one-shot on a conversation or a project that has since gone, and leaves
        # the owner a button that answers "already run" for something that never ran.
        resolved = await self._resolve_resume(reference_value)
        if resolved is None or resolved.summary.project_id is None:
            return _reply_arguments(self._message("That conversation is no longer available."))
        # The check the review screen used to carry. It moved to the act rather than leaving
        # with the screen -- a rendered row outlives the catalogue it was drawn from.
        if not any(
            project.opaque_id == str(resolved.summary.project_id) for project in self.catalogue
        ):
            return _reply_arguments(self._message("The project is no longer available."))
        # Re-checked at the *act*, not only where the row was drawn. This is the mitigation
        # `StopController.execute` already applies to every stop -- the rendered row is
        # re-tested against the shared policy before the command goes out -- and the resume
        # path did not have it on either surface. It matters more now that the row *is* the
        # act: there is no second screen between the catalogue read that drew it and the
        # resume it performs.
        if not resume_available(resolved.summary):
            return _reply_arguments(self._message("That conversation cannot be resumed safely."))
        # The last re-derivation `_launch_reply` did and this did not. A profile can stop
        # being available between the row being drawn and the row being pressed, and the
        # rendered row outlives the capability probe that drew it.
        if not any(
            profile.profile_id == str(resolved.summary.profile_id) and profile.available
            for profile in self.profiles
        ):
            return _reply_arguments(self._message("That agent is unavailable."))
        if not self.callbacks.claim_mutation(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            message_id=message_id,
        ):
            return _reply_arguments(self._message("That action has already run."))
        outcome = await self.backend.sessions.resume(
            ResumeCommand(
                resolved.summary.project_id,
                resolved.summary.profile_id,
                resolved,
                token,
            )
        )
        record = outcome.record
        if outcome.created and record.state is SessionState.FAILED:
            # A resume this press really did create, whose pane did not come up. Stage 3's
            # deliberate decision (`bb23946`, repaired in `4f2cf88`) that FAILED keeps its own
            # message about resolving trust locally — and the message is about *this press's*
            # resume, so it is owed the `created` guard that the rest of this method now has.
            #
            # It was FAILED-first and unguarded until close-out, which left the same
            # over-claim the `created` branch below exists to remove, just narrower: pressing
            # a conversation bound to an *existing* failed session said "Resume did not become
            # ready" — a sentence about an attempt this press never made — and withheld the
            # attachment explanation its sibling gives. Half-fixing an over-claim is how the
            # remaining half stops looking like one.
            return _reply_arguments(
                self._message(
                    "<b>Resume did not become ready</b>\nOpen Sessions after local attention."
                )
            )
        if not outcome.created:
            # Nothing was started: the conversation was already bound and the service handed
            # back the existing record. This asks what the service **did**, where it used to
            # ask what state the record was in — and no question about the state can answer
            # it, because an already-bound RUNNING session and a resume that has just come up
            # are indistinguishable. That is how "Session resumed" came to be printed over a
            # live session this press had not touched, contradicting both the README and step
            # 12 of the acceptance checklist. The states that used to reach here (ENDED,
            # PRESERVED, STOP_REQUESTED, ORPHANED) still do, and RUNNING and STARTING now do
            # too, which is the whole of the repair.
            return _reply_arguments(
                self._message(
                    f"<b>Not resumed</b>\n{escape(record.display.rendered)}\n"
                    f"This conversation is attached to that session, which is now "
                    f"{state_word(record.state, record.orphan_provenance)}. It becomes "
                    "resumable again once that session has ended — open it to see what it "
                    "offers.",
                    (
                        (
                            Button(
                                "Details",
                                self._callback("session.detail", str(record.session_id)),
                            ),
                        ),
                    ),
                )
            )
        return _reply_arguments(
            self._message(
                f"<b>Session resumed</b>\n{escape(record.display.rendered)}\nState: {record.state}",
                ((Button("Inspect", self._callback("session.detail", str(record.session_id))),),),
            )
        )

    async def _project_areas_reply(self) -> RenderedMessage:
        """Offer only the server-enumerated areas; a typed area never reaches the filesystem."""
        if self.backend.projects is None:
            return self._message("Adding a project is unavailable.")
        areas = tuple(
            area
            for area in await asyncio.to_thread(self.backend.projects.available_areas)
            if selectable_area(area)
        )
        if not areas:
            return self._message("No area is available for a new project.")
        return self._message(
            "<b>Add project</b>\nSelect the area for the new project.",
            _button_rows(
                tuple(Button(area, self._callback("project.area", area)) for area in areas)
            )
            # Cancel returns to the launch list, which is the screen that offered Add
            # Project. It used to mint `nav.home`; Home was a defensible cancel target while
            # it was the root, and the sessions list -- which is what that action now answers
            # -- is the parent of nothing in this flow.
            + ((Button("Cancel", self._callback("launch.open", "projects")),),),
        )

    def _project_review_reply(self, identity: ProjectIdentity) -> RenderedMessage:
        return self._message(
            f"<b>Review new project</b>\nArea: {escape(identity.area)}\n"
            f"Name: {escape(identity.name)}",
            (
                (
                    Button(
                        "Create",
                        self._callback(
                            "project.confirm",
                            f"{identity.area}|{identity.name}",
                            mutation=True,
                        ),
                    ),
                ),
                (Button("Back", self._callback("project.open", "areas")),),
                (Button("Cancel", self._callback("launch.open", "projects")),),
            ),
        )

    async def _project_reply(
        self, entity_id: str, token: str, message_id: int
    ) -> dict[str, object]:
        """Create at most once per confirmation, then re-read the catalogue off the loop."""
        if self.backend.projects is None:
            return _reply_arguments(self._message("Adding a project is unavailable."))
        if not self.callbacks.claim_mutation(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            message_id=message_id,
        ):
            return _reply_arguments(self._message("That action has already run."))
        area, separator, name = entity_id.partition("|")
        if not separator:
            return _reply_arguments(self._message("That action has already run."))
        try:
            created = await asyncio.to_thread(
                self.backend.projects.create, CreateProjectCommand(area, name)
            )
        except ProjectCreationError as error:
            return _reply_arguments(
                self._message(f"<b>Project not created</b>\n{escape(str(error))}")
            )
        except Exception:
            _LOG.exception("project creation failed outside the application's error contract")
            return _reply_arguments(self._message("<b>Project not created</b>\nCheck this host."))
        await self.refresh_catalogue()
        return _reply_arguments(
            self._message(
                f"<b>Project created</b>\n{escape(str(created.identity))}",
            )
        )

    async def _sessions_reply(self, page: int = 1, *, notice: str | None = None) -> RenderedMessage:
        """Render one page of managed sessions: grouped, two lines a row, a picker per row.

        This list is unbounded in a way the project list is not — every launch adds a row
        and only reconciliation takes one away — so it pages for the same reason: a keyboard
        tall enough to push the message off the screen is unusable on a phone, and Telegram
        caps the buttons one keyboard may carry.

        **The rows live in the message text, not on the buttons.** A keyboard button cannot
        hold a newline, and the two-line row is the redesign's whole answer to a list nobody
        could scan: the identity in bold on one line, the state, age and gauge in monospace
        on the next. The buttons become short pickers -- `🟢 #7 remote-agents` -- in the same
        order as the text, two to a row.

        **Grouped by `StateGroup`, in its fixed order, and a bucket with nothing in it is not
        drawn.** The grouping is the shared decision (`session_views.state_group`); the
        headings are this surface's words. The header's emoji legend is the count: `Sessions
        · 6  🟢 2 · 🟡 1 · 🔴 2 · ⚪ 1`, counted from the *whole* listing rather than the page,
        and never from a second read -- two reads can disagree, because a session can end
        between them. A bucket at zero is left out of the legend for the same reason it is
        left out of the body.

        `notice` is the lead line an action that *ended somewhere else* leaves here — the
        outcome of a stop, which lands on this list rather than on a screen of its own.
        It is escaped because it carries wording derived from a `StopFailure`, and it is
        rendered above the heading rather than below it so the owner reads what happened
        before they read the list it happened to. It reaches the empty branch too.

        **Budget.** Section headings, six two-line rows and the limits block come to roughly
        700 UTF-16 units against `MAX_TELEGRAM_TEXT_UNITS`, so the page size stays at eight.
        Every string here passes `escape()` and then `presenters._message` (DEC-014).
        """
        records = await self._listed_records()
        counts = group_counts(records)
        legend = " · ".join(
            f"{group_emoji(group)} {count}" for group, count in counts.items() if count
        )
        heading_counts = f" · {len(records)}" + (f"  {legend}" if legend else "")
        # Read once, above the branch, because both branches render it. The empty branch needs
        # it for the reason `notice` already reaches there: stopping the last session is exactly
        # when this list is empty, and an agent's weekly limit does not stop existing because
        # nothing is running against it right now.
        spent = await self._limit_block()
        # Read beside the limits and for the same reasons: both branches render it, and the
        # empty one needs it most -- a machine's enrollment does not stop being a fact because
        # nothing is running against it right now.
        host = await self._host_remote_block()
        if not records:
            self._sessions_page = 1
            # No body Launch. It was the way out before a permanent way out existed; the
            # bar now carries the identical destination on the very next row, and a button
            # duplicating the one directly beneath it reads as a bug.
            return self._message(
                f"{self._notice_line(notice)}<b>Sessions</b>{heading_counts}\n"
                f"Nothing is running.{spent}{host}"
            )
        page_count = max(1, ceil(len(records) / self.session_page_size))
        index = min(max(page, 1), page_count)
        # After the clamp, not before: `_sessions_page` is read as the page to *return* to, so
        # it has to be a page that exists. A request past the end renders the last one, and
        # remembering the request rather than the render would send Back somewhere emptier.
        self._sessions_page = index
        start = (index - 1) * self.session_page_size
        shown = records[start : start + self.session_page_size]
        sections: list[str] = []
        pickers: list[Button] = []
        for group in StateGroup:
            members = [record for record in shown if session_row_parts(record).group is group]
            if not members:
                continue
            lines = [f"{group_emoji(group)} <b>{_GROUP_TITLES[group]}</b>"]
            for record in members:
                context = await self._context_for(record)
                parts = session_row_parts(record, context)
                _first, second = session_lines(record, context)
                # The sequence sits *outside* the bold, so the eye lands on the name and
                # finds the number beside it, and the state line is monospace so the gauges
                # of neighbouring rows line up.
                lines.append(
                    f"<b>{escape(parts.identity)}</b> #{parts.sequence}\n"
                    f"<code>{escape(second)}</code>"
                )
                pickers.append(
                    Button(
                        f"{state_emoji(record.state)} #{parts.sequence} "
                        f"{record.display.project_slug}",
                        self._callback("session.detail", str(record.session_id)),
                    )
                )
            sections.append("\n".join(lines))
        buttons = list(_button_rows(tuple(pickers), 2))
        navigation = []
        if index > 1:
            navigation.append(Button("Previous", self._callback("sessions.page", str(index - 1))))
        if index < page_count:
            navigation.append(Button("Next", self._callback("sessions.page", str(index + 1))))
        if navigation:
            buttons.append(tuple(navigation))
        title = "Sessions" if page_count == 1 else f"Sessions {index}/{page_count}"
        body = "\n\n".join(sections)
        return self._message(
            f"{self._notice_line(notice)}<b>{title}</b>{heading_counts}\n\n{body}{spent}{host}",
            tuple(buttons),
        )

    async def _context_for(self, record: SessionRecord) -> ContextWindow | None:
        """One RUNNING session's context window for its row gauge, or nothing at all.

        Read only for a RUNNING row -- `session_row_parts` draws no gauge for any other state,
        so a read there would be a provider sweep for a figure nobody renders. The broad
        `except` is `_usage_lines`'s trade: the gauge is a decoration on a list whose real
        content is the way into each session, and a provider that changed its file format must
        not cost the owner the list. Reads are not cached here; a page holds at most eight rows
        and the bot draws this screen on a press, never on a timer.
        """
        if self.backend.usage is None or record.state is not SessionState.RUNNING:
            return None
        try:
            usage = await self.backend.usage(record.session_id)
        except Exception:
            logging.getLogger(__name__).debug("usage read failed", exc_info=True)
            return None
        return None if usage is None else usage.context

    @staticmethod
    def _notice_line(notice: str | None) -> str:
        """The lead line, or nothing at all — never an empty line where a notice would be.

        `None` has to render byte-identically to the screen before this parameter existed,
        or every test pinning the sessions list becomes a test of this function instead.
        """
        return "" if notice is None else f"{escape(notice)}\n"

    def _sessions_back(self) -> str:
        """A Back that lands on the page of the sessions list the owner actually left.

        `sessions.page` rather than `sessions.open`, because the two differ by exactly the
        thing this fixes: `sessions.open` renders `_sessions_reply()` at its default first
        page. Opening a row from page 3 and pressing Back used to answer page 1, and the only
        way back to page 3 was Refresh — which is now gone, so this is the whole route.

        `sessions.open` keeps meaning *the top of the list*, which is what the navigation
        bar's Sessions button, `/sessions` and `/start` should all do. Only the detail, which
        was opened from a known page, is entitled to return to one.
        """
        return self._callback("sessions.page", str(self._sessions_page))

    async def _detail_reply(self, session_value: str, message_id: int = 0) -> RenderedMessage:
        record = await self._record(session_value)
        if record is None:
            # Reached by opening a row that ended under the owner, so the list they came
            # from is exactly where they need to go — not Home, and not its first page.
            return self._message(
                "That session is no longer available.",
                back=self._sessions_back(),
                back_label=_BACK_TO_SESSIONS,
            )
        # **These read-only rows are the surfaces' one deliberate divergence, and the four
        # axes are written down here so nobody has to re-derive them from a diff again.**
        # Against `adapters/tui/screens/sessions.py: detail_entries`:
        #
        #   1. order — here Inspect, Rename, [Copy attach], Remote Control; there [Copy
        #      attach], [Inspect], Rename, Remote Control;
        #   2. Inspect is unconditional here and gated on `backend.capture is not None`
        #      there;
        #   3. Copy attach is gated on `_attach_row_is_offered` here and unconditional
        #      there, which is the row's *presence* and is a separate question from DEC-021's
        #      ownership predicate — that rule is now `pane_is_attachable`, shared, and the
        #      gate applies it rather than restating it (Task 3.4);
        #   4. the label — "Inspect" here, "Inspect output" there.
        #
        # Everything *below* these rows is already shared: the trust row reads
        # `trust_available`, the remote-control rows `remote_control_directions`, the stops
        # `available_actions` + `ACTION_LABELS`, and the parity contract pins the last two on
        # both surfaces. So the detail action set is not a duplicate awaiting a merge — it is
        # three shared groups and one divergent head, and unifying any row of the table above
        # is a functionality change. Owner's decision, 2026-08-22, recorded in the
        # shared-use-cases sub-plan under Task 2.3; a later stage that wants one assembler has
        # to parameterize exactly these four axes.
        #
        # **Shape, since the redesign: read-only actions pair up, stops keep their own row.**
        # Inspect and Rename share the first row; Copy attach and the Remote Control direction
        # share the second, each present only where its gate says so. The marks in front of
        # the labels are this surface's (`_ACTION_EMOJI` and friends); the words stay shared.
        reads: list[Button] = [
            Button(f"{_INSPECT_EMOJI} Inspect", self._callback("session.inspect", session_value)),
            # Renaming changes what the session is called and nothing about what it is doing,
            # so it sits with the reads.
            Button(f"{_RENAME_EMOJI} Rename", self._callback("session.rename", session_value)),
        ]
        buttons: list[tuple[Button, ...]] = [tuple(reads)]
        attachable = await self._attach_row_is_offered(record)
        second: list[Button] = []
        if attachable:
            second.append(
                Button(
                    f"{_ATTACH_EMOJI} Copy attach",
                    self._callback("session.attach", session_value),
                )
            )
        # One button per direction the policy still offers -- which is one once this
        # session's state has been observed, and both only while it is unknown. Half of the
        # old pair was always a no-op, on the deepest screen the bot has.
        for direction in remote_control_directions(record, record.remote_control_state):
            second.append(
                Button(
                    f"{_REMOTE_EMOJI} {REMOTE_CONTROL_LABELS[direction]}",
                    self._callback("remote.control", f"{session_value}|{direction.value}"),
                )
            )
        if second:
            buttons.append(tuple(second))
        if await self._awaiting_trust(record):
            buttons.append(
                (
                    Button(
                        "Trust this project",
                        # A mutation token: this button sends a keypress into a live pane, so
                        # it is claimed once and never replayed, exactly like the confirmed
                        # stop and resume buttons.
                        self._callback("session.trust", session_value, mutation=True),
                    ),
                )
            )
        # The stops share one row of their own. Telegram has no separator, so shape and the
        # mark are the only signals available, and the actions that end a session should not
        # look like the ones that read it — a graceful stop is one tap from discarding the
        # pane's output. No state offers more than two stops, so the row stays legible; force
        # is last because `available_actions` puts it last.
        stops: list[Button] = []
        for action in available_actions(record.state, record.orphan_provenance):
            token = self.stops.offer(
                record.session_id,
                record.profile_id,
                record.state,
                record.orphan_provenance,
                action,
                self.owner_user_id,
                self.owner_chat_id,
            )
            if token is not None:
                stops.append(Button(action_button_label(action), token))
        if stops:
            buttons.append(tuple(stops))
        # The status line replaces `State: running`: the same mark the list uses, the shared
        # state word, the short age. Then the explanation, unchanged. Then the fact block --
        # `<code>` lines with the label padded to eight so the values align, and a line is
        # drawn only where it has something to say. Escaped like every other line here even
        # where the value is digits and fixed words — the escape is a property of this
        # boundary (DEC-014), not a judgement about each string's provenance.
        parts = session_row_parts(record)
        facts: list[tuple[str, str]] = []
        for value in await self._usage_lines(record):
            facts.append(("context", value))
        if remote_control_available(record):
            facts.append(
                (
                    "remote",
                    "Remote Control "
                    + _REMOTE_CONTROL_WORDS.get(record.remote_control_state, "unknown"),
                )
            )
        if attachable:
            facts.append(("pane", "attachable"))
        fact_lines = "".join(
            f"\n<code>{label.ljust(_FACT_LABEL_WIDTH)} {escape(value)}</code>"
            for label, value in facts
        )
        return self._message(
            f"<b>{escape(parts.identity)}</b> #{parts.sequence}\n"
            f"<code>{state_emoji(record.state)} {escape(parts.state)} · {parts.age}</code>\n"
            f"{_state_explanation(record.state, record.orphan_provenance)}"
            f"{fact_lines}",
            tuple(buttons),
            back=self._sessions_back(),
            back_label=_BACK_TO_SESSIONS,
        )

    async def _limit_block(self) -> str:
        """Each installed agent's rate-limit windows, as a monospace block under the rows.

        Last on the screen and separated by a blank line, because it is a statement about the
        account rather than about any row — the placement that stops a window reading as the
        spend of whichever session it happens to sit beside, which is the report this block
        exists to answer. Agent names are padded to the longest profile id plus two inside
        `<code>`, so the percentages line up; a stale reading says so (`· as of 2h ago`) and a
        borrowed one names its source (`· via …`, DEC-061). Reset countdowns are the local
        surface's; the phone gets the share.

        Escaped here rather than in `limit_rows`, which returns parts and takes no view on
        either surface's markup (DEC-043, DEC-014). The broad `except` is `_usage_lines`'s
        trade made again one screen up, and it matters more here: this block sits on the screen
        that is the only way to reach a session at all, so a provider that changed its file
        format under an upgrade must not be able to cost the owner the list.
        """
        if self.backend.limits is None:
            return ""
        try:
            rows = limit_rows(await self.backend.limits())
        except Exception:
            logging.getLogger(__name__).debug("account limits read failed", exc_info=True)
            return ""
        if not rows:
            # The heading is part of the block, so it goes when the block does. Emitting it
            # unconditionally promised a block and delivered none -- reached whenever every
            # agent answers with no windows, which is Claude's cache past its thirty-minute
            # fence and a quiet codex, the same routine state the TUI pane's empty sentence
            # exists for.
            return ""
        width = max(len(row.profile) for row in rows) + 2
        lines = []
        for row in rows:
            pieces = [f"{window.label} {window.percent}%" for window in row.windows]
            if row.borrowed is not None:
                pieces.append(f"via {row.borrowed}")
            if row.stale_for is not None:
                pieces.append(f"as of {row.stale_for} ago")
            lines.append(
                f"<code>{escape(row.profile.ljust(width))}{escape(' · '.join(pieces))}</code>"
            )
        return "\n\n<b>Plan limits</b>\n" + "\n".join(lines)

    async def _usage_lines(self, record: SessionRecord) -> tuple[str, ...]:
        """Ask the provider what this session has spent, and never let the answer cost a screen.

        A host that wired no reader renders no usage line at all — the same absence-is-an-answer
        arrangement `capture` and `activity_feed` have, rather than a row saying the host is
        missing something the owner did not ask for.

        The broad `except` is the same trade `ProfileUsageReaders` already makes one layer down,
        made again at the seam that matters: this line is a decoration on a screen whose real
        content is a session's state and its stop actions, and a provider that changed its file
        format under an upgrade must not be able to take those away.
        """
        if self.backend.usage is None:
            return ()
        try:
            return usage_lines(await self.backend.usage(record.session_id))
        except Exception:
            logging.getLogger(__name__).debug("usage read failed", exc_info=True)
            return ()

    async def _attach_reply(self, session_value: str) -> RenderedMessage:
        record = await self._record(session_value)
        back = self._callback("session.detail", session_value)
        # One question, asked once. This used to check `_can_copy_attach` and *then* call
        # `copy_attach`, so it inspected the same pane twice to reach one answer — and the
        # first of those two asked a rule the adapter had restated for itself. `copy_attach`
        # applies `pane_is_attachable` and returns nothing when it does not hold, which is the
        # same refusal by the same rule, so the extra round trip bought exactly nothing.
        command = await self._attach_command(record)
        if command is None:
            return self._message(
                "Copy Attach is unavailable: this session has no pane on this host any more.",
                back=back,
            )
        return self._message(
            f"<b>Copy attach command</b>\n<code>{escape(command)}</code>", back=back
        )

    async def _attach_command(self, record: SessionRecord | None) -> str | None:
        """The copyable command for this pane, or `None` when the owner may not be given one.

        `sessions is None` is a host with no session use case, which cannot answer at all; it
        is the arm the removed predicate opened with, kept because the answer is still "no
        command" rather than an attribute error mid-render.

        `SessionNotFoundError` is caught for the same reason and is **not** belt-and-braces:
        the removed `_can_copy_attach` reached only `inspect`, which answers `None` for a
        session that has gone, so a row drawn just before the record ended used to earn the
        refusal sentence below. `copy_attach` opens with `_require_session` instead, and
        `session.attach` has no `_PENDING_NOTICES` entry, so an uncaught raise here reaches
        `callback`'s `if pending is None: raise` and costs the owner the screen rather than a
        sentence. The window is narrow — between `_record`'s list read and this call — but it
        is the one behavioural difference this task would otherwise have made, and the task
        forbids one.
        """
        if record is None or self.backend.sessions is None:
            return None
        try:
            return await self.backend.sessions.copy_attach(record.session_id)
        except SessionNotFoundError:
            return None

    async def _attach_row_is_offered(self, record: SessionRecord) -> bool:
        """Whether the detail draws a Copy attach row at all — this surface's own choice.

        **Named for the question it answers rather than for the rule it applies**, because the
        two are genuinely different and Task 2.3 wrote the difference down: the local surface
        renders this row unconditionally and explains when it is chosen, so *row presence* is
        the bot's alone (axis 3 of the four the detail's read-only head diverges on). What is
        no longer the bot's is the rule inside it — `pane_is_attachable` is the one DEC-021
        requires identical on both surfaces, and it is now asked rather than restated.

        Still one `inspect` and no store read, which is what the removed `_can_copy_attach`
        cost. Asking `copy_attach` here instead would have been the shorter diff and the wrong
        one: it re-reads the record and builds a command string a row-presence test then
        throws away, turning every detail render into two pane inspections. It would also move
        a question from action time to render time, which `tests/support/backends.py` records
        as a real distinction rather than an incidental one.
        """
        if self.backend.sessions is None:
            return False
        observation = await self.backend.sessions.inspect(InspectQuery(record.session_id))
        return pane_is_attachable(observation, record)

    async def _remote_control_confirm_reply(self, entity_id: str) -> RenderedMessage:
        session_value, separator, state_value = entity_id.partition("|")
        if not separator or state_value not in {"active", "inactive"}:
            return self._message("That Remote Control request is incomplete.")
        record = await self._record(session_value)
        if record is None or record.profile_id != ProfileId("claude"):
            return self._message("Remote Control is unavailable for this session.")
        action = "Enable" if state_value == "active" else "Disable"
        return self._message(
            f"<b>{action} Remote Control?</b>\nThis uses only the verified Claude interaction.",
            (
                (
                    Button(
                        action,
                        self._callback("remote.confirm", entity_id, mutation=True),
                    ),
                ),
                (Button("Cancel", self._callback("session.detail", session_value)),),
            ),
        )

    async def _remote_control_reply(
        self, entity_id: str, token: str, message_id: int
    ) -> dict[str, object]:
        if self.backend.sessions is None:
            return _reply_arguments(self._message("Remote Control is unavailable."))
        session_value, separator, state_value = entity_id.partition("|")
        if not separator:
            return _reply_arguments(self._message("That Remote Control request is incomplete."))
        # Re-read before the claim, for the reason `_launch_reply` gives: the session this
        # button names may have ended since the screen was drawn, and spending the one-shot
        # on it would answer the retry with "already run".
        if await self._record(session_value) is None:
            return _reply_arguments(self._message("That session is no longer available."))
        if not self.callbacks.claim_mutation(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            message_id=message_id,
        ):
            return _reply_arguments(self._message("That action has already run."))
        state = RemoteControlState(state_value)
        result = await self.backend.sessions.set_remote_control(
            RemoteControlCommand(SessionId.parse(session_value), state, token)
        )
        # Just the resulting state. Distinguishing "was already active" from "is now active"
        # is not knowable here: `remote_control` returns the desired state both when it had
        # to send keys and when it found the pane already there, so a message claiming the
        # difference would be asserting something this layer cannot see. The port would have
        # to say whether it acted, which is a wider change than this screen's wording.
        return _reply_arguments(self._message(f"Remote Control: {result.value}."))

    @staticmethod
    def _host_reading(status: HostRemoteControlStatus) -> str:
        """The reading itself: a word for the connection, and the daemon's name for the host.

        `server_name` is provider-supplied text this project did not decode, so it passes the
        presentation boundary's encoder before it reaches a message (DEC-014) -- the same rule
        every project name and captured line on this surface obeys. Escaped whole rather than
        in the one part that needs it, because the escape is a property of the boundary and not
        a judgement made string by string.
        """
        reading = _HOST_CONNECTION_WORDS[status.connection]
        if status.server_name:
            reading = f"{reading} ({status.server_name})"
        return escape(reading)

    def _host_unavailable(self) -> RenderedMessage:
        """What a composition that declared no host capability answers, at every door."""
        return self._message(f"{escape(HOST_REMOTE_CONTROL_TITLE)} is unavailable.")

    def _host_remote_screen(self, status: HostRemoteControlStatus) -> RenderedMessage:
        """The reading, what it means, and the direction (or directions) still worth offering.

        One button per direction the policy returns, which is one for a host whose connection
        the daemon stated and both where it could not be read -- `ERRORED` and `UNREACHABLE`
        both leave the true setting unknown, and pressing a direction is how the owner finds
        out whether it still will not take.
        """
        directions = host_remote_control_directions(status)
        buttons = tuple(
            Button(
                f"{_REMOTE_EMOJI} {HOST_REMOTE_CONTROL_LABELS[direction]}",
                self._callback("host.remote", direction.value),
            )
            for direction in directions
        )
        # Pairing sits behind its own predicate rather than beside the directions: a code
        # minted with no live link expires unused, which reads as a broken feature rather
        # than as an action that was never offered. `mutation=True` because the token it
        # mints is the idempotency key the request will carry.
        if pair_available(status):
            buttons += (
                Button(
                    f"{_REMOTE_EMOJI} Pair a phone",
                    self._callback("host.remote.pair", "host", mutation=True),
                ),
            )
        return self._message(
            f"<b>{escape(HOST_REMOTE_CONTROL_TITLE)}</b>\n"
            f"<code>{self._host_reading(status)}</code>\n"
            f"{escape(_HOST_CONNECTION_EXPLANATIONS[status.connection])}",
            (buttons,) if buttons else (),
        )

    async def _host_remote_control_reply(self) -> RenderedMessage:
        """Read this machine and draw it. No claim and no confirmation -- it is a read."""
        control = self.backend.host_remote_control
        if control is None:
            return self._host_unavailable()
        return self._host_remote_screen(await control.status())

    async def _host_remote_control_confirm_reply(self, entity_id: str) -> RenderedMessage:
        """Ask before the machine changes, the way the pane toggle asks before a pane does.

        The entity is a direction and nothing else: this screen names no session, which is the
        whole structural difference from `_remote_control_confirm_reply` and the reason the two
        are siblings rather than one function with an optional session in it.
        """
        control = self.backend.host_remote_control
        if control is None:
            return self._host_unavailable()
        direction = _host_direction(entity_id)
        if direction is None:
            return self._message("That Remote Control request is incomplete.")
        label = HOST_REMOTE_CONTROL_LABELS[direction]
        return self._message(
            f"<b>{escape(label)}?</b>\n{escape(_HOST_DIRECTION_CAUTIONS[direction])}",
            (
                (
                    Button(
                        label,
                        self._callback("host.remote.confirm", entity_id, mutation=True),
                    ),
                ),
                (Button("Cancel", self._callback("host.remote.open", "host")),),
            ),
        )

    async def _host_remote_control_act_reply(
        self, entity_id: str, token: str, message_id: int
    ) -> dict[str, object]:
        """Flip this machine once, for this exact press, and draw what it now reads.

        **The token is the idempotency key.** It is minted fresh by the confirmation screen
        that drew this button, so each press carries its own -- which matters more here than
        for the pane: a toggle that ends in a broken reading has still burned its key, so a
        reused one would make the retry unavailable rather than merely redundant.

        `claim_mutation` is what stops a redelivered callback from acting twice, and it is
        claimed *after* the capability check for the reason `_launch_reply` gives: spending the
        one-shot on a press that could never have worked answers the retry with "already run".
        """
        control = self.backend.host_remote_control
        if control is None:
            return _reply_arguments(self._host_unavailable())
        direction = _host_direction(entity_id)
        if direction is None:
            return _reply_arguments(self._message("That Remote Control request is incomplete."))
        if not self.callbacks.claim_mutation(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            message_id=message_id,
        ):
            return _reply_arguments(self._message("That action has already run."))
        status = await control.set_state(HostRemoteControlCommand(direction, token))
        # The reading the host now reports, not the direction that was asked for. A screen
        # claiming the press succeeded would be asserting something this layer cannot see --
        # the service answers a failed enable with a reading rather than an exception, and
        # that reading is exactly what the owner needs in order to decide what to do next.
        return _reply_arguments(self._host_remote_screen(status))

    async def _host_pair_reply(self, token: str, message_id: int) -> dict[str, object]:
        """Mint one pairing code and send it once, with no keyboard under it.

        **No keyboard, deliberately.** Every other screen in this bot ends in buttons, and
        one here would be a control that re-renders a message whose whole content is a
        secret -- a second copy in the chat, or worse, one produced long after the owner
        stopped looking. The message is terminal: it is read, and then it is history the
        owner can delete.

        **Nothing about the code is stored, logged, or echoed** (DEC-013). It is escaped like
        any provider text on its way into HTML and then it is gone from this process. A mint
        that fails says so without repeating what the provider printed, because a failure
        after the relay produced a code would otherwise put that code in an error message.

        The token is the idempotency key, as it is for the toggle, and it is claimed after
        the capability check so a press that could never have worked does not spend it.
        """
        control = self.backend.host_remote_control
        if control is None:
            return _reply_arguments(self._host_unavailable())
        # Re-read before minting, exactly as the terminal does. The button was drawn against
        # a reading that may be a screen old, and a link that has dropped since would mint a
        # live code that pairs nothing -- which reads to an owner as a broken feature rather
        # than as an action that was never available. Checked before the claim, so a press
        # that could never have worked does not spend its one-shot.
        status = await control.status()
        if not pair_available(status):
            return _reply_arguments(self._host_remote_screen(status))
        if not self.callbacks.claim_mutation(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            message_id=message_id,
        ):
            return _reply_arguments(self._message("That action has already run."))
        try:
            code = await control.pair(PairCommand(token))
        except Exception as error:
            # The type only -- see the sibling in `adapters/tui/app.py` for why `exception`
            # would put a still-live pairing code into this process's logs.
            _LOG.error("a pairing code could not be minted: %s", type(error).__name__)
            return _reply_arguments(
                self._message(
                    f"No {escape(HOST_REMOTE_CONTROL_TITLE)} pairing code was produced. "
                    "Nothing changed."
                )
            )
        expires = code.expires_at.astimezone().strftime("%H:%M:%S %Z")
        # `render_message` rather than `self._message`, which is the fourth screen in this
        # class to skip the navigation bar and the first to do so for a reason of its own.
        # The bar is a keyboard, and a keyboard under a message whose entire content is a
        # secret is a control that can re-send it -- into the same chat, possibly long after
        # the owner stopped looking. The two permanent exceptions above skip the bar so a
        # wait cannot be pressed into a second launch; this one skips it so a secret cannot
        # be pressed into a second copy.
        return _reply_arguments(
            render_message(
                f"<b>{escape(HOST_REMOTE_CONTROL_TITLE)} pairing code</b>\n"
                f"<code>{escape(code.code)}</code>\n\n"
                f"Shown once. It expires at {escape(expires)}.\n"
                "Type it into the ChatGPT app's manual pairing screen. "
                "Anyone who has it can drive this machine until it expires."
            )
        )

    async def _host_remote_block(self) -> str:
        """This machine's reading for the sessions list, or nothing at all.

        Placed under `Plan limits` and for the same reason that block sits where it does: it is
        a statement about the host rather than about any row, so it must not read as the state
        of whichever session it happens to sit beside.

        The broad `except` is `_limit_block`'s trade, made for the same screen: `status()` does
        not raise by contract -- a boundary that will not answer comes back as a reading -- but
        this line is a decoration on the one screen that is the only way to reach a session at
        all, and no provider change may cost the owner that list.
        """
        control = self.backend.host_remote_control
        if control is None:
            return ""
        try:
            status = await control.status()
        except Exception:
            _LOG.debug("host remote control read failed", exc_info=True)
            return ""
        return (
            f"\n\n<code>{escape(HOST_REMOTE_CONTROL_TITLE)} · {self._host_reading(status)}</code>"
        )

    async def _awaiting_trust(self, record: SessionRecord) -> bool:
        """Whether to offer the trust row. Costs one pane capture per detail render.

        There is deliberately no session-state gate, and the first version of this had one
        (FAILED and STARTING only) on the reasoning that a trust-blocked launch never
        becomes ready. That reasoning was wrong, and it hid the button on the very first
        real session to hit the bug. `claude-remote` prints a banner containing its
        readiness marker *before* the trust dialog renders, so the launch loop can observe
        "Claude Code" and no blocker in the same pass and report the session RUNNING while
        it is in fact stuck on a question. Whether a trust-blocked launch lands in FAILED or
        RUNNING is a race, so state says nothing about it and the pane is the only authority.
        """
        if self.backend.sessions is None or not trust_available(record, TrustState.AWAITING):
            # Asked with AWAITING as a hypothetical: if the answer is False even then, the
            # record alone rules the row out (wrong profile) and the pane never has to be
            # read. Only a session that *could* be answered costs a capture.
            return False
        state = await self.backend.sessions.trust_state(record.session_id)
        return trust_available(record, state)

    async def _trust_reply(self, entity_id: str, token: str, message_id: int) -> dict[str, object]:
        if self.backend.sessions is None:
            return _reply_arguments(self._message("Answering the trust question is unavailable."))
        # Re-read before the claim, for the reason `_launch_reply` and `_remote_control_reply`
        # both give: this button outlives the screen that drew it. The profile is re-checked
        # here too, and that is not belt-and-braces -- the service raises on a profile it
        # cannot answer, and claiming first meant a refused press still burned the one-shot,
        # so the retry answered "already run" for a button that had never worked once.
        record = await self._record(entity_id)
        if record is None:
            return _reply_arguments(self._message("That session is no longer available."))
        if record.profile_id not in TRUST_ANSWERABLE:
            return _reply_arguments(
                self._message("That session's agent does not ask this question.")
            )
        if not self.callbacks.claim_mutation(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            message_id=message_id,
        ):
            return _reply_arguments(self._message("That action has already run."))
        result = await self.backend.sessions.answer_trust(
            AnswerTrustCommand(SessionId.parse(entity_id), token)
        )
        # UNKNOWN is the expected answer, not a failure: answering clears the dialog, so the
        # capture taken afterwards no longer matches. Reporting it as an outcome would tell
        # the owner the thing worked only when it did not.
        if result is TrustState.AWAITING:
            return _reply_arguments(
                self._message("The project is still waiting to be trusted. Try again.")
            )
        return _reply_arguments(
            self._message("Trusted. The agent can continue; relaunch if it already gave up.")
        )

    async def _inspect_reply(self, session_value: str) -> RenderedMessage:
        """Render captured output over a way back to the session that produced it.

        Inspecting used to replace the detail view with the capture and a lone Home button,
        so reading a pane cost the owner every action they had just been offered for it.
        That is the wrong trade in general and the wrong one in particular: a graceful stop
        discards the pane's output, so reading before stopping is the only order that works,
        and it was the most expensive path in the bot.
        """
        back = self._callback("session.detail", session_value)
        result = await self._inspection_result(session_value)
        if result is None:
            return self._message("Inspection is unavailable.", back=back)
        return self._message(f"<pre>{escape(result.text)}</pre>", back=back)

    async def _send_inspection(self, query, session_value: str) -> None:
        result = await self._inspection_result(session_value)
        back = self._callback("session.detail", session_value)
        if result is None:
            await self._render(
                query, _reply_arguments(self._message("Inspection is unavailable.", back=back))
            )
            return
        await self._render(
            query,
            _reply_arguments(self._message(f"<pre>{escape(result.text)}</pre>", back=back)),
        )
        if result.attachment is not None and result.filename is not None:
            # Captured panes carry whatever the agent printed — credentials, paths, whole
            # conversations — so the document is marked unforwardable rather than left
            # saveable from every other client the owner is signed into. That is also why
            # it is remembered: it is the one thing here that cannot be redrawn, so it has
            # to be taken back out deliberately when its session leaves the screen.
            # Unconditionally, and before the new one is sent: a second inspect of the
            # *same* session passes the release check above untouched, so without this the
            # first document is orphaned in the chat with nothing left tracking it.
            await self._release_attachment(query.get_bot(), None)
            self._attachment = (
                session_value,
                await self.view.send_document_apart(
                    query.get_bot(),
                    document=io.BytesIO(result.attachment),
                    filename=result.filename,
                    protect_content=True,
                ),
            )

    async def _release_attachment(self, bot, showing: str | None) -> None:
        """Take a captured document out of the chat once its session is off the screen.

        `showing` is the entity the screen being drawn is about. It is compared by its
        *session* rather than whole, because several actions carry a composite id —
        `session:profile` for a stop, `session|state` for remote control — and an exact
        comparison reads a confirmation dialog *about* a session as a screen about
        something else. That took the document away while the owner was still looking at
        the session, and gave it back to nobody when they cancelled.

        `None` means the screen is about no session at all, which is what a command and a
        finished text step always are.
        """
        if self._attachment is None:
            return
        if showing is not None and self._attachment[0] == _session_scope(showing):
            return
        try:
            removed = await self.view.discard(bot, self._attachment[1])
        except TelegramError:
            # Housekeeping must never cost the owner the screen they pressed for. The
            # document stays and the next navigation tries again — the alternative is a
            # cleared spinner and nothing drawn, which is the dead button this plan exists
            # to remove.
            _LOG.warning("could not remove the captured document; leaving it in the chat")
            return
        if removed:
            self._attachment = None

    async def _inspection_result(self, session_value: str):
        if self.backend.capture is None:
            return None
        try:
            captured = await self.backend.capture(SessionId.parse(session_value))
        except TerminalTargetMissing:
            # The pane died between this view being drawn and the button being pressed —
            # an OOM kill, or a terminal crash. Reconciliation ends the record on its next
            # pass, so this only has to answer the press rather than raise into the handler.
            return None
        return inspect_capture(captured.encode())

    async def _begin_guided_text_entry(self, query, action: str, entity_id: str) -> None:
        """Ask for a value: the instruction in the live view, the input box beside it.

        The split is not a style choice. `ForceReply` cannot be attached to a message being
        *edited* while it carries an inline keyboard — Telegram answers `Inline keyboard
        expected` — so the input box has to be its own message, and its id is remembered so it
        can be taken back out once it has been answered.
        """
        # A str, unlike the `_TextEntry` that `entry` names everywhere else in this class.
        entry_action = "project.name" if action == "project.area" else action
        bot = query.get_bot()
        # A step opened while another is still open would otherwise overwrite the only
        # record of the first one's box.
        await self._abandon_entry(bot)
        await self._render(query, _reply_arguments(self._message(_entry_instruction(entry_action))))
        asked = await self.view.send_apart(bot, self._guided_text_reply(entry_action))
        self._awaiting_text[self._entry_key] = _TextEntry(entry_action, entity_id, asked)

    def _pending_notice(self, action: str) -> str | None:
        """What to show while `action` runs, or None when it answers fast enough not to.

        A bare `force` is absent from the table on purpose: the first press only opens the
        confirmation, and nothing is killed until the second press arrives under a different
        action. That used to be a lookup of remembered confirmation state; it is now readable
        from the action alone.
        """
        return _PENDING_NOTICES.get(action)

    async def _stop_reply(self, action: str, token: str, message_id: int) -> dict[str, object]:
        if action == FORCE:
            return _reply_arguments(await self._force_confirm_reply(token, message_id))
        request = self.stops.claim(token, self.owner_user_id, self.owner_chat_id, message_id)
        if request is None or self.backend.sessions is None:
            # DEC-008 drops the repeat rather than servicing it; where the *answer* is drawn
            # is a separate question, and it lands on the list like every other stop outcome
            # rather than on the one Home-only screen a stop button could still reach.
            return _reply_arguments(
                await self._sessions_reply(notice="That action has already run.")
            )
        # `profile_id` is passed rather than omitted, and that is DEC-006 rather than a
        # detail: `execute_stop` takes it optionally, because the local surface acts on the
        # record under its cursor and has nothing separate to compare. This surface *does* —
        # the token carries the profile the action was offered against — so omitting it here
        # would skip the fail-closed check silently instead of failing. Pinned by
        # `test_a_press_whose_record_changed_profile_never_reaches_the_service`, which drives
        # this press rather than the shared function, because nothing that calls the shared
        # function directly can see whether this call site supplies the argument.
        outcome = await execute_stop(
            request.action,
            request.session_id,
            sessions=self.backend.sessions,
            read_record=lambda: self._record(str(request.session_id)),
            profile_id=request.profile_id,
        )
        if not outcome.dispatched:
            # Lands on the list like every other outcome. Covers all three halves of the
            # guard: the session moved on between the offer and the press, its record is gone
            # entirely, or the profile behind the press is no longer the one in the store.
            # The bot collapses them into one notice deliberately — the local surface words
            # them apart, which is why `execute_stop` reports *which* refusal it was rather
            # than a bare false. The second sentence this used to carry — "Open the list again
            # to see where it is now." — was an instruction to navigate somewhere the owner
            # now already is, so the refusal keeps only the half that says what happened.
            return _reply_arguments(
                await self._sessions_reply(
                    notice="That session moved on before this could run, so nothing was done."
                )
            )
        # `request.action` rather than the pressed one: a confirmed force arrives under an
        # adapter-internal action name, and the outcome is reported in the domain's terms.
        # The record is the one `execute_stop` re-read, not a second read of the store.
        return _reply_arguments(
            await self._stop_outcome_landing(request.action, outcome.record, outcome.failure)
        )

    async def _force_confirm_reply(self, token: str, message_id: int) -> RenderedMessage:
        """Name the session and the cost before offering the only irreversible button.

        The rule is that the destructive button must not be where the thumb already rests,
        and the **order that satisfies it changed** when the navigation bar arrived. Cancel
        used to come first, above `Force stop`, because the row below was a lone `Home`
        nobody pressed — so last-but-one was the safe distance. The bottom row is now the
        bar, the one row in the bot the owner builds muscle memory for, which made
        last-but-one the *worst* position on the screen rather than a safe one.

        So `Force stop` is offered first and Cancel sits between it and the bar: the button
        adjacent to the habitual tap target is the harmless one, and the irreversible one is
        two rows away from it. That also puts this screen in line with every other
        confirmation here — resume, Remote Control, create-project all offer the action
        first and the way out beneath it; force stop was the lone inversion, for a reason
        that has now expired.

        The confirming button is a **new** token carrying a different action, not the one the
        owner just pressed. Re-offering the pressed token cannot work when the screen is
        redrawn in place: the render that draws this screen prunes what the previous keyboard
        left on the message, and the re-offered token is part of exactly that set.
        """
        # A re-read, not a re-resolve: `callback` resolved this token for this message before
        # it dispatched here, and `_release_attachment` and `_abandon_entry` have awaited since.
        # A notification delivered in that gap rebinds the token, and re-asking the message
        # question would refuse the confirmation screen for a force stop that is still legal.
        # The same defect as `StopController.claim`, one window narrower.
        state = self.callbacks.reread(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
        )
        if state is None:
            return self._message("That action is no longer available.")
        session_value, _, profile_value = state.entity_id.partition(":")
        record = await self._record(session_value)
        if record is None:
            return self._message(
                "That session is no longer available.",
                back=self._callback("sessions.open", "sessions"),
            )
        confirmed = self.stops.offer_confirmed_force(
            record.session_id,
            ProfileId(profile_value),
            record.state,
            record.orphan_provenance,
            self.owner_user_id,
            self.owner_chat_id,
        )
        if confirmed is None:
            return self._message(
                "Force stop is no longer available for this session.",
                back=self._callback("session.detail", session_value),
            )
        # The state line is carried onto the confirmation, not only onto the detail screen.
        # Its twin `ForceStopConfirmModal` on the local surface has always rendered it, and
        # after DEC-020 it is what tells the owner they are about to kill a pane this app
        # adopted rather than launched — which is exactly the sentence the one screen that
        # authorises a kill should not be the one screen to omit. The parity contract compares
        # rendered *action sets*, so this divergence was invisible to it.
        return self._message(
            f"<b>Force stop {escape(record.display.rendered)}?</b>\n"
            "This kills the agent immediately and cannot be undone. Anything it has not "
            "saved is lost.\n"
            f"{escape(_state_explanation(record.state, record.orphan_provenance))}",
            (
                (Button(ACTION_LABELS[FORCE], confirmed),),
                (Button("Cancel", self._callback("session.detail", session_value)),),
            ),
        )

    async def _stop_outcome_landing(
        self, action: str, record: SessionRecord, failure: StopFailure | None = None
    ) -> RenderedMessage:
        """Report what the session actually did, named, as the lead line of the session list.

        The outcome used to be a screen of its own whose only exits were `Back` and `Home`,
        which left the owner one dead end away from the list they almost always wanted next.
        It is now the notice on that list: the same words, over the rows they describe. The
        wording is plain text rather than markup because `_sessions_reply` escapes the notice
        once — see `_notice_line` — and that is also what keeps `failure`'s words intact.

        A graceful stop that times out leaves the session RUNNING and removes nothing, so
        "completed" would have been false for the one outcome the owner most needs to act
        on. The record is re-read instead of assumed: `_records()` omits ended sessions, so
        a session that has left the list is one that ended.

        **`failure` is why that re-read is not enough on its own, and it is BL-008.** Finding
        the session still listed says a stop did not take effect; it cannot say which of two
        unrelated things went wrong, because they leave identical evidence. This branch used
        to assert "It did not exit in time" for both — true for `graceful_timeout`, and flatly
        false for `unknown_session`, where no exit sequence was ever sent and the fault is a
        profile this host cannot resolve (DEC-006). Being told the wrong cause is worse than
        being told none: the owner waits for an agent that was never asked to stop.

        The words come from `application.session_actions`, which is where the local surface
        takes them from too — DEC-007 wants the two surfaces to agree about what a stop did,
        and being handed the same sentence is the cheapest form of agreeing. They are escaped
        despite being ours — now by the notice rather than here — because `stop_failure`'s
        fallback interpolates the raw `detail` the terminal adapter reported, which this
        module does not author.
        """
        subject = record.display.rendered
        session_value = str(record.session_id)
        current = await self._record(session_value)
        if current is not None:
            # The `else` is **unreachable today and worded as if it were not**, which is the
            # honest shape for it. `failure` is non-None for graceful and, since force gained
            # the same vocabulary, for a
            # force that found no pane — and `cleanup` and `force_stop` both walk the record to
            # ENDED on every non-raising path (`application/services.py`), so a session still
            # listed after one of those is a state no current code produces. The wording is
            # deliberately neutral
            # about *why* rather than repeating the graceful-stop advice it used to carry:
            # reached at all, this branch is a session that outlived a command that claimed to
            # end it, and telling that operator to wait for a graceful exit would be a guess.
            said = (
                f"{failure.summary} {failure.remedy}"
                if failure is not None
                else (
                    "Nothing was removed and it was left as it is.\n"
                    "Open it again to see where it is now."
                )
            )
            # The session is still listed, so the row the owner needs is on the screen they
            # are about to land on. That is what replaced the "Open session" button this
            # branch used to carry: a list they can act from beats a screen about one session
            # they then have to leave.
            return await self._sessions_reply(notice=f"{subject} is still running\n{said}")
        if failure is not None:
            # The session has left the list and the stop still had something to report. Two
            # producers reach here and the wording has to be true of both, which is why it says
            # what is *observable* — the session is gone from the list — rather than what was
            # done to it.
            #
            # Originally the narrow one: a graceful stop that did not take effect, over a record
            # the other writer DEC-005 permits had ended in the window between the two. The
            # point of threading `failure` here was to stop inferring the outcome from the
            # record, and "Stopped X" over an observation that says nothing was stopped is the
            # reading DEC-006 forbids. Found by the Stage 2 gate's evaluator and its second pass
            # independently.
            #
            # Since force started reporting its own observation, the common one: a force stop
            # that found no managed pane. It killed
            # nothing, the service recorded `VERIFIED_FORCE_STOP` anyway and the record reached
            # ENDED (DEC-017, deliberately — a row the owner cannot clear is the worse failure),
            # so the session really has gone from the list. This branch is reused rather than
            # given its own sentence because DEC-007's shared vocabulary is the safeguard that
            # makes a second surface safe, and a cause worded once per surface is how it stops
            # being shared. The `endings` table below keeps "Force stopped X" for the case where
            # a pane was actually found and killed, which is the only case that may claim it.
            return await self._sessions_reply(
                notice=f"{subject} is no longer listed\n{failure.summary} {failure.remedy}"
            )
        endings = {
            "graceful": (
                f"Stopped {subject}\n"
                "The session has ended. Its pane is gone, so its output is no longer there "
                "to inspect."
            ),
            "cleanup": f"Cleaned up {subject}\nThe session has ended and its pane is gone.",
            "force": f"Force stopped {subject}\nThe session has ended.",
        }
        return await self._sessions_reply(notice=endings[action])

    async def _records(self) -> tuple[SessionRecord, ...]:
        """The listable records, without the readiness pass.

        Every read but the sessions list, which uses `_listed_records`. Stated that way round
        rather than as "every re-read": `_sessions_reply` is also where a stop, a rename and a
        cleanup land, so it is not only reached by an owner opening the list.
        """
        if self.backend.sessions is None:
            return ()
        return self._named(only_listed(await self.backend.sessions.list_sessions()))

    async def _listed_records(self) -> tuple[SessionRecord, ...]:
        """What opening the sessions list reads: the readiness pass and the read, together.

        The pairing is `application/session_views.listed_sessions`, shared with the local
        surface. What stays here is the project-name decoration, which is this surface's own —
        the local one renders an area and a name from its own catalogue instead.
        """
        if self.backend.sessions is None:
            return ()
        return self._named(await listed_sessions(self.backend.sessions))

    def _named(self, records: tuple[SessionRecord, ...]) -> tuple[SessionRecord, ...]:
        """This surface's catalogue, joined by the shared rule.

        The join itself is `application/session_views.with_project_names` and no longer this
        adapter's: the local surface needed the same rule, and a second copy here is the
        shape BL-031 records. What stays this surface's is *which* catalogue -- the bot holds
        a ranked snapshot of its own.
        """
        return with_project_names(records, self.catalogue)

    async def _record(self, session_value: str) -> SessionRecord | None:
        return next(
            (record for record in await self._records() if str(record.session_id) == session_value),
            None,
        )

    async def _finished_sessions(self, session_values: tuple[str, ...]) -> tuple[str, ...]:
        """Which of these sessions can no longer be the subject of a notification.

        The collecting half of `_display_for`. That one decides whether to *send* about a
        session; this one decides whether the message already sent should still be in the
        chat, and both ask `notifiable` rather than keeping a second opinion about what a
        state means (DEC-001, DEC-029).

        A session the owner stopped, force-stopped, preserved or watched fail has answered the
        question its notification was asking, so the alert is obsolete and goes. The
        observation itself is untouched — it stays in `agent_activity` and so stays in the
        local feed, which is a record of what happened rather than a list of things to do.

        **A session it cannot find is left alone**, which is the narrower answer on purpose.
        Nothing deletes a session row, so an id missing from the records means the store did
        not answer rather than the session having finished, and deleting the owner's
        notifications on the strength of a failed read is not a trade worth making. The
        notification simply stands until the read succeeds.
        """
        if self.backend.sessions is None:
            return ()
        wanted = set(session_values)
        return tuple(
            str(record.session_id)
            for record in await self.backend.sessions.list_sessions()
            if str(record.session_id) in wanted and not notifiable(record.state)
        )

    async def _display_for(self, session_value: str) -> str | None:
        """Name a session for a message the owner did not ask for, or decline to.

        Two questions, answered together because the notifier is entitled to neither of them
        separately: *can this session be named*, and *is it still one worth speaking about*.
        Returning `None` for either is what the notifier already does the right thing with —
        it drops the activity as finished business rather than holding it for retry — so the
        whole liveness rule lands on this side of the callable and the adapter that renders
        the message never learns what a `SessionState` is (DEC-001).

        **The liveness question is asked here, at send time, and that placement is the
        feature.** An activity can sit in the retry queue across passes while Telegram is
        refusing sends, and the owner can press Stop while it waits; asked at drain time the
        answer would have been "running" and the message would have gone out a pass later
        anyway. `notifiable` is the lifecycle layer's to answer (DEC-029), not this module's.

        Deliberately not `_record`, and for a reason that has *changed* rather than lapsed.
        That one reads `_records`, which hides an ENDED session because the sessions list
        should not show history — and this used to reach past it so that a `SessionEnd`
        notification could still be named, that kind being precisely the one whose record had
        ENDED by delivery time. That kind is retired. What survives the retirement is the
        narrower need to *recognise* such a record in order to decline it: a session absent
        from `_records` and a session present-but-finished must not both arrive here as
        "cannot be named", because only one of them is worth an operator's attention.
        """
        if self.backend.sessions is None:
            return None
        for record in await self.backend.sessions.list_sessions():
            if str(record.session_id) == session_value:
                if not notifiable(record.state):
                    # Said out loud, and said differently from the notifier's own drop. This
                    # one is the feature working — an agent reported after the owner had
                    # already dealt with its session — while the notifier's is a session this
                    # service can no longer identify at all, which is rare and worth noticing.
                    # Collapsed into one message, the journal could not tell a quiet night
                    # from a store that had lost a row.
                    _LOG.info(
                        "not notifying about a session that is no longer running (%s)",
                        state_word(record.state, record.orphan_provenance),
                    )
                    return None
                # The whole join, not just the transform. This built its own
                # `{opaque_id: name}` index and did its own `.get(str(record.project_id))` --
                # byte-equivalent to `with_project_names`' body, which is the BL-031 shape
                # Stage 1 exists to end, surviving inside the stage that ended it. The
                # forbidden-name sweep could not see it: it greps for `def ` names, and this
                # was an inlined copy with no definition to find.
                (named,) = with_project_names((record,), self.catalogue)
                # The compact identity the redesign gives every two-line row, with the
                # sequence outside it -- the notification renders both on one plain line.
                return f"{session_identity(named)} #{named.display.sequence}"
        return None

    def _projects_reply(
        self,
        projects: tuple[CatalogProject, ...],
        *,
        view_id: str,
        page: int = 1,
        flow: str = "launch",
    ) -> RenderedMessage:
        """Render one page of the project catalogue for whichever flow asked for it.

        The stored view is keyed by flow as well as view id, so a search inside Resume
        cannot be paged into by Launch and vice versa.
        """
        picker = _PROJECT_PICKERS[flow]
        self._project_views[f"{flow}:{view_id}"] = projects
        try:
            rendered = paginate_catalogue(projects, page, self.project_page_size)
        except ValueError:
            return self._message("That project list is no longer open. Open Launch again.")
        buttons = [
            (
                Button(
                    project.name,
                    self._callback(picker.select, project.opaque_id),
                ),
            )
            for project in rendered.projects
        ]
        navigation = []
        if rendered.page > 1:
            navigation.append(
                Button("Previous", self._callback(picker.page, f"{view_id}|{rendered.page - 1}"))
            )
        if rendered.page < rendered.page_count:
            navigation.append(
                Button("Next", self._callback(picker.page, f"{view_id}|{rendered.page + 1}"))
            )
        if navigation:
            buttons.append(tuple(navigation))
        # Beside Search, because they answer the same moment: the project you wanted is not
        # on this screen. Add Project used to live on Home, one level up from the only screen
        # that can tell you it is missing.
        finders = [Button("Search", self._callback(picker.search, "search"))]
        if picker.creates_projects and self.backend.projects is not None:
            finders.append(Button("Add Project", self._callback("project.open", "areas")))
        buttons.append(tuple(finders))
        # No `back`. This screen had exactly one parent when Home was the only way to reach
        # it; the bar reaches it in one press from anywhere, so a Back pointing at Home now
        # lands the owner somewhere they were never standing — which is the one thing Back
        # must not do. `_message`'s contract is "pass it wherever there is a real parent",
        # and a screen reachable from everywhere has none. The bar is the way out.
        return self._message(
            f"<b>{picker.title} {rendered.page}/{rendered.page_count}</b>\n{picker.instruction}",
            tuple(buttons),
        )

    def _project_page_reply(self, entity_id: str, flow: str = "launch") -> RenderedMessage:
        view_id, separator, page_value = entity_id.partition("|")
        if not separator or view_id not in {"all", "search"}:
            return self._message("That project list is no longer open. Open Launch again.")
        try:
            page = int(page_value)
        except ValueError:
            return self._message("That project list is no longer open. Open Launch again.")
        projects = self._project_views.get(f"{flow}:{view_id}")
        if projects is None and view_id == "all":
            # The stored view is process-local, so a restart empties it while the button that
            # reads it now survives. "all" is reconstructible -- it is the current catalogue --
            # so paging re-renders rather than refusing, and only a search, whose query nobody
            # kept, has to send the owner back.
            projects = self.catalogue
        if projects is None:
            return self._message("That search is no longer open. Search again.")
        return self._projects_reply(projects, view_id=view_id, page=page, flow=flow)

    def _resume_projects_reply(self) -> RenderedMessage:
        return self._projects_reply(self.catalogue, view_id="all", flow="resume")

    async def _resume_profiles_reply(self, project_id: str) -> RenderedMessage:
        if not any(project.opaque_id == project_id for project in self.catalogue):
            return self._message("The project is no longer available.")
        if self.backend.conversations is None:
            return self._message("Resume is unavailable.")
        capabilities = {
            str(item.profile_id): item for item in await self.backend.conversations.capabilities()
        }
        buttons = []
        unavailable = []
        for profile in self.profiles:
            capability = capabilities.get(profile.profile_id)
            # `profile.available` and the `None` capability stay the bot's own: the local
            # surface renders no row at all for an agent it cannot offer, so it never asks
            # either question. `resume_capable` is the part both surfaces were answering
            # separately, and the `reason` composed in the `else` below is what the bot does
            # with the answer rather than part of it.
            if profile.available and capability is not None and resume_capable(capability):
                buttons.append(
                    Button(
                        _profile_name(profile.profile_id),
                        self._callback("resume.profile", f"{project_id}|{profile.profile_id}|1"),
                    )
                )
            else:
                # `any_reason` is the blocking reason when there is one and the probe note
                # otherwise -- this branch is reached with `available` true whenever the
                # catalogue is not resume-capable, and it has always shown the note there.
                reason = profile.any_reason or (
                    capability.reason if capability is not None else "catalogue_unavailable"
                )
                unavailable.append(f"{_profile_name(profile.profile_id)} ({reason})")
        text = "<b>Select a resumable agent</b>"
        if unavailable:
            text += "\nUnavailable: " + escape(", ".join(unavailable))
        return self._message(
            text,
            _button_rows(tuple(buttons))
            + ((Button("Back", self._callback("resume.open", "projects")),),),
        )

    async def _resume_catalogue_reply(self, entity_id: str) -> RenderedMessage:
        parsed = _split_resume_page(entity_id)
        if parsed is None or self.backend.conversations is None:
            return self._message("That conversation list is no longer open.")
        project_id, profile_id, page = parsed
        if not any(project.opaque_id == project_id for project in self.catalogue):
            return self._message("The project is no longer available.")
        try:
            result = await self.backend.conversations.catalogue(
                ConversationCatalogueQuery(
                    page, RESUME_PAGE_SIZE, ProfileId(profile_id), ProjectId(project_id)
                )
            )
        except ValueError:
            return self._message("That conversation list is no longer open.")
        if result.unavailable_reason is not None:
            return self._message(f"Resume is unavailable ({escape(result.unavailable_reason)}).")
        buttons = tuple(
            (
                Button(
                    _resume_button_text(summary.description, summary.updated_at),
                    # The act, not a step toward it. Launch stopped asking for a review when
                    # choosing the agent became the act; choosing a *named conversation* is a
                    # more specific choice than choosing an agent, so resume was charging an
                    # extra press for less ambiguity.
                    #
                    # An earlier version of this comment claimed DEC-008 made the single
                    # press safe. It does not, and the error mattered: DEC-008 is about
                    # *repeats* — `claim_mutation` admits one caller per token, which makes
                    # the **second** press safe and says nothing about the first, unintended
                    # one. What the first press costs here is not what it costs on launch:
                    # `SessionService._resume_locked` binds a conversation to the session it
                    # creates. Migration 8 is what stops that being *permanent* — the unique
                    # index is partial on `state <> 'ended'`, so the conversation binds again
                    # once its session has ended. An unwanted resume therefore costs a
                    # session to stop, not a conversation forever.
                    self._callback("resume.confirm", str(summary.reference), mutation=True),
                ),
            )
            for summary in result.conversations
            if resume_available(summary)
        )
        navigation = []
        if result.page > 1:
            navigation.append(
                Button(
                    "Previous",
                    self._callback("resume.page", f"{project_id}|{profile_id}|{result.page - 1}"),
                )
            )
        if result.page < result.page_count:
            navigation.append(
                Button(
                    "Next",
                    self._callback("resume.page", f"{project_id}|{profile_id}|{result.page + 1}"),
                )
            )
        # Back belongs in the navigation rows `_message` appends, like every other screen,
        # rather than as a body button — and the empty case needs it most, since it used to
        # offer nothing but a row restating that there was nothing.
        back = self._callback("resume.project", project_id)
        if not buttons:
            return self._message(
                "<b>Prior conversations</b>\nThis agent has no resumable conversation "
                "for this project.",
                back=back,
            )
        rows = list(buttons)
        if navigation:
            rows.append(tuple(navigation))
        return self._message(
            f"<b>Prior conversations {result.page}/{result.page_count}</b>\n"
            "Select a resumable conversation.",
            tuple(rows),
            back=back,
        )

    async def _resolve_resume(self, reference_value: str):
        if self.backend.conversations is None:
            return None
        try:
            reference = ConversationReference(reference_value)
        except ValueError:
            return None
        return await self.backend.conversations.resolve_for_resume(reference)

    def _profiles_reply(self, project_id: str) -> RenderedMessage:
        if not any(project.opaque_id == project_id for project in self.catalogue):
            return self._message("The project is no longer available.")
        buttons = tuple(
            Button(
                _profile_name(profile.profile_id),
                # The mutation is claimed here rather than on a review screen that no longer
                # exists. DEC-008 makes the *repeat* safe — a second press of the same button
                # is dropped by the one-shot claim, never serviced into a second session, and
                # Sub-plan 1 made that claim durable rather than process-local. It says
                # nothing about the first, unintended press; what makes that acceptable here
                # is specific to launch, namely that an unwanted launch creates a disposable
                # session and costs a stop. The resume path thirty lines below carries the
                # same correction, because assuming DEC-008 covered the first press is exactly
                # the error that let a one-press resume ship and become a gate escalation.
                self._callback(
                    "launch.profile", f"{project_id}|{profile.profile_id}", mutation=True
                ),
            )
            for profile in self.profiles
            if profile.available
        )
        return self._message(
            "<b>Select an agent</b>",
            _button_rows(buttons) + ((Button("Back", self._callback("launch.open", "projects")),),),
        )

    def _message(
        self,
        text: str,
        keyboard: tuple[tuple[Button, ...], ...] = (),
        *,
        back: str | None = None,
        back_label: str = "Back",
    ) -> RenderedMessage:
        """Render one screen and close it with Back, then the fixed navigation bar.

        Every screen ends with the same three destinations in the same position, so a move
        between flows costs one press from wherever the owner is. Before this the only way
        out was Home, and reaching Launch from a session meant going *up* to a screen whose
        entire content was the three buttons this row now carries everywhere — so the
        dashboard was a waypoint charged for on every cross-flow move.

        `back` is the screen that owns this one, and it keeps a row of its own above the bar.
        Merging the two would be the obvious saving and it is the wrong one: Back means
        something different on every screen and the bar means the same thing on all of them,
        which is the whole property that makes a fixed row worth having.

        Telegram has no chrome — no menu, no tab strip — so a keyboard row is the only place
        a persistent affordance can live, and the bottom is where a thumb already is.

        Three renders do **not** come through here, and so carry no bar. Two are permanent:
        `callback`'s pending screen, which drops its keyboard so a wait cannot be pressed
        into a second launch, and `notifications`, which is a message rather than a screen.
        Both call `render_message` directly; that is the mechanism, not an oversight.

        There was a third for one stage — Home, which rendered its own keyboard through
        `presenters.render_home` and so answered barless. That screen is gone: its counts
        live on the sessions list, its Add Project on the launch list, and its three
        destinations are this row. "Every screen closes with the bar" is now true of the
        bot rather than only of the screens built here.

        There was a third slot here once, `refresh`, offered on the two screens whose answer
        goes stale under the owner. Both re-derive their answer on every entry, so it only
        ever saved a tap, and the one thing it did that no other route did — return to the
        sessions page it was pressed on — is now what `_sessions_back` gives Back.
        """
        rows: list[tuple[Button, ...]] = []
        if back is not None:
            # `back_label` names the parent where a screen wants to -- the session detail says
            # `‹ Back to sessions` -- and stays the bare word everywhere a parent is obvious.
            rows.append((Button(back_label, back),))
        # Resume is absent rather than disabled on a host that wired no conversation
        # service: Telegram has no disabled state, and a button that answers "unavailable"
        # is a worse answer than no button.
        bar = [
            Button(
                _tab("Sessions", self._flow == "sessions"),
                self._callback("sessions.open", "sessions"),
            ),
            Button(
                _tab("Launch", self._flow == "launch"),
                self._callback("launch.open", "projects"),
            ),
        ]
        if self.backend.conversations is not None:
            bar.append(
                Button(
                    _tab("Resume", self._flow == "resume"),
                    self._callback("resume.open", "projects"),
                )
            )
        rows.append(tuple(bar))
        return render_message(text, keyboard + tuple(rows))

    def _callback(self, action: str, entity_id: str, *, mutation: bool = False) -> str:
        """Mint a token for a screen that has not been delivered yet.

        The keyboard is built before the message exists, so the token is created unbound and
        `LiveView` attaches it once Telegram has answered with a message id.
        """
        return self.callbacks.create(
            action,
            entity_id,
            self.owner_user_id,
            self.owner_chat_id,
            mutation=mutation,
        )

    def _guided_text_reply(self, action: str, text: str | None = None) -> dict[str, object]:
        instruction, placeholder = _GUIDED_TEXT_ENTRY.get(
            action, ("Reply with a label, or send Skip, Cancel, or Back.", "Optional session label")
        )
        return {
            "text": text or instruction,
            "reply_markup": ForceReply(input_field_placeholder=placeholder),
        }


def build_private_bot(
    owner_user_id: int,
    owner_chat_id: int,
    *,
    stops: StopController | None = None,
    view: LiveView | None = None,
    notifier: ActivityNotifier | None = None,
    **boundary: object,
) -> PrivateBotBoundary:
    """Compose a working bot: the boundary, and the three collaborators it drives.

    These used to be built by `PrivateBotBoundary.__post_init__`, out of whatever the
    boundary had been handed. Convenient, because everything they need is already there —
    and wrong for the same reason, since it left the one object whose job is deciding how
    the pieces fit with no say in three of them. A composition root that wanted a different
    live view, or a notifier over a second callback store, had nowhere to say so.

    **The ports are the boundary's, not fresh ones.** All three are read off the constructed
    boundary rather than from this function's arguments, so the defaults it applied — the
    in-memory `CallbackStateStore`, `ChatViewStore` and `StandingNotificationStore` — are
    the ones they share. Building a `CallbackStateStore()` here instead would run, would
    pass every screen test, and would silently drop every button the boundary had minted.

    **The notifier is attached after construction, and the cycle is real.** It takes
    `display` and `finished`, which name a session for a message the owner did not ask for;
    naming one needs the catalogue the boundary is holding and the liveness rule it applies
    at send time. So it cannot precede the boundary, and the choice is where to pay for
    that — here, in the open, or hidden inside the object it entangles. Here.
    """
    bot = PrivateBotBoundary(owner_user_id, owner_chat_id, **boundary)  # type: ignore[arg-type]
    bot.stops = stops if stops is not None else StopController(bot.callbacks)
    bot.view = (
        view
        if view is not None
        else LiveView(chat_id=owner_chat_id, callbacks=bot.callbacks, anchors=bot.anchors)
    )
    bot.notifier = (
        notifier
        if notifier is not None
        else ActivityNotifier(
            view=bot.view,
            callbacks=bot.callbacks,
            owner_user_id=owner_user_id,
            display=bot._display_for,  # noqa: SLF001 -- the cycle this factory exists to pay
            standing=bot.standing,
            finished=bot._finished_sessions,  # noqa: SLF001
        )
    )
    return bot


async def run_private_bot(
    secrets: TelegramSecrets, boundary: PrivateBotBoundary | None = None
) -> None:
    """Long-poll the approved bot until SIGTERM/SIGINT, refusing a competing webhook."""
    # The factory, not the class. This default used to be a bare `PrivateBotBoundary(...)`,
    # which worked only while `__post_init__` built the collaborators for itself: a bare one
    # now leaves `stops`, `view` and `notifier` unset, and `notifier.attach` below is six
    # lines away. Nothing in the suite calls this function, so the AttributeError would have
    # reached a real run first.
    boundary = boundary or build_private_bot(secrets.owner_user_id, secrets.owner_chat_id)
    # Sequential update handling is load-bearing rather than incidental: a render mints its
    # keyboard unbound and binds it once Telegram answers, and `bind_pending` adopts every
    # unbound token in the chat. Two renders in flight at once would let one screen's buttons
    # be adopted by the other's message. This is python-telegram-bot's default; it is written
    # out so a change made for throughput cannot quietly reopen that interleaving -- and
    # `test_the_bot_handles_updates_sequentially` now fails on `True`, on a non-literal, and
    # on the call being deleted, because until it existed this comment was the whole guard.
    # A second argument rests on the same setting at `_sessions_page` above.
    application = ApplicationBuilder().token(secrets.bot_token).concurrent_updates(False).build()
    application.add_handler(CommandHandler("start", boundary.start))
    application.add_handler(CommandHandler("launch", boundary.launch_command))
    application.add_handler(CommandHandler("resume", boundary.resume_command))
    application.add_handler(CommandHandler("sessions", boundary.sessions_command))
    application.add_handler(CommandHandler("help", boundary.help_command))
    # Registered on every host, while `owner_commands` *lists* it only where the capability
    # is wired: a command a bot does not handle is answered by silence, and an owner who
    # typed it from a menu their other host published deserves the sentence instead.
    application.add_handler(CommandHandler("remote", boundary.remote_command))
    application.add_handler(CallbackQueryHandler(boundary.callback))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE, boundary.text)
    )
    stopping = asyncio.Event()
    _install_stop_signals(stopping)
    await application.initialize()
    # The one thing the boundary cannot build for itself. Every other message this bot sends
    # answers an update and reaches Telegram through that update's own bot handle; a
    # notification answers nothing, so it needs the application's, and the application does
    # not exist until here.
    boundary.notifier.attach(application.bot)
    try:
        await _sync_owner_metadata(
            application.bot, secrets.owner_chat_id, owner_commands(boundary.backend)
        )
        webhook = await application.bot.get_webhook_info()
        if webhook.url:
            raise RuntimeError("Telegram webhook is configured; refusing concurrent polling")
        await application.start()
        if application.updater is None:
            raise RuntimeError("Telegram updater is unavailable")
        await application.updater.start_polling(drop_pending_updates=False)
        await stopping.wait()
    finally:
        if application.updater is not None and application.updater.running:
            await application.updater.stop()
        if application.running:
            await application.stop()
        await application.shutdown()


async def _sync_owner_metadata(
    bot, owner_chat_id: int, commands: tuple[BotCommand, ...] = _OWNER_COMMANDS
) -> None:
    """Publish this deployment's shell. `commands` is what *this* composition wired.

    Defaulted rather than required because the menu is the same four on every host that wired
    no host capability, and every caller but `run_private_bot` -- which alone holds a backend
    to ask -- means exactly those four.
    """
    scope = BotCommandScopeChat(owner_chat_id)
    await bot.delete_my_commands()
    await bot.set_my_commands(commands, scope=scope)
    await bot.set_chat_menu_button(chat_id=owner_chat_id, menu_button=MenuButtonCommands())
    await bot.set_my_description(_BOT_DESCRIPTION)
    await bot.set_my_short_description(_BOT_SHORT_DESCRIPTION)


async def audit_owner_metadata(secrets: TelegramSecrets) -> dict[str, object]:
    """Read the public Telegram shell without exposing the configured credential."""
    async with Bot(secrets.bot_token) as bot:
        return await audit_bot_metadata(bot, secrets.owner_chat_id)


async def audit_bot_metadata(bot, owner_chat_id: int) -> dict[str, object]:
    """Check owner-only command metadata against the reviewed bot shell."""
    owner_scope = BotCommandScopeChat(owner_chat_id)
    default_commands = await bot.get_my_commands()
    owner_commands = await bot.get_my_commands(scope=owner_scope)
    menu = await bot.get_chat_menu_button(chat_id=owner_chat_id)
    description = await bot.get_my_description()
    short_description = await bot.get_my_short_description()
    expected_commands = [command.command for command in _OWNER_COMMANDS]
    # Two shells are healthy, because this reads a live bot over the network and has no
    # composition to ask which one it should be: a host that wired the host-level toggle
    # publishes one command more, and reporting that as unhealthy would make the audit fail on
    # every machine with `codex` installed. What it still catches is the failure it was written
    # for -- a menu that has drifted from the reviewed set in any other way.
    healthy_commands = (expected_commands, [*expected_commands, _HOST_REMOTE_COMMAND.command])
    report = {
        "default_commands": [command.command for command in default_commands],
        "owner_commands": [command.command for command in owner_commands],
        "owner_menu": getattr(menu, "type", None),
        "description_matches": description.description == _BOT_DESCRIPTION,
        "short_description_matches": short_description.short_description == _BOT_SHORT_DESCRIPTION,
    }
    report["healthy"] = (
        not report["default_commands"]
        and report["owner_commands"] in healthy_commands
        and report["owner_menu"] == "commands"
        and report["description_matches"]
        and report["short_description_matches"]
    )
    return report


def _install_stop_signals(stopping: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for event in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(event, stopping.set)
        except NotImplementedError:
            signal.signal(event, lambda _number, _frame: stopping.set())


def _host_direction(entity_id: str) -> RemoteControlState | None:
    """The direction a host token carries, or `None` for anything that is not one.

    `UNKNOWN` is a member of the enum and is refused here rather than at the command, which
    raises `ValueError`: a token minted before a deploy is still live (DEC-011), so this
    boundary answers an entity it no longer draws with a sentence rather than a traceback.
    """
    if entity_id not in {RemoteControlState.ACTIVE.value, RemoteControlState.INACTIVE.value}:
        return None
    return RemoteControlState(entity_id)


def _session_scope(entity_id: str) -> str:
    """The session an entity id is about, for the composite ids some actions carry.

    A stop token names `session:profile` and a remote-control token names `session|state`.
    Everything else is left whole — a project id, or the `home` a pre-upgrade `nav.home` or
    `nav.refresh` token still carries, has no session in it and will simply never match one.

    *A close-out edit "corrected" this to drop `home`, on the grounds that nothing has minted
    such an entity id since Stage 2. Minting is not the same as reaching: those tokens are
    durable in SQLite and outlive the deploy that stopped drawing them — which is the whole
    of DEC-011 and the reason `_reply_for` keeps the handler — so `home` still arrives here
    and `test_live_service.py:941` pins exactly that. The example was right; the correction
    was the thing taken from a premise rather than read off the code.*
    """
    for separator in (":", "|"):
        entity_id = entity_id.split(separator, 1)[0]
    return entity_id


def _reply_arguments(message: RenderedMessage) -> dict[str, object]:
    return {
        "text": message.text,
        "parse_mode": ParseMode.HTML,
        # `uniform_keyboard` and not `message.keyboard`: the floor is presentation, applied at
        # the boundary where a typed screen becomes Telegram's own types, so no screen builder
        # has to remember it and no test of a screen's *content* is reading padding.
        "reply_markup": InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(button.text, callback_data=button.callback_data)
                    for button in row
                ]
                for row in uniform_keyboard(message.keyboard)
            ]
        ),
    }


def _page_number(value: str) -> int:
    """Read a page from a token this bot minted, falling back to the first page."""
    try:
        return int(value)
    except ValueError:
        return 1


def _split_launch(value: str) -> tuple[str, str]:
    project_id, separator, profile_id = value.partition("|")
    if not separator:
        raise ValueError("launch callback is invalid")
    return project_id, profile_id


def _entry_instruction(action: str) -> str:
    return _ENTRY_INSTRUCTIONS.get(action, "Reply below with an optional session label.")


def _split_resume_page(value: str) -> tuple[str, str, int] | None:
    project_id, separator, remainder = value.partition("|")
    profile_id, separator2, page_value = remainder.partition("|")
    if not separator or not separator2:
        return None
    try:
        page = int(page_value)
    except ValueError:
        return None
    return (project_id, profile_id, page) if page > 0 else None


def _resume_button_text(description: str | None, updated_at: datetime) -> str:
    """Keep a resume title useful without overflowing the compact keyboard.

    "Owner-approved" was the old wording here and it implied a vetting step that does not
    exist: this is the owner's own last instruction to the agent, read back out of the
    provider's transcript and truncated to fit a button (BL-007). See
    `domain/conversations.py` `ConversationSummary` for what is and is not screened.

    The obvious word for that text is one `tests/architecture/check_telegram_actions.py`
    forbids anywhere in this package, comments included, because its presence here would
    otherwise mean this adapter had grown a way to *send* one. The check is a plain substring
    scan and cannot tell prose from a call, which is the right trade for a guard on the
    control surface -- so the wording works around it rather than the guard being narrowed.
    """
    prefix = description[:48].rstrip() if description else "Resumable"
    return f"{prefix} · {updated_at:%Y-%m-%d %H:%M UTC}"


def _profile_name(profile_id: str) -> str:
    return {
        "claude": "Claude",
        "claude-remote": "Claude Remote",
        "codex": "Codex",
        "opencode": "OpenCode",
        "cursor-agent": "Cursor Agent",
    }.get(profile_id, "Unavailable")


_ACTIVE_TAB = "• "
"""What marks the tab the owner is standing in, prefixed rather than wrapped.

A prefix keeps the label's first characters where the eye already reads them; wrapping the
name in symbols moves every label one column right and makes the three read as decoration.
"""

_FLOW_OF_PREFIX = {
    "sessions": "sessions",
    "session": "sessions",
    "remote": "sessions",
    "launch": "launch",
    "project": "launch",
    "resume": "resume",
}
"""Which flow an action's screen belongs to, keyed by the part before its dot.

A session's own screens — its detail, its capture, its rename, its Remote Control
confirmation — are the *sessions* flow, because that is where the owner came from and where
Back returns them; the marker tracks where they are standing, not which button they last
pressed. The bare stop actions carry no dot and are mapped beside them for the same reason.

`project` is the **Add Project wizard** and nothing else: `launch.project` and
`resume.project` partition on their own prefixes and never reach this key. It maps to
`launch` because that is where the wizard lives — it is entered from the launch project
list, and the screen you add a project from is the screen that told you it was missing.

*(This paragraph was written one stage early, while the wizard was still entered from Home,
and said so. Home is gone and the entry point moved with it, so the mapping that was
"scheduled" is now simply current.)*
"""


def _flow_of(action: str) -> str | None:
    if action in {GRACEFUL, CLEANUP, FORCE, CONFIRMED_FORCE}:
        return "sessions"
    return _FLOW_OF_PREFIX.get(action.partition(".")[0])


def _tab(label: str, active: bool) -> str:
    return f"{_ACTIVE_TAB}{label}" if active else label


def _button_rows(buttons: tuple[Button, ...], width: int = 2) -> tuple[tuple[Button, ...], ...]:
    return tuple(tuple(buttons[index : index + width]) for index in range(0, len(buttons), width))


def _state_explanation(state: SessionState, orphan_provenance: OrphanProvenance | None) -> str:
    """Defer to the shared mapping so both surfaces describe a state identically."""
    return explain_state(state, orphan_provenance)
