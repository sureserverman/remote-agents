"""Live private-bot polling boundary with exact owner/chat authorization."""

from __future__ import annotations

import asyncio
import io
import logging
import signal
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from html import escape
from time import monotonic

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeChat,
    CopyTextButton,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from remote_agents.adapters.telegram.binding_notifications import BindingNotifier
from remote_agents.adapters.telegram.callbacks import (
    CallbackStateStore,
)
from remote_agents.adapters.telegram.flood import FloodGate
from remote_agents.adapters.telegram.inspection import inspect_capture
from remote_agents.adapters.telegram.labels import _MESSAGE_EMOJI
from remote_agents.adapters.telegram.labels import action_button_label as action_button_label
from remote_agents.adapters.telegram.labels import unmarked as unmarked
from remote_agents.adapters.telegram.launch_resume import LaunchResumeScreens
from remote_agents.adapters.telegram.limit_reset_notifications import (
    LimitResetNotifier,
)
from remote_agents.adapters.telegram.live_view import ChatViewStore, LiveView
from remote_agents.adapters.telegram.navigation import flow_of as _flow_of
from remote_agents.adapters.telegram.navigation import navigation_row
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
)
from remote_agents.adapters.telegram.restart_notifications import RestartNotifier
from remote_agents.adapters.telegram.rollover_notifications import RolloverNotifier
from remote_agents.adapters.telegram.schedule_notifications import ScheduleNotifier
from remote_agents.adapters.telegram.schedules import (
    ScheduleScreens,
    ScheduleState,
)
from remote_agents.adapters.telegram.screen import reply_arguments as _reply_arguments
from remote_agents.adapters.telegram.sessions import _RELAY_REASON_WORDS as _RELAY_REASON_WORDS
from remote_agents.adapters.telegram.sessions import SessionListState, SessionScreens
from remote_agents.adapters.telegram.sessions import git_facts as git_facts
from remote_agents.adapters.telegram.sessions import session_state_line as session_state_line
from remote_agents.adapters.telegram.settings import (
    SettingsScreens,
    available_sections,
)
from remote_agents.adapters.telegram.stops import CONFIRMED_FORCE, StopController
from remote_agents.adapters.telegram.trust_notifications import (
    TrustNotifier,
)
from remote_agents.application.backend import Backend
from remote_agents.application.errors import (
    SessionNotFoundError,
)
from remote_agents.application.host_remote_control import (
    HOST_REMOTE_CONTROL_TITLE,
    PairCommand,
    pair_available,
)
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import (
    CatalogProject,
    rank_if_usage_is_reported,
    search_catalogue,
)
from remote_agents.application.remote_control_default import (
    remote_control_default_line,
)
from remote_agents.application.session_actions import (
    ACTION_LABELS,
    CLEANUP,
    FORCE,
    GRACEFUL,
    notifiable,
    state_word,
)
from remote_agents.application.session_views import (
    CATALOGUE_GAP_RETRY_SECONDS,
    listed_sessions,
    only_listed,
    project_name,
    schedule_lines,
    session_identity,
    session_lines,
    session_row,
    session_row_parts,
    unnamed_projects,
    with_project_names,
)
from remote_agents.config import TelegramSecrets
from remote_agents.domain.models import (
    SessionId,
    SessionRecord,
    normalize_label,
)
from remote_agents.domain.projects import ProjectIdentity
from remote_agents.ports.callback_state import CallbackStatePort
from remote_agents.ports.chat_view import ChatViewPort
from remote_agents.ports.message_relay import (
    MessageRelay,
    RelayOutcome,
    RelayResult,
)
from remote_agents.ports.standing_notification import StandingNotificationPort
from remote_agents.ports.terminal import (
    TerminalTargetMissing,
)

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
    BotCommand("settings", "Configure this machine and future sessions"),
    BotCommand("help", "Show available actions"),
)
"""Settings is always a destination. Its root explains which controls this host provides."""
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
    "session.message": (
        "Reply with the message to type into this session. Send Cancel or Back to leave it.",
        "Message",
    ),
    "schedule.time": (
        "Reply with a time: HH:MM, or YYYY-MM-DD HH:MM. Send Cancel or Back to leave this step.",
        "HH:MM",
    ),
    "schedule.message": (
        "Reply with the message to type when the session starts. Send Cancel or Back to leave "
        "this step.",
        "Message",
    ),
}
_ENTRY_INSTRUCTIONS = {
    "launch.search": "Reply below with a project name.",
    "resume.search": "Reply below with a project name.",
    "project.name": "Reply below with the new project name.",
    "session.rename": "Reply below with a name for this session.",
    "session.message": "Reply below with the message to type into this session.",
    "schedule.time": "Reply below with a time.",
    "schedule.message": "Reply below with the message to type when the session starts.",
}
_SEARCH_ACTIONS = {"launch.search": "launch", "resume.search": "resume"}
_TEXT_ENTRY_ACTIONS = frozenset(
    {
        "launch.search",
        "resume.search",
        "project.area",
        "session.rename",
        "session.message",
        "schedule.time",
        "schedule.message",
    }
)
"""The actions that open a guided step, and so the only ones that may leave a box open."""


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


#: Older standing trust questions share action names with the menu. Resolve their carrier
#: against the durable trust question before adopting an otherwise unknown screen.
_TRUST_MESSAGE_ACTIONS = frozenset({"session.trust", "session.decline", "session.detail"})

_PENDING_NOTICES = {
    "graceful": "Stopping the session — waiting for the agent to exit…",
    "cleanup": "Cleaning up the session…",
    CONFIRMED_FORCE: "Force stopping the session…",
    # A decline is not a store read. It sends the agent's own negative keys and then polls the
    # pane for up to `_TRUST_DECLINE_WAIT_SECONDS` before falling through to a kill, which is
    # well past this table's admission rule. Two things follow from being absent, and both are
    # the reasons the table exists: the owner waits on a still-live button with no notice, and
    # `callback`'s `if pending is None: raise` costs them the screen on a failure whose token
    # has already been spent.
    "session.decline": "Closing the session without trusting it…",
    # **The accept half, for every word of the reason above.** It sends the agent's own
    # affirmative keys and then sleeps `_TRUST_ANSWER_WAIT_SECONDS` before re-reading the
    # pane, so it makes the owner wait for the same *reason* as its sibling, though not for the
    # same span: this one sleeps a fixed second, while a decline polls up to
    # `_TRUST_DECLINE_WAIT_SECONDS` and then kills. Same shape, different bound -- and its token
    # is claimed
    # before the terminal call, so an escaping failure spent the one-shot and left them a
    # cleared spinner with no words. It was absent while `session.decline` was present, which
    # is the asymmetry rather than a decision; found by the second Tier-2 pass on this task.
    "session.trust": "Answering the folder-trust question…",
    "launch.profile": "Launching — waiting for the agent to become ready…",
    "launch.mode": "Launching — waiting for the agent to become ready…",
    "resume.confirm": "Resuming — waiting for the agent to become ready…",
    # The slowest action in this bot, and it had no notice. Enabling probes the daemon (10 s
    # ceiling), runs the enable (30 s, a relay round trip), then re-reads the daemon -- so the
    # confirm screen could sit unchanged with its button still drawn for the better part of a
    # minute, and a second press answered "that action has already run" for something still in
    # flight. Also the difference between a failure the owner is told about and one that
    # leaves the screen silently unchanged: `callback`'s `if pending is None: raise` costs the
    # screen for actions absent from this table.
    "host.remote.confirm": "Changing this machine's Remote Control — waiting for the daemon…",
}
"""The actions that make the owner wait, and what to show them while they do.

Each of these reaches a terminal and then polls it: a launch waits for its profile's
readiness marker, a graceful stop waits for the pane to exit, and both are bounded by the
same startup timeout — twenty seconds in the deployed composition. Everything absent from
this table answers from the store or from one tmux call, fast enough that a notice would
flash and be gone.
"""

_SESSION_ENDING_ACTIONS = frozenset({GRACEFUL, CLEANUP, CONFIRMED_FORCE, "session.decline"})
"""The actions after which a session is no longer one this service speaks first about.

The same four members as `_LIST_LANDING_ACTIONS` below and deliberately a separate name: that
one is about which *screen* to draw next, this one about which messages have stopped being
true. Bare `FORCE` is absent from both, and for the same reason -- it draws the confirmation,
so nothing has happened yet. `session.decline` is present in both because it really does end
the session on the press, with no second screen between (DEC-078).

What it triggers is about timing and nothing more. `ActivityNotifier.retire_finished` runs on
the delivery pass regardless, because the local console ends sessions in another process and
this handler never hears about those; sweeping here as well is what makes the owner watch their
own stop take its notification with it, rather than find it gone half a minute later.
"""

_LIST_LANDING_ACTIONS = frozenset({GRACEFUL, CLEANUP, CONFIRMED_FORCE, "session.decline"})
"""The actions that draw the **session list** rather than a screen about their own session.

`_release_attachment` is told what the next screen is about, and every other action can
answer that with the entity it carries. These cannot: they carry a session id and then land
somewhere that is not about it, so a captured document would be retained on behalf of a
session the owner can no longer see. Unconfirmed `FORCE` is deliberately absent — it draws
the confirmation, which *is* about that session. `session.decline` is present: it lands on the
list, and the session it names is gone by then.
"""


_HELD_RELAY_NOTICES = 20
"""How many queued-message notices a flood hold may keep; past it the oldest is dropped, logged."""

#: Screens the live view draws but must never put back at the bottom of the chat by itself.
#:
#: One member, and the rule is about content rather than about flow: `host.remote.pair`
#: renders a live pairing secret, and `move_to_bottom` re-sends the last remembered screen
#: once per notification pass with no press involved. Everything else here is a menu, and a
#: menu is exactly what that mechanism exists to keep reachable.
_UNREMEMBERED_ACTIONS = frozenset({"host.remote.pair"})


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
    trust_dialogs: Mapping[str, object] = field(default_factory=dict)
    """Which curated profiles this project can read a folder-trust dialog for.

    Handed in for exactly the reason `glyphs` is, and from the same registry fold: the dialogs
    belong to the provider verticals (DEC-070) and this adapter imports nothing under
    `adapters.agents`. It replaced a frozenset in the domain naming claude alone, which is why
    a codex session used to carry one answer where the owner had asked for two.
    """

    glyphs: Mapping[str, str] = field(default_factory=dict)
    """Each curated profile's mark, as the composition root read it off the registry.

    A mapping handed in, not a table grown here: the marks belong to the provider verticals
    (DEC-070) and this adapter imports nothing under `adapters.agents` — the architecture
    check pins that, and a dict of literals here would pass it while defeating it. This
    surface owns only the *sentence* the mark is drawn into (DEC-043).

    Empty by default, which is the honest state for the many compositions that wire no
    registry at all: a profile with no mark renders exactly the label drawn before this
    field existed.
    """
    held_relay_notices: list[str] = field(default_factory=list, repr=False)
    """Queued-message notices the chat's flood hold stopped, oldest first (BL-107).

    Kept rather than dropped, because the Cancel reply promises "You'll be told if it does". The
    service flushes them on every activity pass through `flush_relay_notices`. In memory only,
    like the rollover and schedule notices (BL-127, owner ruling 2026-10-08): a restart while
    one is held loses it, which a hold of minutes makes rare."""
    names: Mapping[str, str] = field(default_factory=dict)
    """Each curated profile's display name, read off the registry like `glyphs` (BL-101).

    Handed in for the reason the marks are: the names belong to the provider verticals
    (DEC-070) and this adapter imports nothing under `adapters.agents`. A profile missing
    from it is captioned "Unavailable" rather than given a name spelled here.
    """
    message_relay: MessageRelay | None = None
    """What sends the owner's message into a session (DEC-099), or None where none is wired.

    A port, named for messages, because this package may not name what it relays (DEC-075). None
    is the honest state for every composition but the service's, and a session screen then
    offers no Send message row.
    """

    relayable: frozenset[str] = frozenset()
    """The curated profiles a message may be sent to -- those whose agent declares a composer.

    Folded by the composition root off the registry, like `trust_dialogs` and for its reason:
    the declarations belong to the verticals (DEC-070).
    """
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
    _last_drawn_key: tuple[object, ...] | None = field(init=False, default=None)
    """The `_page_key` of the last sessions page actually drawn, or None before the first."""
    _last_drawn_at: float = field(init=False, default=0.0)
    """When that draw happened, so `_REFRESH_INTERVAL_SECONDS` can bound how stale it gets."""
    flood: FloodGate = field(init=False, default_factory=FloodGate)
    """When Telegram last said this chat may not be spoken to, shared by every sender.

    On the boundary rather than inside one of them because a flood ban is the chat's state:
    the redraw, the command handlers and the activity notifier each used to discover one
    separately and each kept going regardless.
    """
    notifier: ActivityNotifier = field(init=False)
    """The three collaborators -- `stops`, `view` and this one -- filled by `build_private_bot`
    rather than by this class.

    `init=False` and genuinely unset until the factory runs, which is the honest state: a
    bare `PrivateBotBoundary(...)` is a boundary nobody has wired. Defaulting them instead
    would be worse than either — it would give a composition root that never chose a live
    view one that silently works, which is exactly the situation moving the wiring out was
    meant to end.
    """
    rollover_notifier: RolloverNotifier | None = None
    """Says when a rollover failed or its stop did not go through, or None where the backend
    has no rollovers. Bot-only, like the schedule notices; only failures push (DEC-031)."""
    restart_notifier: RestartNotifier | None = None
    binding_notifier: BindingNotifier | None = None
    """Says, once per restart, which interrupted sessions came back (DEC-127), or None where the
    backend has no sessions. Bot-only, like the rollover notices."""
    schedule_notifier: ScheduleNotifier | None = None
    """Says what each scheduled fire came to, or None where nothing manages schedules.

    Bot-only, like the limit-reset notice: a missed or skipped fire is about no live session, and
    the local feed is session-shaped (DEC-031 as amended for scheduled sessions).
    """
    limit_reset_notifier: LimitResetNotifier | None = None
    """The pass that notices a provider wiping its meters early, or None where nothing can.

    None wherever `Backend.limits` is not wired — a host whose providers publish no limits has
    nothing for it to read — and in every boundary built by a test that constructs one
    directly. Bot-only by decision (DEC-097): the message is about the account and the local
    feed is session-shaped.
    """

    trust_notifier: TrustNotifier | None = None
    """The pass that asks an untrusted session's folder-trust question, or None where none does.

    Optional rather than `init=False`, unlike the three collaborators above, because it is
    genuinely absent from most compositions: only a service wired with a durable trust store
    has one, and every boundary a test constructs directly has none. `run_private_bot` guards
    on it.

    Named rather than `object`, which is what `test_the_boundary_declares_no_untyped_backend_
    field` insists on and was right to: an untyped seam on this class is how a collaborator
    gets swapped for something that only happens to answer the same calls.
    """
    _awaiting_text: dict[tuple[int, int], _TextEntry] = field(default_factory=dict)
    #: The schedule being made, one step at a time, until it is confirmed or abandoned.
    #: Process-local like `_awaiting_text`: a restart mid-flow forgets it, and the next step
    #: answers that the flow has to start again.
    _schedule_state: ScheduleState = field(default_factory=ScheduleState)
    _attachment: tuple[str, int] | None = None
    #: The pairing-code message, until the owner's next interaction takes it out of the chat.
    #: A bare id rather than the attachment's `(scope, id)` pair, because a pairing code is
    #: about no session and so has nothing to stay on screen for.
    _pairing_message: int | None = None
    _project_views: dict[str, tuple[CatalogProject, ...]] = field(default_factory=dict)
    _flow: str | None = None
    """Active footer destination, or None on a neutral screen such as Help.

    Commands and resolved callbacks select the destination before rendering. PTB handles
    updates sequentially, so each interaction draws its own marker and binds its own tokens.
    The live-view screen tag separately determines whether a background redraw is allowed.
    """
    _catalogue_gap_read_at: float | None = None
    """When this process last re-read the catalogue because a session named an unknown project.

    Monotonic, and `None` until it happens. Written and read only by `_close_catalogue_gap`,
    which carries the account. Process-local like the render state below it, and for a
    stronger reason: it is a rate limit on a read, so a restart forgetting it costs one extra
    registry read and nothing else.
    """
    _bot: Bot | None = None
    """The application's own Telegram handle, for the one edit that answers no update.

    Typed `Bot` rather than `object | None`, which
    `test_the_boundary_declares_no_untyped_backend_field` rightly refuses: that rule is about
    a frontend reaching into anonymous slots by guesswork,
    and this is a named handle of a type this module already imports. The two notifiers type
    theirs loosely only because they are constructed before `telegram` is in scope for them.

    Every screen this bot draws answers an update and reaches Telegram through that update's
    bot handle. A redraw triggered by a store change answers nothing -- there is no update and
    no press -- so it needs the application's handle, exactly as the two notifiers do, and the
    application does not exist until `run_private_bot` initializes it. `attach_bot` is where
    it arrives. `None` until then, which is why `redraw_sessions_if_open` answers False rather
    than raising: a boundary nobody attached is one nothing is serving.
    """
    _drawing_screen: str | None = None
    """The tag the *next* render should remember, set by the reply builder that knows it.

    A render takes generic `arguments` and cannot tell a sessions list from a help screen, and
    the three call sites that render are equally blind -- they have an update, not a subject.
    The function that builds the list is the one thing in the process that knows what it built,
    so it marks, and the render consumes and clears. Clearing is the load-bearing half:
    untagged means "some other screen", so a mark left standing would make the next unrelated
    screen claim to be the list.
    """
    _handling_press: bool = False
    """True while a callback query is being handled, so a store change does not draw over it.

    That press is about to render a screen of its own -- a detail, a confirmation, a stop's
    answer -- and an edit landing underneath it would put the sessions list over something the
    owner actually asked for. Safe as a plain flag rather than a lock for the reason
    `_bar_marker` records above: `run_private_bot` builds the application with
    `concurrent_updates(False)`, so updates are handled one at a time.
    """
    _redraw_owed: bool = False
    """Whether a store change arrived inside the edit floor and still needs drawing.

    The difference between coalescing and throttling, and this class claimed the first while
    doing the second. `settle_owed_redraw` is what pays it, and `_settle_task` is what makes
    sure something does.
    """
    _settle_task: asyncio.Task[None] | None = None
    """The one-shot task that pays an owed redraw once the floor lifts.

    **This is a reversal, and the argument it replaces was wrong.** The first version refused
    to own a task at all -- "a timer here would be a second clock in a class whose whole design
    is one screen, redrawn on demand" -- and left settling to the store-change listener. But
    that listener runs only when the store changes, and a debt is incurred precisely when a
    change has *just* been refused: settling was attempted once, immediately, while the floor
    still forbade it, and a store that went quiet afterwards left the page stale for good.

    A one-shot task per suppressed change is not a clock. It is the deferral the word
    "coalesced" was always promising, and without it that word was simply untrue.
    """
    _redraw_allowed_at: float = 0.0
    """The monotonic moment the next store-driven redraw of the sessions page may go out.

    Process-local render state like the two fields around it. Telegram rate-limits edits per
    chat, and a burst of launches writes the store several times a second -- so the watcher
    can report changes faster than this chat may be edited. One edit per
    `_REDRAW_INTERVAL_SECONDS` is the ceiling, and it is enforced here rather than by asking
    Telegram and handling the refusal, because a refusal costs the edit *and* the round trip.

    Deliberately **not** persisted and deliberately not a lock. A restart forgets it and
    allows one immediate redraw, which is correct: there is no screen to protect yet.
    """
    _session_list_state: SessionListState = field(default_factory=SessionListState)
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

        The read itself, and the ranking it applies, are `_reread_catalogue`'s — this is that
        read plus the one thing only a picker-open wants, below.

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
        await self._reread_catalogue()
        # Dropping the remembered picker views belongs to *opening a picker*, not to reading
        # the catalogue -- which is why it is here and not in `_reread_catalogue`. The
        # paragraph above says paging must not re-read because a reshuffle under a thumb is
        # the harm; `_close_catalogue_gap` re-reads on a path that is not a picker at all, and
        # would do exactly that harm if it cleared these too. A remembered view is a snapshot
        # of a message already sent, and a row in one that has since left the catalogue is
        # caught where it is pressed -- `_resume_profiles_reply` checks membership before it
        # offers anything.
        self._project_views.clear()

    async def _reread_catalogue(self) -> None:
        """Read the projects off the event loop and rank them into `self.catalogue`.

        The registry read and development-root walk run on a thread, so refreshing never
        stalls unrelated Telegram interactions or tmux polling.

        The recency ranking is applied **here**, once, rather than in either picker. Launch,
        Resume and search all render `self.catalogue` -- the two pickers share
        `_projects_reply` and search filters the same tuple -- so ordering it at the source
        reaches all three without a ranking call per rendered row, and without either picker
        knowing that a ranking exists. It is also why a session launched during the run
        changes the next render's order: the usage read happens on the refresh that follows it.

        Separated from `refresh_catalogue` so that the two things that want a fresh catalogue
        can differ in what else they do about it. A host that wired no read is a no-op rather
        than a failure, exactly as before.
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

    async def _close_catalogue_gap(self, records: tuple[SessionRecord, ...]) -> None:
        """Re-read the catalogue when a session names a project this snapshot never had.

        **The same hole the console's sessions pane had, one surface over.** `self.catalogue`
        is refreshed when a *picker* opens -- `launch.open`, `resume.open`, `/launch` -- and
        the sessions list is not a picker. So a project registered while this service was
        running, whose sessions were launched from the local surface, rendered in Telegram as
        the raw 24-character opaque id until the owner happened to open Launch. Milder than
        the pane's version, which could never recover at all, and the same defect.

        The gap test and the retry bound are `application/session_views`', shared with the
        local surface so the two cannot drift on when a re-read is worth doing. What is this
        adapter's own is the re-read: `_reread_catalogue` and deliberately not
        `refresh_catalogue`, because dropping the remembered picker views here would reshuffle
        a list the owner may be paging through -- the harm `refresh_catalogue`'s own docstring
        warns about, arriving from a path that never opens a picker.

        Silent on failure, like the join it serves: rows draw under the opaque id, which is
        unreadable and correct, exactly as they did before.
        """
        if not unnamed_projects(records, self.catalogue):
            # Cleared rather than left standing, so a project that has just become known does
            # not spend the rest of the interval it landed in unable to try again.
            self._catalogue_gap_read_at = None
            return
        now = monotonic()
        if (
            self._catalogue_gap_read_at is not None
            and now - self._catalogue_gap_read_at < CATALOGUE_GAP_RETRY_SECONDS
        ):
            return
        # Stamped before the await rather than after: a bound raised past the suspension point
        # does not bind. `concurrent_updates(False)` means two presses cannot interleave here
        # today, but this rests on that fact rather than restating it.
        self._catalogue_gap_read_at = now
        await self._reread_catalogue()

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
            update.effective_message, _reply_arguments(await self.sessions._sessions_reply())
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
        await self.view.render(bot, arguments, screen=self._take_drawing_screen())
        await self._release_attachment(bot, None)
        await self._release_pairing_code(bot)
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
                    self.launch_resume._projects_reply(
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
                bot,
                entry,
                message,
                _reply_arguments(self.launch_resume._project_review_reply(identity)),
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
                    bot,
                    entry,
                    message,
                    _reply_arguments(await self.sessions._detail_reply(entry.entity_id)),
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
                        await self.sessions._sessions_reply(
                            notice="That session is no longer available."
                        )
                    ),
                )
                return
            await self._finish_entry(
                bot,
                entry,
                message,
                _reply_arguments(await self.sessions._detail_reply(entry.entity_id)),
            )
            return
        if entry.action == "session.message":
            # The relay decides send, queue or refuse from a fresh look at the pane (DEC-099);
            # this surface only says what it decided. A token drawn before a restart that
            # dropped the relay still opens this step, so its absence is answered, not raised.
            if self.message_relay is None:
                await self._finish_entry(
                    bot,
                    entry,
                    message,
                    _reply_arguments(self._message("Sending messages is unavailable.")),
                )
                return
            try:
                result = await self.message_relay.submit(SessionId.parse(entry.entity_id), value)
            except Exception:
                # Raising here would leave the box open and every later reply raising again --
                # Stage 2's Critical, for rename. Nothing can say whether the text reached the
                # pane (a failed queue write after a send would look the same), so the answer
                # sends the owner to look rather than claiming either way.
                _LOG.exception("relaying a message to %s failed", entry.entity_id)
                await self._finish_entry(
                    bot,
                    entry,
                    message,
                    _reply_arguments(
                        self._message(
                            "Something failed while sending. Check the session before sending "
                            "it again.",
                            back=self._callback("session.detail", entry.entity_id),
                            back_label="Back to session",
                        )
                    ),
                )
                return
            await self._finish_entry(
                bot,
                entry,
                message,
                _reply_arguments(await self.sessions._relay_reply(entry.entity_id, result)),
            )
            return
        if entry.action in {"schedule.time", "schedule.message"}:
            await self._schedule_text(bot, entry, message, value)
            return
        # Every remaining text step returns above. A step that reaches here is one whose
        # action was added to `_TEXT_ENTRY_ACTIONS` without a branch to answer it, which would
        # otherwise consume the owner's reply and draw nothing.
        raise AssertionError(f"no text handler for {entry.action!r}")

    async def _schedule_text(self, bot, entry: _TextEntry, message, value: str) -> None:
        result = self.scheduling.accept_text(entry.action, entry.entity_id, value)
        if isinstance(result, str):
            await self._ask_again(bot, entry, message, result)
        else:
            await self._finish_entry(bot, entry, message, _reply_arguments(result))

    @property
    def _entry_key(self) -> tuple[int, int]:
        return (self.owner_user_id, self.owner_chat_id)

    async def _entry_landing(self, entry: _TextEntry) -> dict[str, object]:
        """Where abandoning a guided step puts the owner: the screen that opened it.

        One place rather than four call sites, because "Cancel" and "Back" arrive by typed
        text and by button and must agree about where they go.
        """
        if entry.action in {"session.rename", "session.message"}:
            return _reply_arguments(await self.sessions._detail_reply(entry.entity_id))
        if entry.action in {"schedule.time", "schedule.message"}:
            screen = self.scheduling.text_landing(entry.action, entry.entity_id)
            return _reply_arguments(
                screen or self.launch_resume._projects_reply(self.catalogue, view_id="all")
            )
        if entry.action == "project.name":
            # `project.name` is what the *step* is called; `project.area` is the button that
            # opens it. Abandoning the name returns to the area picker that asked for it.
            return _reply_arguments(await self.launch_resume._project_areas_reply())
        flow = _SEARCH_ACTIONS.get(entry.action, "launch")
        if flow == "resume":
            return _reply_arguments(self.launch_resume._resume_projects_reply())
        return _reply_arguments(self.launch_resume._projects_reply(self.catalogue, view_id="all"))

    async def _finish_entry(self, bot, entry: _TextEntry, message, arguments) -> None:
        """Draw the answer, then take the question and the answer out of the chat.

        Render first for the same reason a command does: if the screen cannot be drawn, the
        owner keeps what they typed and can see that nothing came of it.
        """
        await self.view.render(bot, arguments, screen=self._take_drawing_screen())
        self._awaiting_text.pop(self._entry_key, None)
        await self._release_attachment(bot, None)
        await self._release_pairing_code(bot)
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
                _reply_arguments(self.launch_resume._projects_reply(self.catalogue, view_id="all")),
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
                update.effective_message,
                _reply_arguments(self.launch_resume._resume_projects_reply()),
            )

    async def sessions_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if self.permits(update) and update.effective_message is not None:
            self._flow = "sessions"
            await self._answer_command(
                update.effective_message, _reply_arguments(await self.sessions._sessions_reply())
            )

    async def remote_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Open machine-wide Remote Control, a child of Settings."""
        del context
        if self.permits(update) and update.effective_message is not None:
            self._flow = "settings"
            await self._answer_command(
                update.effective_message,
                _reply_arguments(await self.settings._host_remote_control_reply()),
            )

    def _mark_screen(self, screen: str) -> None:
        self._drawing_screen = screen

    @property
    def sessions(self) -> SessionScreens:
        return SessionScreens(
            self.backend,
            self.callbacks,
            self.owner_user_id,
            self.owner_chat_id,
            self._message,
            self._callback,
            list_state=self._session_list_state,
            mark_screen=self._mark_screen,
            _host_remote_block=self._host_remote_block,
            _claude_default_block=self._claude_default_block,
            _inspection_result=self._inspection_result,
            _listed_records=self._listed_records,
            _record=self._record,
            _session_name=self._session_name,
            glyphs=self.glyphs,
            profiles=self.profiles,
            trust_dialogs=self.trust_dialogs,
            message_relay=self.message_relay,
            relayable=self.relayable,
            session_page_size=self.session_page_size,
            stops=self.stops,
        )

    @property
    def launch_resume(self) -> LaunchResumeScreens:
        return LaunchResumeScreens(
            self.backend,
            self.callbacks,
            self.owner_user_id,
            self.owner_chat_id,
            self._message,
            self._callback,
            _project_views=self._project_views,
            get_catalogue=lambda: self.catalogue,
            profiles=self.profiles,
            project_page_size=self.project_page_size,
            refresh_catalogue=self.refresh_catalogue,
            trust_dialogs=self.trust_dialogs,
            trust_notifier=self.trust_notifier,
            _provider_name=self._provider_name,
            permission_question=self.settings._permission_question,
        )

    @property
    def scheduling(self) -> ScheduleScreens:
        return ScheduleScreens(
            self.backend,
            self.callbacks,
            self.owner_user_id,
            self.owner_chat_id,
            self._message,
            self._callback,
            state=self._schedule_state,
            mark_screen=self._mark_screen,
            catalogue=self.catalogue,
            profiles=self.profiles,
            _provider_name=self._provider_name,
        )

    @property
    def settings(self) -> SettingsScreens:
        return SettingsScreens(
            self.backend,
            self.callbacks,
            self.owner_user_id,
            self.owner_chat_id,
            self._message,
            self._callback,
        )

    async def settings_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Open the Settings destination."""
        del context
        if self.permits(update) and update.effective_message is not None:
            self._flow = "settings"
            await self._answer_command(
                update.effective_message, _reply_arguments(self.settings.root())
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
            "<b>Launch</b> (/launch) starts a curated agent in a project.",
        ]
        if self.backend.conversations is not None:
            lines.append("<b>Resume</b> (/resume) continues a saved conversation in a new session.")
        lines.append(
            "<b>Sessions</b> (/sessions) lists what is running. Open one to read its output, "
            "copy an attach command, rename it, or stop it."
        )
        if self.backend.projects is not None:
            lines.append("<b>Add Project</b> registers a new project to launch into.")
        if self.backend.host_remote_control is not None:
            lines.append(
                f"<b>{escape(HOST_REMOTE_CONTROL_TITLE)}</b> (/remote) shows enrollment status "
                "and turns the relay connection on or off for the whole machine."
            )
        sections = [title for _, title, available in available_sections(self.backend) if available]
        choices = ", ".join(sections) if sections else "No editable settings on this machine"
        lines.append(f"<b>Settings</b> (/settings): {choices}.")
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
        # Held for the whole handler, released in `finally`, so a store change arriving
        # mid-press cannot edit the screen this press is about to draw. `_handling_press`
        # records why a plain flag is enough here.
        self._handling_press = True
        try:
            await self._handle_callback(update)
        finally:
            self._handling_press = False

    async def _handle_callback(self, update: Update) -> None:
        """The press itself, with `callback` holding the flag around it."""
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
        # Trust answers appear on the menu as well as on independently sent questions.
        # Older amended questions carry an ordinary session.detail token. The stored chat
        # and message identify both generations without granting a stale menu an exemption.
        apart = state is not None and state.action == _NOTIFIED_DETAIL
        if (
            state is not None
            and state.action in _TRUST_MESSAGE_ACTIONS
            and self.trust_notifier is not None
            and message_id != self.view.anchor()
        ):
            question = await self.trust_notifier.question_for_message(state.entity_id, message_id)
            if question is not None:
                apart = True
                if question.settled != (state.action == "session.detail"):
                    state = None
        if state is not None and not apart:
            self.view.adopt(message_id)
            if self.view.anchor() != message_id:
                state = None
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
            anchor = self.view.anchor()
            if anchor is not None and (anchor != message_id or self.view.revision):
                return
            self._flow = "sessions"
            await self._render(query, _reply_arguments(await self.sessions._sessions_reply()))
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
            await self._release_pairing_code(query.get_bot())
            if state.action not in _TEXT_ENTRY_ACTIONS:
                await self._abandon_entry(query.get_bot())
            if state.action == "session.inspect":
                await self._send_inspection(query, state.entity_id)
                return
            # Beside `session.inspect` and for the same reason: this one cannot be an *edit*
            # of the live view. `protect_content` is a parameter of `sendMessage` and not of
            # `editMessageText`, so routing the code through `_render` raised
            # `TypeError: ExtBot.edit_message_text() got an unexpected keyword argument
            # 'protect_content'` on every press and no code ever reached the owner. Found in
            # production on 2026-09-04, by the owner pressing the button.
            if state.action == "host.remote.pair":
                await self._send_pairing_code(query, token=query.data or "", message_id=message_id)
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
            # A screen whose whole content is a secret is drawn but never remembered, so the
            # live view cannot re-send it later on its own. `move_to_bottom` fires once per
            # activity-notification pass and re-sends whatever was last remembered -- which,
            # for the pairing reply, meant sending the code again as a brand new message with
            # its own push notification, under a line that says "shown once". Reproduced
            # before this branch existed.
            await self._render(
                query,
                await self._reply_for(
                    state.action,
                    state.entity_id,
                    token=query.data or "",
                    message_id=message_id,
                ),
                remember=state.action not in _UNREMEMBERED_ACTIONS,
            )
            if notified:
                await self.notifier.dismiss(query.get_bot(), state.entity_id, message_id)
            if state.action in _SESSION_ENDING_ACTIONS:
                await self.notifier.retire_finished()
                if self.trust_notifier is not None:
                    try:
                        await self.trust_notifier.retire_if_finished(state.entity_id)
                    except Exception:
                        _LOG.exception("deferring cleanup of a stopped session’s trust question")
        except Exception:
            if pending is None:
                raise
            # The pending screen carries no buttons, so failing after it is drawn would
            # strand the owner on a dead message. Put them back on something they can act on.
            _LOG.exception("callback action failed while its pending notice was on screen")
            self._flow = "sessions"
            await self._render(
                query,
                _reply_arguments(
                    await self.sessions._sessions_reply(
                        notice="That action did not complete, and the session was left as it is."
                    )
                ),
            )

    async def _render(
        self,
        query,
        arguments: dict[str, object],
        *,
        retire: bool = True,
        remember: bool = True,
    ) -> None:
        """Draw a screen into this chat's live view, whichever message that currently is.

        Addressed by anchor rather than by the message the press came from. Those are the
        same message in the ordinary case, and deliberately not the same one after a re-send
        — the chat has one screen, so a render's target is a property of the chat rather
        than of whatever update happened to trigger it.

        `LiveView` owns the edit-then-prune-then-bind order and the no-op guard; what is
        left here is telling it which bot to speak through.
        """
        await self.view.render(
            query.get_bot(),
            arguments,
            retire=retire,
            remember=remember,
            screen=self._take_drawing_screen(),
        )

    async def _reply_for(
        self, action: str, entity_id: str, *, token: str = "", message_id: int = 0
    ) -> dict[str, object]:
        if action in {"nav.home", "nav.refresh"}:
            self._flow = "sessions"
            return _reply_arguments(await self.sessions._sessions_reply())
        if action.startswith("schedules."):
            return await self.scheduling._schedules_action_reply(
                action, entity_id, token, message_id
            )
        if action.startswith("sched."):
            return await self.scheduling._schedule_step_reply(action, entity_id, token, message_id)
        flow = _flow_of(action)
        if flow == "sessions":
            return await self.sessions.reply_for(action, entity_id, token, message_id)
        if flow in {"resume", "launch"}:
            return await self.launch_resume.reply_for(action, entity_id, token, message_id)
        if flow == "settings":
            return await self.settings.reply_for(action, entity_id, token, message_id)
        return _reply_arguments(self._message("That action is no longer available."))

    #: The tag `LiveView` remembers for the sessions list, and the only screen a store change
    #: is allowed to redraw. A constant rather than a literal in three places because the
    #: whole mechanism turns on the writer and the reader agreeing on one string.
    _SESSIONS_SCREEN = "sessions"

    #: The floor between two store-driven edits of the sessions page.
    _REDRAW_INTERVAL_SECONDS = 2.0

    #: How stale the page may get while nothing changes. The rows carry an age and the block
    #: below them a limits sweep, so a page that is only ever drawn on a change would sit
    #: showing "2m" an hour later. This is the ceiling on that, not a repaint timer: a change
    #: still draws immediately.
    _REFRESH_INTERVAL_SECONDS = 60.0

    @staticmethod
    def _ban_seconds(error: RetryAfter) -> float:
        """Read `retry_after` as seconds, whichever type this python-telegram-bot hands back.

        It is an `int` today and the library has announced it becomes a `timedelta`
        (`PTBDeprecationWarning`, opt-in now via `PTB_TIMEDELTA`). A float() of a timedelta
        raises, so the version that flips would turn every flood ban into a `TypeError`
        inside the handler that exists to absorb it -- and the retry loop would be back.
        """
        retry_after = error.retry_after
        total_seconds = getattr(retry_after, "total_seconds", None)
        return float(total_seconds()) if callable(total_seconds) else float(retry_after)

    @staticmethod
    def _page_key(records: tuple[SessionRecord, ...], page: int) -> tuple[object, ...]:
        """What this page is *about*, cheaply and without minting anything.

        Deliberately built from the records rather than from the rendered message. Rendering
        mints a fresh callback token per button, and those rows are written to the database
        the store watcher fingerprints -- so asking "would this draw differ?" by drawing it
        is what made the question self-answering: every redraw wrote, every write published a
        change, and the change scheduled the next redraw. Thirty real edits a minute to one
        private chat with nothing happening, until Telegram stopped the bot.
        """
        return (
            page,
            tuple(
                (
                    str(r.session_id),
                    str(r.state),
                    str(r.display),
                    str(r.terminal_reason),
                    str(r.remote_control_state),
                )
                for r in records
            ),
        )

    def _hold_off(self, seconds: float) -> None:
        """Refuse sends for `seconds`, because Telegram said so.

        Two effects, because two mechanisms have to learn it. `flood` is the chat-wide answer
        every sender consults -- the activity notifier reads it before a pass, so a ban found
        by a button press stops the notifier too. `_redraw_allowed_at` is the redraw's own
        floor, pushed out so the store-driven loop cannot walk past the ban two seconds at a
        time, which is the behaviour that turned a ten-second cooldown into six hours.
        """
        self.flood.hold_off(seconds)
        until = monotonic() + max(seconds, 0.0)
        if until > self._redraw_allowed_at:
            self._redraw_allowed_at = until

    async def on_error(self, update: object, context: object) -> None:
        """Answer an exception no handler caught, and never let one reach the poll loop.

        Registered because there was no error handler at all: python-telegram-bot logged
        "No error handlers are registered, logging exception" and every failure surfaced as a
        raw traceback in the journal, including the flood-control ban that made the bot
        unusable for six hours.

        A `RetryAfter` reaching here came from a command or a button, not from the redraw --
        but the ban it reports is the *chat's*, not that one call's, so the redraw is held off
        too. Otherwise the loop keeps editing straight through a ban a press has already
        discovered, which is the behaviour this fix exists to remove.
        """
        error = getattr(context, "error", None)
        if isinstance(error, RetryAfter):
            seconds = self._ban_seconds(error)
            self._hold_off(seconds)
            _LOG.warning("telegram flood control: holding sends to this chat for %ss", seconds)
            return
        _LOG.error("unhandled error while processing a Telegram update", exc_info=error)

    def _defer_settle(self, bot: Bot, delay: float) -> None:
        """Arrange for one attempt to pay the debt once the floor lifts. At most one pending.

        At most one because the debt is a single bit: a second suppressed change inside the
        same window is the same debt, and a task per change would edit once per change the
        moment the floor lifted -- which is the burst the floor exists to prevent.
        """
        pending = self._settle_task
        if pending is not None and not pending.done():
            return

        async def pay_when_allowed() -> None:
            try:
                await asyncio.sleep(max(delay, 0.0))
                await self.settle_owed_redraw(bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Nobody asked for this edit, so its failure is not the owner's to see. The
                # debt stays owed and the next store change tries again.
                _LOG.warning("an owed sessions redraw could not be paid", exc_info=True)

        self._settle_task = asyncio.create_task(pay_when_allowed())

    def cancel_pending_redraw(self) -> None:
        """Drop any pending settle, so a stopping process leaves no task behind. Idempotent."""
        pending, self._settle_task = self._settle_task, None
        if pending is not None and not pending.done():
            pending.cancel()

    async def settle_owed_redraw(self, bot: Bot | None = None) -> bool:
        """Draw a redraw the floor suppressed, if one is owed and the floor has lifted.

        The other half of `redraw_sessions_if_open`'s coalescing. Called by whoever is
        watching the store -- one attempt per poll, which is cheap because it is a flag test
        until there is something to do.

        Called from two places, and it needs both. The store-change listener asks after a
        refused redraw, which covers a busy store cheaply; `_defer_settle`'s one-shot task
        covers the case that listener structurally cannot -- a store that goes quiet straight
        after the change it refused.
        """
        if not self._redraw_owed:
            return False
        if monotonic() < self._redraw_allowed_at:
            return False
        self._redraw_owed = False
        return await self.redraw_sessions_if_open(bot)

    def _take_drawing_screen(self) -> str | None:
        """Read the pending screen tag and clear it, so it marks exactly one render."""
        tag, self._drawing_screen = self._drawing_screen, None
        return tag

    def attach_bot(self, bot: Bot) -> None:
        """Hand it the application's Telegram handle, which arrives long after construction."""
        self._bot = bot

    async def _redraw_trust_anchor(self, bot: Bot, message_id: int, revision: int) -> bool:
        if self._handling_press or self.flood.held():
            return False
        # Render the Sessions tab without changing an overlapping user action's navigation.
        sessions = replace(self.sessions, _message=partial(self._message, flow="sessions"))
        arguments = _reply_arguments(await sessions._sessions_reply(mark=False))
        if self._handling_press:
            return False
        try:
            drawn = await self.view.render(
                bot,
                arguments,
                screen=self._SESSIONS_SCREEN,
                expected_anchor=message_id,
                expected_revision=revision,
            )
        except RetryAfter as error:
            self._hold_off(self._ban_seconds(error))
            return False
        if drawn is not None:
            self._flow = "sessions"
        return drawn is not None

    async def redraw_sessions_if_open(self, bot: Bot | None = None) -> bool:
        """Redraw the sessions page in place, if that is what the owner is looking at.

        The entry point a `StoreChanged` reaches. Answers whether it drew, so a caller can
        say so and a test can assert the decision rather than infer it from Telegram.

        **Three refusals, and each is a different screen this must not touch.** It draws only
        when `LiveView` says the anchor is the sessions list -- a detail, a confirmation or a
        one-time secret is not a list and must not be replaced by one. It draws at most once
        per `_REDRAW_INTERVAL_SECONDS`, because a burst of launches writes the store faster
        than Telegram will accept edits. And it draws nothing at all while a press is being
        handled: that press is about to render a screen of its own, and editing underneath it
        would put a list over an answer the owner asked for.

        `_sessions_reply` is re-read rather than `_last_arguments` re-sent. Those are
        different things and the difference is the whole point: `move_to_bottom` re-sends what
        was drawn, which would put the *old* rows back at the bottom of the chat. This asks
        the store what the page says now, at the page number the owner is on.
        """
        speaker = bot if bot is not None else self._bot
        if speaker is None:
            return False
        if not self.view.showing(self._SESSIONS_SCREEN):
            return False
        if self._handling_press:
            return False
        now = monotonic()
        if now < self._redraw_allowed_at:
            # Owed, not dropped. The floor may delay an edit; it may not discard one -- and
            # this page has no fallback timer behind it the way both terminal screens do, so a
            # change landing inside the window with no further write behind it would leave the
            # page stale until the owner navigated away and back.
            self._redraw_owed = True
            self._defer_settle(speaker, self._redraw_allowed_at - now)
            return False
        self._redraw_allowed_at = now + self._REDRAW_INTERVAL_SECONDS
        self._redraw_owed = False
        # `mark=False`: this caller already knows what it built, so it needs no mark -- and a
        # mark set here is *shared mutable state set across an await*. Updates are serialized
        # (`concurrent_updates(False)`), but this redraw runs on its own task, so a command
        # handler rendering while this one reads would consume the `sessions` mark and tag its
        # own screen with it. Found by the test below it, not by reasoning.
        revision = self.view.revision
        records = await self._listed_records()
        key = self._page_key(records, self._session_list_state.page)
        stale = monotonic() - self._last_drawn_at >= self._REFRESH_INTERVAL_SECONDS
        if key == self._last_drawn_key and not stale:
            # Nothing this page shows has changed, so drawing it would mint a new keyboard,
            # write those tokens, and publish the very store change that scheduled this
            # redraw. That loop is the defect: it ran at the floor for eighteen hours with an
            # idle store behind it. Refusing here is what breaks it -- and `_redraw_owed` is
            # left alone, because nothing is owed when there is nothing to say.
            return False
        rendered = await self.sessions._sessions_reply(
            self._session_list_state.page, mark=False, records=records
        )
        # **Both guards again, after the read.** Assembling this page is not quick -- a tmux
        # capture per unready row, an account-wide limits sweep, a Remote Control read, a
        # context read per row -- and every one of those is a suspension point. A press
        # arriving in that window renders the screen the owner asked for and clears its own
        # flag again, all before this resumes; an edit landing afterwards replaces their
        # screen with a list and prunes its tokens. Checking at the top decided whether to
        # *start*; these decide whether it is still true that we may finish.
        if self._handling_press or not self.view.showing(self._SESSIONS_SCREEN):
            self._redraw_owed = True
            self._defer_settle(speaker, self._REDRAW_INTERVAL_SECONDS)
            return False
        # `_take_drawing_screen`, not the constant. `_sessions_reply` marks, and a render that
        # passed the tag literally left that mark standing -- so the next unrelated screen the
        # owner opened consumed it, `LiveView` believed a help screen was the list, and the
        # following store change overwrote what they were reading. One producer, one consumer,
        # including here.
        try:
            message_id = await self.view.render(
                speaker,
                _reply_arguments(rendered),
                screen=self._SESSIONS_SCREEN,
                expected_revision=revision,
            )
            if message_id is None:
                return False
            self._last_drawn_key = key
            self._last_drawn_at = monotonic()
        except RetryAfter as error:
            # Telegram's own answer to "when may I speak again", and the only floor that
            # matters once it has said it. `_REDRAW_INTERVAL_SECONDS` is this bot's own
            # courtesy limit; it is not a permission, and treating it as one is what turned a
            # ten-second cooldown into a six-hour outage on 2026-09-13: the redraw retried
            # every two seconds for eighteen hours while Telegram answered each attempt with a
            # larger ban, because nothing here ever read `retry_after`. The debt stays owed, so
            # the page is redrawn once the hold lifts rather than left stale.
            self._hold_off(self._ban_seconds(error))
            self._redraw_owed = True
            self._defer_settle(speaker, self._redraw_allowed_at - monotonic())
            return False
        return True

    _SCHEDULES_SCREEN = "schedules"

    async def redraw_schedules_if_open(self, bot: Bot | None = None) -> bool:
        """Redraw the schedules list in place when the store moved and it is on screen.

        The sessions page's guards, cut to what this list needs: never over another screen,
        never under a press, and never an edit that would say what the list already says --
        drawing mints tokens, and a write the watcher saw would redraw again."""
        speaker = bot if bot is not None else self._bot
        if speaker is None or self.backend.schedules is None:
            return False
        if self._handling_press or not self.view.showing(self._SCHEDULES_SCREEN):
            return False
        revision = self.view.revision
        book = self.backend.schedules
        lines = schedule_lines(await book.list(), self.catalogue, book.zone, book.now())
        if tuple(lines) == self._schedule_state.last_lines:
            return False
        rendered = await self.scheduling._schedules_reply(mark=False, lines=lines)
        if self._handling_press or not self.view.showing(self._SCHEDULES_SCREEN):
            return False
        message_id = await self.view.render(
            speaker,
            _reply_arguments(rendered),
            screen=self._SCHEDULES_SCREEN,
            expected_revision=revision,
        )
        if message_id is None:
            return False
        # Only now: a redraw that stood down for a press, or whose edit failed, is still owed.
        self._schedule_state.last_lines = tuple(lines)
        return True

    async def _session_name(self, session_value: str) -> str:
        record = await self._record(session_value)
        if record is None:
            return "the session"
        return f"<b>{escape(session_row_parts(record).identity)}</b>"

    async def announce_relayed(self, session_value: str, result: RelayResult) -> None:
        """Tell the owner what became of a waiting message once a "finished" event tried it.

        Sent apart, like the limit-reset notices: it is news about a session, not a screen,
        so it must not replace whatever the owner is looking at. A message still waiting is
        not news -- the owner was told it was queued -- so nothing is said about it.
        """
        if self._bot is None or result.outcome is RelayOutcome.QUEUED:
            return
        name = await self._session_name(session_value)
        why = _RELAY_REASON_WORDS.get(result.reason, "") if result.reason else ""
        late = (
            " It was already being typed in when you cancelled or replaced it."
            if result.overtaken
            else ""
        )
        if result.outcome is RelayOutcome.SENT:
            text = f"{_MESSAGE_EMOJI} Sent queued message to {name}.{late}"
        elif result.outcome is RelayOutcome.UNCONFIRMED:
            text = (
                f"{_MESSAGE_EMOJI} Typed queued message into {name}, but not confirmed — {why}. "
                f"The text may still be in the agent's input.{late}"
            )
        elif why:
            text = f"{_MESSAGE_EMOJI} Dropped queued message for {name} — {why}."
        else:
            # The service's own report of a retry that raised after the message was gone
            # (BL-107). The retry settles a message that *may* have been typed, so "dropped"
            # would be a claim nobody can make -- and one that invites a re-send, which types
            # it twice (DEC-099).
            text = (
                f"{_MESSAGE_EMOJI} Queued message for {name} failed partway through delivery, "
                "so it may have been typed. Check the session before sending it again."
            )
        self.held_relay_notices.append(text)
        if len(self.held_relay_notices) > _HELD_RELAY_NOTICES:
            del self.held_relay_notices[0]
            _LOG.warning("a held queued-message notice was dropped: too many were held")
        await self.flush_relay_notices()

    async def flush_relay_notices(self) -> None:
        """Send every held notice, oldest first, unless the flood hold still stands.

        A notice leaves the list only once it is sent; a send that fails keeps it, and the rest,
        for the next flush. The service calls this on every activity pass, so a notice held by
        the flood is sent once the hold runs out even when no new notice follows it.
        """
        if self._bot is None:
            return
        while self.held_relay_notices and not self.flood.held():
            # Taken off the list before the send, not after: the service pass and a bot handler
            # can flush on the same loop, and a notice read in place could be sent by both.
            text = self.held_relay_notices.pop(0)
            try:
                await self.view.send_apart(self._bot, {"text": text, "parse_mode": ParseMode.HTML})
            except Exception:
                _LOG.exception("sending a queued-message notice failed; it is kept for later")
                self.held_relay_notices.insert(0, text)
                return

    async def _send_pairing_code(self, query, *, token: str, message_id: int) -> None:
        """Mint one pairing code and send it once, with no keyboard under it.

        **Sent apart from the live view, never edited into it.** Two independent reasons, and
        the first one is why no code ever reached the owner until 2026-09-04: `protect_content`
        exists on `sendMessage` and not on `editMessageText`, so an edit carrying it raises
        `TypeError` before anything is delivered. The second reason outlives the first --
        editing the secret into the anchor would put it in the one message this bot keeps
        rewriting, so the next screen would silently overwrite the code the owner was still
        reading, and every earlier screen would have carried it in its edit history.

        The live view is still redrawn, with the ordinary host screen: the owner pressed a
        button and something must acknowledge it. The code follows as its own terminal
        message, which lands at the bottom of the chat where it is read.

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
            await self._render(query, _reply_arguments(self.settings._host_unavailable()))
            return
        # Re-read before minting, exactly as the terminal does. The button was drawn against
        # a reading that may be a screen old, and a link that has dropped since would mint a
        # live code that pairs nothing -- which reads to an owner as a broken feature rather
        # than as an action that was never available. Checked before the claim, so a press
        # that could never have worked does not spend its one-shot.
        status = await control.status()
        if not pair_available(status):
            await self._render(query, _reply_arguments(self.settings._host_remote_screen(status)))
            return
        if not self.callbacks.claim_mutation(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            message_id=message_id,
        ):
            await self._render(
                query, _reply_arguments(self._message("That action has already run."))
            )
            return
        try:
            code = await control.pair(PairCommand(token))
        except Exception as error:
            # The type only -- see the sibling in `adapters/tui/app.py` for why `exception`
            # would put a still-live pairing code into this process's logs.
            _LOG.error("a pairing code could not be minted: %s", type(error).__name__)
            await self._render(
                query,
                _reply_arguments(
                    self._message(
                        f"No {escape(HOST_REMOTE_CONTROL_TITLE)} pairing code reached you. If "
                        "one was minted before the failure it expires on its own; nothing "
                        "about this machine's setting changed."
                    )
                ),
            )
            return
        expires = _expiry_phrase(code.expires_at)
        # `render_message` rather than `self._message`, which is the fourth screen in this
        # class to skip the navigation bar and the first to do so for a reason of its own.
        # The bar is a keyboard, and a keyboard under a message whose entire content is a
        # secret is a control that can re-send it -- into the same chat, possibly long after
        # the owner stopped looking. The two permanent exceptions above skip the bar so a
        # wait cannot be pressed into a second launch; this one skips it so a secret cannot
        # be pressed into a second copy.
        arguments = _reply_arguments(
            render_message(
                f"<b>{escape(HOST_REMOTE_CONTROL_TITLE)} pairing code</b>\n"
                f"<code>{escape(code.code)}</code>\n\n"
                "Anyone who types this into ChatGPT gets control of this machine: it can "
                "read the files in every project a session is open in, and run commands "
                "there. Do not forward it.\n"
                f"{escape(expires)}. A phone that pairs keeps its access after that — turn "
                f"{escape(HOST_REMOTE_CONTROL_TITLE)} off to end it.\n"
                "<b>This message removes itself</b> the next time you use this bot. Until "
                "then Telegram keeps it here and on every device signed into this account, "
                "so delete it yourself if you are stepping away."
            ),
            # Marked unforwardable like a captured pane, and for a stronger reason than the
            # one that earned it there: a pane carries whatever an agent printed, and this
            # carries a key to the machine. `protect_content` is a `sendMessage` parameter,
            # which is the other half of why this goes out through `send_apart`.
            protect_content=True,
        )
        # One tap to the clipboard. `copy_text` is a *client-side* button: pressing it copies
        # and sends nothing to this bot, which is what makes it admissible under the rule that
        # no control under this message may re-send it. It is not a callback, so it needs no
        # token -- which matters, because `send_apart` deliberately binds none.
        #
        # The code is thereby in the message twice, in the text and in the markup. That is not
        # a new exposure: same message, same chat, same lifetime, and the owner could always
        # select the text by hand. What it buys is not fumbling a hyphenated code on a phone.
        arguments["reply_markup"] = InlineKeyboardMarkup(
            ((InlineKeyboardButton("Copy code", copy_text=CopyTextButton(code.code)),),)
        )
        # The acknowledgement first, so the code is the last thing in the chat -- it is what
        # the owner is looking for, and a screen drawn after it would push it up.
        await self._render(query, _reply_arguments(self.settings._host_remote_screen(status)))
        # Tracked so it can take itself out of the chat again. A pairing code is read once and
        # then it is a live secret sitting in a chat history, so it is released on the owner's
        # next interaction the way a captured document is released when its session leaves the
        # screen -- the same mechanism, for a stronger reason.
        self._pairing_message = await self.view.send_apart(query.get_bot(), arguments)

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
        reading = self.settings._host_reading(status)
        return f"\n\n<code>{escape(HOST_REMOTE_CONTROL_TITLE)} · {reading}</code>"

    async def _claude_default_block(self) -> str:
        """Claude's stored Remote Control default for the sessions list, or nothing at all.

        **A block of its own rather than a line inside either neighbour**, because it answers to
        neither of them. Folded into `_limit_block` it would render *above* the Codex reading
        and vanish whenever an agent had no window to report, which is the routine state that
        block already returns nothing for; folded into `_host_remote_block` it would vanish
        whenever this machine wired no Codex daemon. They are three independent capabilities
        and the Settings screen already says so in as many words -- *each row is its own
        setting, so changing one says nothing about the others* -- so the sessions list must not
        be the one screen where one absence takes another's reading down with it.

        **The line is rendered by `remote_control_default_line` rather than spelled here.**
        `_host_remote_block` above re-spells its ` · ` join inline because its reading is
        assembled from a status object this surface decodes itself; this one is not, and a
        second spelling of a string the terminal's Settings row and the limits pane both draw
        is precisely the drift DEC-007 exists to end. One definition, three renderers.

        **An unwired port draws nothing, which is deliberately not what Settings does.** There
        the same absence becomes *"Claude Remote Control is unavailable."*, because Settings is
        opened to be told what this machine can do and a declared absence is the answer
        (DEC-009/DEC-061). Nobody opens the sessions list to ask that. It is the only way to
        reach a session at all, so a permanent line naming a provider this composition never
        wired would be noise on the one screen that can least afford it -- and the Codex
        reading directly above already disappears on exactly this condition, so a Claude line
        that announced itself instead would make one block answer two ways about two absences
        that mean the same thing.

        The broad `except` is `_host_remote_block`'s trade, made for the same screen and for a
        port that promises not to need it: `read` answers `PROVIDER_DEFAULT` for every way a
        settings file can be unreadable rather than raising. What it cannot promise about is a
        composition wiring something else or a read cancelled under a redraw, and neither of
        those may cost the owner the list.
        """
        default = self.backend.claude_remote_control_default
        if default is None:
            return ""
        try:
            value = await default.read()
        except Exception:
            _LOG.debug("claude remote control default read failed", exc_info=True)
            return ""
        return f"\n\n<code>{escape(remote_control_default_line(value))}</code>"

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

    async def _release_pairing_code(self, bot) -> None:
        """Take the pairing-code message out of the chat at the owner's next interaction.

        **Unconditional, unlike `_release_attachment`.** A captured document is *about* a
        session, so it stays while the owner is still looking at that session; a pairing code
        is about nothing that can still be on screen. It has one job, the owner does it in the
        seconds after it arrives, and every second after that it is a live credential sitting
        in a chat history -- on this device and every other one signed into the account.

        So the next thing the owner does with the bot takes it away. That is as close to
        "disappears once you have used it" as this medium allows: `copy_text` is handled
        entirely by the client and reports nothing back, so no bot can know a code was
        copied. The message says to delete it; this stops that being the owner's job.

        A refusal is kept rather than forgotten -- `discard` answers whether the message is
        actually gone, and dropping the id on a failure would leave a live secret in the chat
        with nothing left that could ever try again.
        """
        if self._pairing_message is None:
            return
        if await self.view.discard(bot, self._pairing_message):
            self._pairing_message = None

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

    async def _records(self) -> tuple[SessionRecord, ...]:
        """The listable records, without the readiness pass.

        Every read but the sessions list, which uses `_listed_records`. Stated that way round
        rather than as "every re-read": `_sessions_reply` is also where a stop, a rename and a
        cleanup land, so it is not only reached by an owner opening the list.
        """
        if self.backend.sessions is None:
            return ()
        return await self._named(only_listed(await self.backend.sessions.list_sessions()))

    async def _listed_records(self) -> tuple[SessionRecord, ...]:
        """What opening the sessions list reads: the readiness pass and the read, together.

        The pairing is `application/session_views.listed_sessions`, shared with the local
        surface. What stays here is the project-name decoration, which is this surface's own —
        the local one renders an area and a name from its own catalogue instead.
        """
        if self.backend.sessions is None:
            return ()
        return await self._named(await listed_sessions(self.backend.sessions))

    async def _named(self, records: tuple[SessionRecord, ...]) -> tuple[SessionRecord, ...]:
        """This surface's catalogue, joined by the shared rule.

        The join itself is `application/session_views.with_project_names` and no longer this
        adapter's: the local surface needed the same rule, and a second copy here is the
        shape BL-031 records. What stays this surface's is *which* catalogue -- the bot holds
        a ranked snapshot of its own.

        Async because a snapshot that cannot name a project is a fact this method is the first
        to know, and the funnel every named read passes through is the one place worth acting
        on it -- see `_close_catalogue_gap`. The same move `RemoteAgentsTui._with_names` makes,
        for the same defect.
        """
        await self._close_catalogue_gap(records)
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

    def _project_name(self, project_id: str) -> str:
        """The catalogue's name for a project, for a message about no session record."""
        return project_name(project_id, self.catalogue)

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
                # `with_project_names` directly rather than `_named`: this is one record on
                # the notification path, and a notification is not a screen the owner is
                # reading names off a list in. The gap close belongs to the listing reads,
                # which run when the owner is looking at a list of projects and waiting.
                (named,) = with_project_names((record,), self.catalogue)
                # The compact identity the redesign gives every two-line row, with the
                # sequence outside it -- the notification renders both on one plain line.
                return f"{session_identity(named)} #{named.display.sequence}"
        return None

    def _provider_name(self, profile_id: str) -> str:
        return self.names.get(profile_id, "Unavailable")

    def _message(
        self,
        text: str,
        keyboard: tuple[tuple[Button, ...], ...] = (),
        *,
        back: str | None = None,
        back_label: str = "Back",
        flow: str | None = None,
    ) -> RenderedMessage:
        """Append the contextual Back row, when supplied, then the global navigation."""
        rows = list(keyboard)
        if back is not None:
            rows.append((Button(back_label, back),))
        rows.append(
            navigation_row(
                flow if flow is not None else self._flow,
                can_resume=self.backend.conversations is not None,
                callback=self._callback,
            )
        )
        return render_message(text, tuple(rows))

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
    trust_store: object | None = None,
    notice_store: object | None = None,
    rollover_settled: Callable[[str], Awaitable[bool]] | None = None,
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
        else LiveView(
            chat_id=owner_chat_id, callbacks=bot.callbacks, anchors=bot.anchors, flood=bot.flood
        )
    )
    bot.notifier = (
        notifier
        if notifier is not None
        else ActivityNotifier(
            view=bot.view,
            callbacks=bot.callbacks,
            flood=bot.flood,
            owner_user_id=owner_user_id,
            display=bot._display_for,  # noqa: SLF001 -- the cycle this factory exists to pay
            standing=bot.standing,
            finished=bot._finished_sessions,  # noqa: SLF001
        )
    )
    if bot.backend.limits is not None:
        object.__setattr__(
            bot,
            "limit_reset_notifier",
            LimitResetNotifier(
                # The one argumentless read both surfaces share, handed over rather than
                # reached for: the notifier cannot choose a source, so the owner's Settings
                # choice and DEC-087's opt-in stay the only things that decide one.
                limits=bot.backend.limits,
                view=bot.view,
                # The existing naming site, passed in. The notifier is forbidden to spell a
                # provider name of its own, and a test over its AST holds that.
                name_for=bot._provider_name,  # noqa: SLF001 -- the bot's own names
                flood=bot.flood,
                # The owner's choice, read on every pass: off, or how long a message stays.
                choice=(
                    None
                    if bot.backend.early_reset_notice is None
                    else bot.backend.early_reset_notice.read
                ),
                notices=notice_store,  # type: ignore[arg-type]
            ),
        )
    if bot.backend.rollovers is not None:
        object.__setattr__(
            bot,
            "rollover_notifier",
            RolloverNotifier(
                view=bot.view,
                project_name=bot._project_name,  # noqa: SLF001 -- the cycle this factory pays
                flood=bot.flood,
                notices=notice_store,  # type: ignore[arg-type]
                settled=rollover_settled,
            ),
        )
    if bot.backend.sessions is not None:
        object.__setattr__(
            bot,
            "restart_notifier",
            RestartNotifier(
                view=bot.view,
                project_name=bot._project_name,  # noqa: SLF001 -- the cycle this factory pays
                flood=bot.flood,
            ),
        )
    if bot.backend.sessions is not None:
        object.__setattr__(
            bot,
            "binding_notifier",
            BindingNotifier(
                view=bot.view,
                finished=bot._finished_sessions,
                notices=notice_store,
                project_name=bot._project_name,  # noqa: SLF001 -- the cycle this factory closes
                flood=bot.flood,
            ),
        )
    if bot.backend.schedules is not None:
        object.__setattr__(
            bot,
            "schedule_notifier",
            ScheduleNotifier(
                view=bot.view,
                # The catalogue's own name for the project, the one every screen shows.
                project_name=bot._project_name,  # noqa: SLF001 -- the cycle this factory pays
                flood=bot.flood,
            ),
        )
    if trust_store is not None:
        object.__setattr__(
            bot,
            "trust_notifier",
            TrustNotifier(
                sessions=bot.backend.sessions,
                store=trust_store,
                view=bot.view,
                callbacks=bot.callbacks,
                owner_user_id=owner_user_id,
                trust_dialogs=bot.trust_dialogs,
                redraw_sessions=bot._redraw_trust_anchor,
            ),
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
    # Before any handler, so nothing registered below can raise into the poll loop unanswered.
    application.add_error_handler(boundary.on_error)
    application.add_handler(CommandHandler("start", boundary.start))
    application.add_handler(CommandHandler("launch", boundary.launch_command))
    application.add_handler(CommandHandler("resume", boundary.resume_command))
    application.add_handler(CommandHandler("sessions", boundary.sessions_command))
    application.add_handler(CommandHandler("help", boundary.help_command))
    # Registered on every host, while `owner_commands` *lists* it only where the capability
    # is wired: a command a bot does not handle is answered by silence, and an owner who
    # typed it from a menu their other host published deserves the sentence instead.
    application.add_handler(CommandHandler("remote", boundary.remote_command))
    application.add_handler(CommandHandler("settings", boundary.settings_command))
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
    # The same handle again, to the one screen edit that answers no update: a store change
    # redrawing the open sessions page has no press to borrow a bot from.
    boundary.attach_bot(application.bot)
    # The same handle, for the same reason, to the pass that asks the folder-trust question:
    # it answers no update either. Guarded because a boundary built by a composition that
    # wires no trust pass -- every test that constructs one directly -- has none.
    if boundary.trust_notifier is not None:
        boundary.trust_notifier.attach(application.bot)
    # And to the limits watch, for the same reason and with the same guard: it answers no
    # update either, and a boundary whose providers publish no limits has none.
    if boundary.limit_reset_notifier is not None:
        boundary.limit_reset_notifier.attach(application.bot)
    # And to the schedule notices, which answer no update either.
    if boundary.schedule_notifier is not None:
        boundary.schedule_notifier.attach(application.bot)
    # And to the rollover notices, which answer no update either.
    if boundary.rollover_notifier is not None:
        boundary.rollover_notifier.attach(application.bot)
    # And to the restart notice, held if the startup pass, which runs beside polling, finished
    # before the bot attached.
    if boundary.restart_notifier is not None:
        boundary.restart_notifier.attach(application.bot)
    # And to the unbound notices, which answer no update either.
    if boundary.binding_notifier is not None:
        boundary.binding_notifier.attach(application.bot)
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


def _expiry_phrase(expires_at) -> str:
    """When the code dies, as a duration first and a clock time second.

    A duration because that is what the owner acts on -- a pairing code lives minutes, and
    "expires at 13:00:00 BST" makes a reader do arithmetic against a wall clock. Seconds are
    dropped for the same reason: nobody acts on the second.

    The clock time is kept in the parenthetical for the case the message is read late, and it
    carries its zone abbreviation because it is the *host's* zone -- this text is read on a
    phone that may be somewhere else entirely.
    """
    from datetime import UTC, datetime

    local = expires_at.astimezone()
    remaining = expires_at - datetime.now(UTC)
    # Floored, never rounded: a deadline that overstates the time left is the one
    # direction that costs the owner something.
    minutes = int(remaining.total_seconds() // 60)
    clock = local.strftime("%H:%M %Z")
    if minutes < 1:
        return f"It has expired, or is about to (it was good until {clock})"
    unit = "minute" if minutes == 1 else "minutes"
    return f"Valid for about {minutes} more {unit}, until {clock}"


def _entry_instruction(action: str) -> str:
    return _ENTRY_INSTRUCTIONS.get(action, "Reply below with an optional session label.")
