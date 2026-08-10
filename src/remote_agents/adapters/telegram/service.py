"""Live private-bot polling boundary with exact owner/chat authorization."""

from __future__ import annotations

import asyncio
import io
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
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
from remote_agents.adapters.telegram.presenters import (
    Button,
    RenderedMessage,
    render_home,
    render_message,
)
from remote_agents.adapters.telegram.stops import CONFIRMED_FORCE, StopController
from remote_agents.adapters.telegram.wizard import ProfileAvailability
from remote_agents.application.commands import (
    InspectQuery,
    LaunchCommand,
    RemoteControlCommand,
    ResumeCommand,
)
from remote_agents.application.conversations import ConversationCatalogueQuery, ConversationService
from remote_agents.application.errors import ProjectCreationError
from remote_agents.application.project_admin import CreateProjectCommand
from remote_agents.application.project_catalog import (
    CatalogProject,
    paginate_catalogue,
    search_catalogue,
)
from remote_agents.application.relative_time import age
from remote_agents.application.session_actions import (
    ACTION_LABELS,
    FORCE,
    GRACEFUL,
    StopFailure,
    available_actions,
    explain_state,
    remote_control_available,
)
from remote_agents.config import TelegramSecrets
from remote_agents.domain.conversations import ConversationReference, ConversationState
from remote_agents.domain.models import ProfileId, ProjectId, SessionId, SessionRecord, SessionState
from remote_agents.domain.projects import ProjectIdentity
from remote_agents.domain.remote_control import RemoteControlState
from remote_agents.ports.callback_state import CallbackStatePort
from remote_agents.ports.chat_view import ChatViewPort
from remote_agents.ports.terminal import TerminalTargetMissing

_BOT_DESCRIPTION = "Private control for curated local agent sessions."
_BOT_SHORT_DESCRIPTION = "Private local agent-session control"
_LOG = logging.getLogger(__name__)
_OWNER_COMMANDS = (
    BotCommand("start", "Open the status dashboard"),
    BotCommand("launch", "Launch a curated agent"),
    BotCommand("sessions", "View managed sessions"),
    BotCommand("help", "Show available actions"),
)
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
}
_ENTRY_INSTRUCTIONS = {
    "launch.search": "Reply below with a project name.",
    "resume.search": "Reply below with a project name.",
    "project.name": "Reply below with the new project name.",
}
_SEARCH_ACTIONS = {"launch.search": "launch", "resume.search": "resume"}
_TEXT_ENTRY_ACTIONS = frozenset({"launch.search", "resume.search", "launch.label", "project.area"})
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


_PROJECT_PICKERS = {
    "launch": _ProjectPicker(
        select="launch.project",
        page="launch.page",
        search="launch.search",
        title="Projects",
        instruction="Select a project to launch.",
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
    "launch.confirm": "Launching — waiting for the agent to become ready…",
    "resume.confirm": "Resuming — waiting for the agent to become ready…",
}
"""The actions that make the owner wait, and what to show them while they do.

Each of these reaches a terminal and then polls it: a launch waits for its profile's
readiness marker, a graceful stop waits for the pane to exit, and both are bounded by the
same startup timeout — twenty seconds in the deployed composition. Everything absent from
this table answers from the store or from one tmux call, fast enough that a notice would
flash and be gone.
"""


@dataclass(slots=True)
class PrivateBotBoundary:
    """Authorize the one configured private chat before handling any supported action."""

    owner_user_id: int
    owner_chat_id: int
    catalogue: tuple[CatalogProject, ...] = ()
    profiles: tuple[ProfileAvailability, ...] = ()
    launcher: object | None = None
    conversations: ConversationService | None = None
    creator: object | None = None
    capture: Callable[[SessionId], Awaitable[str]] | None = None
    callbacks: CallbackStatePort = field(default_factory=CallbackStateStore)
    anchors: ChatViewPort = field(default_factory=ChatViewStore)
    stops: StopController = field(init=False)
    view: LiveView = field(init=False)
    _awaiting_text: dict[tuple[int, int], _TextEntry] = field(default_factory=dict)
    _labels: dict[str, str] = field(default_factory=dict)
    _attachment: tuple[str, int] | None = None
    _project_views: dict[str, tuple[CatalogProject, ...]] = field(default_factory=dict)
    project_page_size: int = 10
    session_page_size: int = 8
    catalogue_source: Callable[[], tuple[CatalogProject, ...]] | None = None

    def __post_init__(self) -> None:
        self.stops = StopController(self.callbacks)
        self.view = LiveView(
            chat_id=self.owner_chat_id, callbacks=self.callbacks, anchors=self.anchors
        )

    async def refresh_catalogue(self) -> None:
        """Re-read the projects so one created at runtime becomes selectable immediately.

        The registry read and development-root walk run off the event loop, so refreshing
        never stalls unrelated Telegram interactions or tmux polling.
        """
        if self.catalogue_source is None:
            return
        self.catalogue = await asyncio.to_thread(self.catalogue_source)
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
        """Render a fresh owner-only home view without reading command content."""
        del context
        if not self.permits(update) or update.effective_message is None:
            return
        await self._answer_command(update.effective_message, await self._home_reply())

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
            await self._finish_entry(bot, entry, message, await self._home_reply())
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
        if value.casefold() == "skip":
            self._labels.pop(entry.entity_id, None)
        else:
            try:
                self._labels[entry.entity_id] = _label(value)
            except ValueError:
                await self._ask_again(
                    bot, entry, message, "Use a visible label of up to 40 characters."
                )
                return
        await self._finish_entry(
            bot, entry, message, _reply_arguments(self._confirm_reply(entry.entity_id))
        )

    @property
    def _entry_key(self) -> tuple[int, int]:
        return (self.owner_user_id, self.owner_chat_id)

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
            await self._answer_command(
                update.effective_message,
                _reply_arguments(self._projects_reply(self.catalogue, view_id="all")),
            )

    async def sessions_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if self.permits(update) and update.effective_message is not None:
            await self._answer_command(
                update.effective_message, _reply_arguments(await self._sessions_reply())
            )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Explain the actions this deployment actually offers, and leave a way on.

        Help used to answer with two lines of plain text and no keyboard, which made it the
        one screen in the bot that went nowhere, and it named only two of the four things
        Home can offer. What is listed here is what this composition was wired with, so a
        bot without resume or project creation does not advertise them.
        """
        del context
        if not self.permits(update) or update.effective_message is None:
            return
        lines = [
            "<b>Remote agents</b>",
            "",
            "<b>Launch</b> starts a curated agent in a project.",
        ]
        if self.conversations is not None:
            lines.append("<b>Resume</b> continues a saved conversation in a new session.")
        lines.append(
            "<b>Sessions</b> lists what is running. Open one to read its output, copy an "
            "attach command, or stop it."
        )
        if self.creator is not None:
            lines.append("<b>Add Project</b> registers a new project to launch into.")
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
        # A callback query in this chat can only have come from an inline keyboard this bot
        # sent, and `permits` has already established the chat. So the message it was
        # pressed on is a screen of ours — enough to recover an anchor a composition never
        # recorded, without waiting for the token to resolve.
        #
        # It is not yet true that it is the chat's *only* screen: until Task 2.3 moves the
        # command handlers off `reply_text`, a command still adds a message. That is why
        # this only fills an absent anchor and never moves a recorded one — adopting the
        # pressed message would otherwise walk the live view backwards onto an older screen.
        self.view.adopt(message_id)
        state = self.callbacks.resolve(
            query.data or "", owner_id=owner_id, chat_id=chat_id, message_id=message_id
        )
        if state is None:
            # A press this screen cannot account for: the button belongs to a keyboard this
            # message no longer carries. Nothing expired — the token was pruned when the
            # screen that drew it was replaced — so this is a race between a thumb and a
            # redraw, not an error, and it gets a toast rather than the modal alert the
            # expiry used to raise. The words say what happened without claiming a deadline
            # that no longer exists.
            await query.answer("That screen has moved on.")
            await self._render(query, await self._home_reply())
            return
        pending = self._pending_notice(state.action)
        await query.answer(pending)
        try:
            # Whatever this press draws, it answers "is that session still on screen". Inside
            # the try, so an unexpected failure lands on the recovery screen below rather
            # than leaving a cleared spinner and nothing drawn.
            await self._release_attachment(query.get_bot(), state.entity_id)
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
        except Exception:
            if pending is None:
                raise
            # The pending screen carries no buttons, so failing after it is drawn would
            # strand the owner on a dead message. Put them back on something they can act on.
            _LOG.exception("callback action failed while its pending notice was on screen")
            await self._render(
                query,
                _reply_arguments(
                    self._message(
                        "That action did not complete, and the session was left as it is.\n"
                        "Open it again to see where it is now.",
                        back=self._callback("sessions.open", "sessions"),
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

    def _bind_sent(self, message) -> None:
        """Adopt a freshly sent screen as the live view, and hand it its tokens."""
        message_id = getattr(message, "message_id", None) if message is not None else None
        if message_id:
            self.anchors.record_anchor(self.owner_chat_id, message_id)
            self.callbacks.bind_pending(self.owner_chat_id, message_id)

    async def _home_reply(self, *, refresh: bool = False) -> dict[str, object]:
        if refresh:
            await self.refresh_catalogue()
        records = await self._records()
        return _reply_arguments(
            render_home(
                refresh=self._callback("nav.refresh", "home"),
                launch=self._callback("launch.open", "projects"),
                resume=(self._callback("resume.open", "projects") if self.conversations else None),
                sessions=self._callback("sessions.open", "sessions"),
                add_project=(self._callback("project.open", "areas") if self.creator else None),
                active=sum(record.state is SessionState.RUNNING for record in records),
                preserved=sum(record.state is SessionState.PRESERVED for record in records),
            )
        )

    async def _reply_for(
        self, action: str, entity_id: str, *, token: str = "", message_id: int = 0
    ) -> dict[str, object]:
        if action in {"nav.home", "nav.refresh"}:
            return await self._home_reply(refresh=action == "nav.refresh")
        if action == "launch.confirm":
            return await self._launch_reply(entity_id, token, message_id)
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
            return _reply_arguments(self._projects_reply(self.catalogue, view_id="all"))
        if action == "launch.page":
            return _reply_arguments(self._project_page_reply(entity_id))
        if action == "launch.project":
            return _reply_arguments(self._profiles_reply(entity_id))
        if action == "launch.profile":
            return _reply_arguments(self._confirm_reply(entity_id))
        if action == "sessions.open":
            return _reply_arguments(await self._sessions_reply())
        if action == "sessions.page":
            return _reply_arguments(await self._sessions_reply(_page_number(entity_id)))
        if action == "resume.open":
            return _reply_arguments(self._resume_projects_reply())
        if action == "resume.projects":
            return _reply_arguments(self._project_page_reply(entity_id, flow="resume"))
        if action == "resume.project":
            return _reply_arguments(await self._resume_profiles_reply(entity_id))
        if action in {"resume.profile", "resume.page"}:
            return _reply_arguments(await self._resume_catalogue_reply(entity_id))
        if action == "resume.select":
            return _reply_arguments(await self._resume_confirm_reply(entity_id))
        if action == "session.detail":
            return _reply_arguments(await self._detail_reply(entity_id, message_id))
        if action == "session.attach":
            return _reply_arguments(await self._attach_reply(entity_id))
        if action == "remote.control":
            return _reply_arguments(await self._remote_control_confirm_reply(entity_id))
        if action == "session.inspect":
            return _reply_arguments(await self._inspect_reply(entity_id))
        return _reply_arguments(self._message("That action is no longer available."))

    async def _launch_reply(self, entity_id: str, token: str, message_id: int) -> dict[str, object]:
        if self.launcher is None:
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
        record = await self.launcher.launch(
            LaunchCommand(
                ProjectId(project_id),
                ProfileId(profile_id),
                token,
                self._labels.pop(entity_id, None),
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
                    (
                        (
                            Button(
                                "Details",
                                self._callback("session.detail", str(record.session_id)),
                            ),
                        ),
                        (Button("Sessions", self._callback("sessions.open", "sessions")),),
                        (Button("Launch another", self._callback("launch.open", "projects")),),
                    ),
                )
            )
        return _reply_arguments(
            self._message(
                f"<b>Session created</b>\n{escape(record.display.rendered)}\nState: {record.state}",
                (
                    (Button("Inspect", self._callback("session.detail", str(record.session_id))),),
                    (Button("Sessions", self._callback("sessions.open", "sessions")),),
                    (Button("Launch another", self._callback("launch.open", "projects")),),
                ),
            )
        )

    async def _resume_reply(
        self, reference_value: str, token: str, message_id: int
    ) -> dict[str, object]:
        if self.launcher is None or self.conversations is None:
            return _reply_arguments(self._message("Resuming is unavailable."))
        if not self.callbacks.claim_mutation(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            message_id=message_id,
        ):
            return _reply_arguments(self._message("That action has already run."))
        resolved = await self._resolve_resume(reference_value)
        if resolved is None or resolved.summary.project_id is None:
            return _reply_arguments(self._message("That conversation is no longer available."))
        record = await self.launcher.resume(
            ResumeCommand(
                resolved.summary.project_id,
                resolved.summary.profile_id,
                resolved,
                token,
            )
        )
        if record.state is SessionState.FAILED:
            return _reply_arguments(
                self._message(
                    "<b>Resume did not become ready</b>\nOpen Sessions after local attention."
                )
            )
        return _reply_arguments(
            self._message(
                f"<b>Session resumed</b>\n{escape(record.display.rendered)}\nState: {record.state}",
                (
                    (Button("Inspect", self._callback("session.detail", str(record.session_id))),),
                    (Button("Sessions", self._callback("sessions.open", "sessions")),),
                ),
            )
        )

    async def _project_areas_reply(self) -> RenderedMessage:
        """Offer only the server-enumerated areas; a typed area never reaches the filesystem."""
        if self.creator is None:
            return self._message("Adding a project is unavailable.")
        areas = tuple(
            area
            for area in await asyncio.to_thread(self.creator.available_areas)
            if _selectable_area(area)
        )
        if not areas:
            return self._message("No area is available for a new project.")
        return self._message(
            "<b>Add project</b>\nSelect the area for the new project.",
            _button_rows(
                tuple(Button(area, self._callback("project.area", area)) for area in areas)
            )
            + ((Button("Cancel", self._callback("nav.home", "home")),),),
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
                (Button("Cancel", self._callback("nav.home", "home")),),
            ),
        )

    async def _project_reply(
        self, entity_id: str, token: str, message_id: int
    ) -> dict[str, object]:
        """Create at most once per confirmation, then re-read the catalogue off the loop."""
        if self.creator is None:
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
            created = await asyncio.to_thread(self.creator.create, CreateProjectCommand(area, name))
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
                ((Button("Launch", self._callback("launch.open", "projects")),),),
            )
        )

    async def _sessions_reply(self, page: int = 1) -> RenderedMessage:
        """Render one page of managed sessions, newest page navigation last.

        This list is unbounded in a way the project list is not — every launch adds a row
        and only reconciliation takes one away — so it pages for the same reason: a keyboard
        tall enough to push the message off the screen is unusable on a phone, and Telegram
        caps the buttons one keyboard may carry.
        """
        if self.launcher is not None:
            await self.launcher.refresh_readiness()
        records = await self._records()
        if not records:
            return self._message(
                "<b>Sessions</b>\nNothing is running.",
                ((Button("Launch", self._callback("launch.open", "projects")),),),
                refresh=self._callback("sessions.page", "1"),
            )
        page_count = max(1, ceil(len(records) / self.session_page_size))
        index = min(max(page, 1), page_count)
        start = (index - 1) * self.session_page_size
        buttons = [
            (
                Button(
                    _session_row_label(record),
                    self._callback("session.detail", str(record.session_id)),
                ),
            )
            for record in records[start : start + self.session_page_size]
        ]
        navigation = []
        if index > 1:
            navigation.append(Button("Previous", self._callback("sessions.page", str(index - 1))))
        if index < page_count:
            navigation.append(Button("Next", self._callback("sessions.page", str(index + 1))))
        if navigation:
            buttons.append(tuple(navigation))
        return self._message(
            f"<b>Sessions {index}/{page_count}</b>",
            tuple(buttons),
            refresh=self._callback("sessions.page", str(index)),
        )

    async def _detail_reply(self, session_value: str, message_id: int = 0) -> RenderedMessage:
        record = await self._record(session_value)
        if record is None:
            # Reached by opening a row that ended under the owner, so the list they came
            # from is exactly where they need to go — not Home.
            return self._message(
                "That session is no longer available.",
                back=self._callback("sessions.open", "sessions"),
            )
        buttons = [(Button("Inspect", self._callback("session.inspect", session_value)),)]
        if await self._can_copy_attach(record):
            buttons.append(
                (Button("Copy attach", self._callback("session.attach", session_value)),)
            )
        if remote_control_available(record):
            buttons.append(
                (
                    Button(
                        "Enable Remote Control",
                        self._callback("remote.control", f"{session_value}|active"),
                    ),
                )
            )
            buttons.append(
                (
                    Button(
                        "Disable Remote Control",
                        self._callback("remote.control", f"{session_value}|inactive"),
                    ),
                )
            )
        # The stops share one row while every read-only action above gets a full-width row
        # of its own. Telegram has no separator, so shape is the only signal available, and
        # the actions that end a session should not look like the ones that read it — a
        # graceful stop is one tap from discarding the pane's output. No state offers more
        # than two stops, so the row stays legible.
        stops: list[Button] = []
        for action in available_actions(record.state):
            token = self.stops.offer(
                record.session_id,
                record.profile_id,
                record.state,
                action,
                self.owner_user_id,
                self.owner_chat_id,
            )
            if token is not None:
                stops.append(Button(ACTION_LABELS[action], token))
        if stops:
            buttons.append(tuple(stops))
        return self._message(
            f"<b>{escape(record.display.rendered)}</b>\n"
            f"State: {record.state.value}\n{_state_explanation(record.state)}",
            tuple(buttons),
            back=self._callback("sessions.open", "sessions"),
        )

    async def _attach_reply(self, session_value: str) -> RenderedMessage:
        record = await self._record(session_value)
        back = self._callback("session.detail", session_value)
        if record is None or not await self._can_copy_attach(record):
            return self._message(
                "Copy Attach is unavailable until this managed pane is live.", back=back
            )
        copy = getattr(self.launcher, "copy_attach", None)
        command = await copy(record.session_id) if copy is not None else None
        if command is None:
            return self._message(
                "Copy Attach is unavailable until this managed pane is live.", back=back
            )
        return self._message(
            f"<b>Copy attach command</b>\n<code>{escape(command)}</code>", back=back
        )

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
        if self.launcher is None:
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
        result = await self.launcher.set_remote_control(
            RemoteControlCommand(SessionId.parse(session_value), state, token)
        )
        return _reply_arguments(self._message(f"Remote Control: {result.value}."))

    async def _can_copy_attach(self, record: SessionRecord) -> bool:
        if self.launcher is None:
            return False
        inspect = getattr(self.launcher, "inspect", None)
        if inspect is None:
            return False
        observation = await inspect(InspectQuery(record.session_id))
        return bool(
            observation is not None
            and observation.live
            and observation.project_id == record.project_id
            and observation.profile_id == record.profile_id
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
        if self.capture is None:
            return None
        try:
            captured = await self.capture(SessionId.parse(session_value))
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
        if request is None or self.launcher is None:
            return _reply_arguments(self._message("That action has already run."))
        record = await self._record(str(request.session_id))
        result = (
            await self.stops.execute(request, self.launcher, record) if record is not None else None
        )
        if result is None or not result.dispatched:
            return _reply_arguments(
                self._message(
                    "That session moved on before this could run, so nothing was done.\n"
                    "Open the list again to see where it is now.",
                    back=self._callback("sessions.open", "sessions"),
                )
            )
        # `request.action` rather than the pressed one: a confirmed force arrives under an
        # adapter-internal action name, and the outcome is reported in the domain's terms.
        return _reply_arguments(
            await self._stop_outcome_reply(request.action, record, result.failure)
        )

    async def _force_confirm_reply(self, token: str, message_id: int) -> RenderedMessage:
        """Name the session and the cost before offering the only irreversible button.

        Cancel comes first and on its own row. Home is not a cancel — it is a way out of
        the whole screen — and the destructive button should not be the one the thumb is
        already resting near.

        The confirming button is a **new** token carrying a different action, not the one the
        owner just pressed. Re-offering the pressed token cannot work when the screen is
        redrawn in place: the render that draws this screen prunes what the previous keyboard
        left on the message, and the re-offered token is part of exactly that set.
        """
        state = self.callbacks.resolve(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            message_id=message_id,
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
            self.owner_user_id,
            self.owner_chat_id,
        )
        if confirmed is None:
            return self._message(
                "Force stop is no longer available for this session.",
                back=self._callback("session.detail", session_value),
            )
        return self._message(
            f"<b>Force stop {escape(record.display.rendered)}?</b>\n"
            "This kills the agent immediately and cannot be undone. Anything it has not "
            "saved is lost.",
            (
                (Button("Cancel", self._callback("session.detail", session_value)),),
                (Button(ACTION_LABELS[FORCE], confirmed),),
            ),
        )

    async def _stop_outcome_reply(
        self, action: str, record: SessionRecord, failure: StopFailure | None = None
    ) -> RenderedMessage:
        """Report what the session actually did, named, rather than that a command ran.

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
        despite being ours because `stop_failure`'s fallback interpolates the raw `detail` the
        terminal adapter reported, which this module does not author.
        """
        subject = escape(record.display.rendered)
        session_value = str(record.session_id)
        current = await self._record(session_value)
        if current is not None:
            # The `else` is **unreachable today and worded as if it were not**, which is the
            # honest shape for it. `failure` is non-None for exactly the graceful action, and
            # `cleanup` and `force_stop` both walk the record to ENDED on every non-raising
            # path (`application/services.py`), so a session that is still listed after one of
            # those is a state no current code produces. The wording is deliberately neutral
            # about *why* rather than repeating the graceful-stop advice it used to carry:
            # reached at all, this branch is a session that outlived a command that claimed to
            # end it, and telling that operator to wait for a graceful exit would be a guess.
            said = (
                f"{escape(failure.summary)} {escape(failure.remedy)}"
                if failure is not None
                else (
                    "Nothing was removed and it was left as it is.\n"
                    "Open it again to see where it is now."
                )
            )
            return self._message(
                f"<b>{subject} is still running</b>\n{said}",
                ((Button("Open session", self._callback("session.detail", session_value)),),),
                back=self._callback("sessions.open", "sessions"),
            )
        if failure is not None:
            # The session has left the list, but the stop still reported that it did not take
            # effect — the other writer DEC-005 permits ended it in the window between the two.
            # Narrow, and worth not getting wrong: the whole point of threading `failure` here
            # was to stop inferring the outcome from the record, and "Stopped X" over an
            # observation that says nothing was stopped is the reading DEC-006 forbids. Found
            # by the Stage 2 gate's evaluator and its second pass independently.
            return self._message(
                f"<b>{subject} is no longer listed</b>\n"
                f"{escape(failure.summary)} {escape(failure.remedy)}",
                back=self._callback("sessions.open", "sessions"),
            )
        endings = {
            "graceful": (
                f"<b>Stopped {subject}</b>\n"
                "The session has ended. Its pane is gone, so its output is no longer there "
                "to inspect."
            ),
            "cleanup": f"<b>Cleaned up {subject}</b>\nThe session has ended and its pane is gone.",
            "force": f"<b>Force stopped {subject}</b>\nThe session has ended.",
        }
        return self._message(endings[action], back=self._callback("sessions.open", "sessions"))

    async def _records(self) -> tuple[SessionRecord, ...]:
        if self.launcher is None:
            return ()
        project_names = {project.opaque_id: project.name for project in self.catalogue}
        return tuple(
            _with_project_name(record, project_names.get(str(record.project_id)))
            for record in await self.launcher.list_sessions()
            if record.state is not SessionState.ENDED
        )

    async def _record(self, session_value: str) -> SessionRecord | None:
        return next(
            (record for record in await self._records() if str(record.session_id) == session_value),
            None,
        )

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
        buttons.append((Button("Search", self._callback(picker.search, "search")),))
        return self._message(
            f"<b>{picker.title} {rendered.page}/{rendered.page_count}</b>\n{picker.instruction}",
            tuple(buttons),
            back=self._callback("nav.home", "home"),
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
        if self.conversations is None:
            return self._message("Resume is unavailable.")
        capabilities = {
            str(item.profile_id): item for item in await self.conversations.capabilities()
        }
        buttons = []
        unavailable = []
        for profile in self.profiles:
            capability = capabilities.get(profile.profile_id)
            if (
                profile.available
                and capability is not None
                and capability.catalogue_available
                and capability.selected_resume_available
            ):
                buttons.append(
                    Button(
                        _profile_name(profile.profile_id),
                        self._callback("resume.profile", f"{project_id}|{profile.profile_id}|1"),
                    )
                )
            else:
                reason = profile.reason or (
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
        if parsed is None or self.conversations is None:
            return self._message("That conversation list is no longer open.")
        project_id, profile_id, page = parsed
        if not any(project.opaque_id == project_id for project in self.catalogue):
            return self._message("The project is no longer available.")
        try:
            result = await self.conversations.catalogue(
                ConversationCatalogueQuery(page, 10, ProfileId(profile_id), ProjectId(project_id))
            )
        except ValueError:
            return self._message("That conversation list is no longer open.")
        if result.unavailable_reason is not None:
            return self._message(f"Resume is unavailable ({escape(result.unavailable_reason)}).")
        buttons = tuple(
            (
                Button(
                    _resume_button_text(summary.description, summary.updated_at),
                    self._callback("resume.select", str(summary.reference)),
                ),
            )
            for summary in result.conversations
            if summary.state is ConversationState.RESUMABLE
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
        # Back belongs in the navigation row with Home, like every other screen, rather
        # than as a body button — and the empty case needs it most, since it used to offer
        # nothing but a row restating that there was nothing.
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

    async def _resume_confirm_reply(self, reference_value: str) -> RenderedMessage:
        resolved = await self._resolve_resume(reference_value)
        if resolved is None or resolved.summary.project_id is None:
            return self._message("That conversation is no longer available.")
        summary = resolved.summary
        if summary.state is not ConversationState.RESUMABLE:
            return self._message("That conversation cannot be resumed safely.")
        project = next(
            (item for item in self.catalogue if item.opaque_id == str(summary.project_id)), None
        )
        if project is None:
            return self._message("The project is no longer available.")
        return self._message(
            f"<b>Review resume</b>\nProject: {escape(project.name)}\n"
            f"Agent: {_profile_name(str(summary.profile_id))}\n"
            f"Last updated: {summary.updated_at:%Y-%m-%d %H:%M UTC}",
            (
                (
                    Button(
                        "Resume",
                        self._callback("resume.confirm", reference_value, mutation=True),
                    ),
                ),
                (Button("Cancel", self._callback("nav.home", "home")),),
            ),
        )

    async def _resolve_resume(self, reference_value: str):
        if self.conversations is None:
            return None
        try:
            reference = ConversationReference(reference_value)
        except ValueError:
            return None
        return await self.conversations.resolve_for_resume(reference)

    def _profiles_reply(self, project_id: str) -> RenderedMessage:
        if not any(project.opaque_id == project_id for project in self.catalogue):
            return self._message("The project is no longer available.")
        buttons = tuple(
            Button(
                _profile_name(profile.profile_id),
                self._callback("launch.profile", f"{project_id}|{profile.profile_id}"),
            )
            for profile in self.profiles
            if profile.available
        )
        return self._message(
            "<b>Select an agent</b>",
            _button_rows(buttons) + ((Button("Back", self._callback("launch.open", "projects")),),),
        )

    def _confirm_reply(self, entity_id: str) -> RenderedMessage:
        project_id, profile_id = _split_launch(entity_id)
        if not any(project.opaque_id == project_id for project in self.catalogue):
            return self._message("The project is no longer available.")
        if not any(
            profile.profile_id == profile_id and profile.available for profile in self.profiles
        ):
            return self._message("That agent is unavailable.")
        project = next(project for project in self.catalogue if project.opaque_id == project_id)
        label = self._labels.get(entity_id)
        return self._message(
            f"<b>Review launch</b>\nProject: {escape(project.name)}\n"
            f"Agent: {_profile_name(profile_id)}\nLabel: {escape(label) if label else 'None'}",
            (
                (Button("Launch", self._callback("launch.confirm", entity_id, mutation=True)),),
                (Button("Add label", self._callback("launch.label", entity_id)),),
                (Button("Back", self._callback("launch.project", project_id)),),
                (Button("Cancel", self._callback("nav.home", "home")),),
            ),
        )

    def _message(
        self,
        text: str,
        keyboard: tuple[tuple[Button, ...], ...] = (),
        *,
        back: str | None = None,
        refresh: str | None = None,
    ) -> RenderedMessage:
        """Render one screen and close it with the navigation row it is entitled to.

        Home used to be the only way out of every screen, which made returning to the list
        a session came from cost two taps and a second search for the row. `back` takes the
        callback of the screen that owns this one; pass it wherever there is a real parent.

        `refresh` takes the callback that re-renders *this* screen, so it is offered only
        where the answer can go stale under the owner — the dashboard counts and the
        sessions list. Everything else would be a button that redraws what it already shows.
        """
        navigation = []
        if back is not None:
            navigation.append(Button("Back", back))
        if refresh is not None:
            navigation.append(Button("Refresh", refresh))
        navigation.append(Button("Home", self._callback("nav.home", "home")))
        return render_message(text, keyboard + (tuple(navigation),))

    def _callback(self, action: str, entity_id: str, *, mutation: bool = False) -> str:
        """Mint a token for a screen that has not been delivered yet.

        The keyboard is built before the message exists, so the token is created unbound and
        `_render`/`_bind_sent` attaches it once Telegram has answered with a message id.
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


async def run_private_bot(
    secrets: TelegramSecrets, boundary: PrivateBotBoundary | None = None
) -> None:
    """Long-poll the approved bot until SIGTERM/SIGINT, refusing a competing webhook."""
    boundary = boundary or PrivateBotBoundary(secrets.owner_user_id, secrets.owner_chat_id)
    # Sequential update handling is load-bearing rather than incidental: a render mints its
    # keyboard unbound and binds it once Telegram answers, and `bind_pending` adopts every
    # unbound token in the chat. Two renders in flight at once would let one screen's buttons
    # be adopted by the other's message. This is python-telegram-bot's default; it is written
    # out so a change made for throughput cannot quietly reopen that interleaving.
    application = ApplicationBuilder().token(secrets.bot_token).concurrent_updates(False).build()
    application.add_handler(CommandHandler("start", boundary.start))
    application.add_handler(CommandHandler("launch", boundary.launch_command))
    application.add_handler(CommandHandler("sessions", boundary.sessions_command))
    application.add_handler(CommandHandler("help", boundary.help_command))
    application.add_handler(CallbackQueryHandler(boundary.callback))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.UpdateType.MESSAGE, boundary.text)
    )
    stopping = asyncio.Event()
    _install_stop_signals(stopping)
    await application.initialize()
    try:
        await _sync_owner_metadata(application.bot, secrets.owner_chat_id)
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


async def _sync_owner_metadata(bot, owner_chat_id: int) -> None:
    scope = BotCommandScopeChat(owner_chat_id)
    await bot.delete_my_commands()
    await bot.set_my_commands(_OWNER_COMMANDS, scope=scope)
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
    report = {
        "default_commands": [command.command for command in default_commands],
        "owner_commands": [command.command for command in owner_commands],
        "owner_menu": getattr(menu, "type", None),
        "description_matches": description.description == _BOT_DESCRIPTION,
        "short_description_matches": short_description.short_description == _BOT_SHORT_DESCRIPTION,
    }
    report["healthy"] = (
        not report["default_commands"]
        and report["owner_commands"] == expected_commands
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
    Everything else is left whole — a project id or `home` has no session in it, and will
    simply never match one.
    """
    for separator in (":", "|"):
        entity_id = entity_id.split(separator, 1)[0]
    return entity_id


def _reply_arguments(message: RenderedMessage) -> dict[str, object]:
    return {
        "text": message.text,
        "parse_mode": ParseMode.HTML,
        "reply_markup": InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(button.text, callback_data=button.callback_data)
                    for button in row
                ]
                for row in message.keyboard
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


def _selectable_area(value: str) -> bool:
    """Offer an existing directory only when the project identity rule also accepts it."""
    try:
        ProjectIdentity(area=value, name=value)
    except ValueError:
        return False
    return True


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
    """Keep owner-approved titles useful without overflowing the compact keyboard."""
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


def _with_project_name(record: SessionRecord, name: str | None) -> SessionRecord:
    if name is None or name == record.display.project_slug:
        return record
    try:
        display = replace(record.display, project_slug=name)
    except ValueError:
        return record
    return replace(record, display=display)


def _button_rows(buttons: tuple[Button, ...], width: int = 2) -> tuple[tuple[Button, ...], ...]:
    return tuple(tuple(buttons[index : index + width]) for index in range(0, len(buttons), width))


def _label(value: str) -> str:
    normalized = " ".join(value.split())
    if (
        not normalized
        or len(normalized) > 40
        or any(not character.isprintable() for character in value)
    ):
        raise ValueError("label is invalid")
    return normalized


def _session_row_label(record: SessionRecord) -> str:
    return f"{record.display.rendered} · {record.state.value} · {age(record.created_at)}"


def _state_explanation(state: SessionState) -> str:
    """Defer to the shared mapping so both surfaces describe a state identically."""
    return explain_state(state)
