"""Live private-bot polling boundary with exact owner/chat authorization."""

from __future__ import annotations

import asyncio
import io
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from html import escape

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
from telegram.error import BadRequest
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
from remote_agents.adapters.telegram.presenters import (
    Button,
    NavigationCallbacks,
    RenderedMessage,
    render_home,
    render_message,
)
from remote_agents.adapters.telegram.stops import StopController
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
from remote_agents.application.session_actions import (
    available_actions,
    explain_state,
    remote_control_available,
)
from remote_agents.config import TelegramSecrets
from remote_agents.domain.conversations import ConversationReference, ConversationState
from remote_agents.domain.models import ProfileId, ProjectId, SessionId, SessionRecord, SessionState
from remote_agents.domain.projects import ProjectIdentity
from remote_agents.domain.remote_control import RemoteControlState
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
    "project.name": (
        "Reply with the new project name. Send Cancel or Back to leave this step.",
        "New project name",
    ),
}
_ENTRY_INSTRUCTIONS = {
    "launch.search": "Reply below with a project name.",
    "project.name": "Reply below with the new project name.",
}


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
    callbacks: CallbackStateStore = field(default_factory=CallbackStateStore)
    stops: StopController = field(init=False)
    _view_revisions: dict[tuple[int, int], int] = field(default_factory=dict)
    _force_confirmed: set[str] = field(default_factory=set)
    _awaiting_text: dict[tuple[int, int], tuple[str, str]] = field(default_factory=dict)
    _labels: dict[str, str] = field(default_factory=dict)
    _project_views: dict[str, tuple[CatalogProject, ...]] = field(default_factory=dict)
    project_page_size: int = 10
    catalogue_source: Callable[[], tuple[CatalogProject, ...]] | None = None

    def __post_init__(self) -> None:
        self.stops = StopController(self.callbacks)

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
        self._awaiting_text.pop((self.owner_user_id, self.owner_chat_id), None)
        await update.effective_message.reply_text(**(await self._home_reply()))

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Accept bounded local catalogue search or a session label while explicitly requested."""
        del context
        if not self.permits(update) or update.effective_message is None:
            return
        request = self._awaiting_text.get((self.owner_user_id, self.owner_chat_id))
        if request is None:
            return
        value = update.effective_message.text or ""
        action, entity_id = request
        if value.casefold() in {"cancel", "back"}:
            self._awaiting_text.pop((self.owner_user_id, self.owner_chat_id), None)
            await update.effective_message.reply_text(**(await self._home_reply()))
            return
        if action == "launch.search":
            projects = search_catalogue(self.catalogue, value)
            if not projects:
                await update.effective_message.reply_text(
                    **self._guided_text_reply(
                        "launch.search", "No projects found. Try another name."
                    )
                )
                return
            self._awaiting_text.pop((self.owner_user_id, self.owner_chat_id), None)
            self._next_revision(self.owner_user_id, self.owner_chat_id)
            await update.effective_message.reply_text(
                **_reply_arguments(self._projects_reply(projects, view_id="search"))
            )
            return
        if action == "project.name":
            try:
                identity = ProjectIdentity(area=entity_id, name=value.strip())
            except ValueError as error:
                await update.effective_message.reply_text(
                    **self._guided_text_reply("project.name", str(error))
                )
                return
            self._awaiting_text.pop((self.owner_user_id, self.owner_chat_id), None)
            self._next_revision(self.owner_user_id, self.owner_chat_id)
            await update.effective_message.reply_text(
                **_reply_arguments(self._project_review_reply(identity))
            )
            return
        if value.casefold() == "skip":
            self._labels.pop(entity_id, None)
            self._awaiting_text.pop((self.owner_user_id, self.owner_chat_id), None)
            self._next_revision(self.owner_user_id, self.owner_chat_id)
            await update.effective_message.reply_text(
                **_reply_arguments(self._confirm_reply(entity_id))
            )
            return
        try:
            self._labels[entity_id] = _label(value)
        except ValueError:
            await update.effective_message.reply_text(
                **self._guided_text_reply(
                    "launch.label", "Use a visible label of up to 40 characters."
                )
            )
            return
        self._awaiting_text.pop((self.owner_user_id, self.owner_chat_id), None)
        self._next_revision(self.owner_user_id, self.owner_chat_id)
        await update.effective_message.reply_text(
            **_reply_arguments(self._confirm_reply(entity_id))
        )

    async def launch_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if self.permits(update) and update.effective_message is not None:
            self._next_revision(self.owner_user_id, self.owner_chat_id)
            await update.effective_message.reply_text(
                **_reply_arguments(self._projects_reply(self.catalogue, view_id="all"))
            )

    async def sessions_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if self.permits(update) and update.effective_message is not None:
            await update.effective_message.reply_text(
                **_reply_arguments(await self._sessions_reply())
            )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if self.permits(update) and update.effective_message is not None:
            await update.effective_message.reply_text(
                "Use Launch to start a curated agent, or Sessions to inspect and stop "
                "managed sessions."
            )

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Acknowledge and refresh only callbacks issued to this exact private chat."""
        del context
        if not self.permits(update) or update.callback_query is None:
            return
        query = update.callback_query
        owner_id = self.owner_user_id
        chat_id = self.owner_chat_id
        revision = self._view_revisions.get((owner_id, chat_id), 0)
        state = self.callbacks.resolve(
            query.data or "", owner_id=owner_id, chat_id=chat_id, view_revision=revision
        )
        if state is None:
            await query.answer("This view has expired.")
            try:
                await query.edit_message_text(**(await self._home_reply()))
            except BadRequest as error:
                if "Message is not modified" not in str(error):
                    raise
            return
        await query.answer()
        try:
            if state.action == "session.inspect":
                await self._send_inspection(query, state.entity_id)
                return
            if state.action in {"launch.search", "launch.label", "project.area"}:
                await self._begin_guided_text_entry(query, state.action, state.entity_id)
                return
            await query.edit_message_text(
                **(await self._reply_for(state.action, state.entity_id, token=query.data or ""))
            )
        except BadRequest as error:
            if "Message is not modified" not in str(error):
                raise

    async def _home_reply(self, *, refresh: bool = False) -> dict[str, object]:
        if refresh:
            await self.refresh_catalogue()
        self._next_revision(self.owner_user_id, self.owner_chat_id)
        records = await self._records()
        return _reply_arguments(
            render_home(
                self._navigation_callbacks(),
                launch=self._callback("launch.open", "projects"),
                resume=(self._callback("resume.open", "projects") if self.conversations else None),
                sessions=self._callback("sessions.open", "sessions"),
                add_project=(self._callback("project.open", "areas") if self.creator else None),
                active=sum(record.state is SessionState.RUNNING for record in records),
                preserved=sum(record.state is SessionState.PRESERVED for record in records),
            )
        )

    async def _reply_for(
        self, action: str, entity_id: str, *, token: str = ""
    ) -> dict[str, object]:
        if action in {"nav.home", "nav.refresh"}:
            return await self._home_reply(refresh=action == "nav.refresh")
        if action == "launch.confirm":
            return await self._launch_reply(entity_id, token)
        if action == "resume.confirm":
            return await self._resume_reply(entity_id, token)
        if action == "remote.confirm":
            return await self._remote_control_reply(entity_id, token)
        if action == "project.confirm":
            return await self._project_reply(entity_id, token)
        if action in {"graceful", "cleanup", "force"}:
            return await self._stop_reply(action, token)
        self._next_revision(self.owner_user_id, self.owner_chat_id)
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
        if action == "resume.open":
            return _reply_arguments(self._resume_projects_reply())
        if action == "resume.project":
            return _reply_arguments(await self._resume_profiles_reply(entity_id))
        if action in {"resume.profile", "resume.page"}:
            return _reply_arguments(await self._resume_catalogue_reply(entity_id))
        if action == "resume.select":
            return _reply_arguments(await self._resume_confirm_reply(entity_id))
        if action == "session.detail":
            return _reply_arguments(await self._detail_reply(entity_id))
        if action == "session.attach":
            return _reply_arguments(await self._attach_reply(entity_id))
        if action == "remote.control":
            return _reply_arguments(await self._remote_control_confirm_reply(entity_id))
        if action == "session.inspect":
            return _reply_arguments(await self._inspect_reply(entity_id))
        return _reply_arguments(self._message("This view has expired."))

    async def _launch_reply(self, entity_id: str, token: str) -> dict[str, object]:
        if self.launcher is None or not self.callbacks.claim_mutation(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            view_revision=self._view_revisions[(self.owner_user_id, self.owner_chat_id)],
        ):
            return _reply_arguments(self._message("That request has expired."))
        project_id, profile_id = _split_launch(entity_id)
        record = await self.launcher.launch(
            LaunchCommand(
                ProjectId(project_id),
                ProfileId(profile_id),
                token,
                self._labels.pop(entity_id, None),
            )
        )
        self._next_revision(self.owner_user_id, self.owner_chat_id)
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

    async def _resume_reply(self, reference_value: str, token: str) -> dict[str, object]:
        if (
            self.launcher is None
            or self.conversations is None
            or not self.callbacks.claim_mutation(
                token,
                owner_id=self.owner_user_id,
                chat_id=self.owner_chat_id,
                view_revision=self._view_revisions[(self.owner_user_id, self.owner_chat_id)],
            )
        ):
            return _reply_arguments(self._message("That request has expired."))
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
        self._next_revision(self.owner_user_id, self.owner_chat_id)
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

    async def _project_reply(self, entity_id: str, token: str) -> dict[str, object]:
        """Create at most once per confirmation, then re-read the catalogue off the loop."""
        if self.creator is None or not self.callbacks.claim_mutation(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            view_revision=self._view_revisions.get((self.owner_user_id, self.owner_chat_id), 0),
        ):
            return _reply_arguments(self._message("That request has expired."))
        area, separator, name = entity_id.partition("|")
        if not separator:
            return _reply_arguments(self._message("That request has expired."))
        try:
            created = await asyncio.to_thread(self.creator.create, CreateProjectCommand(area, name))
        except ProjectCreationError as error:
            self._next_revision(self.owner_user_id, self.owner_chat_id)
            return _reply_arguments(
                self._message(f"<b>Project not created</b>\n{escape(str(error))}")
            )
        except Exception:
            _LOG.exception("project creation failed outside the application's error contract")
            self._next_revision(self.owner_user_id, self.owner_chat_id)
            return _reply_arguments(self._message("<b>Project not created</b>\nCheck this host."))
        await self.refresh_catalogue()
        self._next_revision(self.owner_user_id, self.owner_chat_id)
        return _reply_arguments(
            self._message(
                f"<b>Project created</b>\n{escape(str(created.identity))}",
                ((Button("Launch", self._callback("launch.open", "projects")),),),
            )
        )

    async def _sessions_reply(self) -> RenderedMessage:
        if self.launcher is not None:
            await self.launcher.refresh_readiness()
        records = await self._records()
        return self._message(
            "<b>Sessions</b>",
            tuple(
                (
                    Button(
                        _session_row_label(record),
                        self._callback("session.detail", str(record.session_id)),
                    ),
                )
                for record in records
            )
            or ((Button("No managed sessions", self._callback("nav.home", "home")),),),
        )

    async def _detail_reply(self, session_value: str) -> RenderedMessage:
        record = await self._record(session_value)
        if record is None:
            return self._message("That session is no longer available.")
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
        for action in available_actions(record.state):
            token = self.stops.offer(
                record.session_id,
                record.profile_id,
                record.state,
                action,
                self.owner_user_id,
                self.owner_chat_id,
                self._view_revisions[(self.owner_user_id, self.owner_chat_id)],
            )
            if token is not None:
                buttons.append((Button(action.title(), token),))
        return self._message(
            f"<b>{escape(record.display.rendered)}</b>\n"
            f"State: {record.state.value}\n{_state_explanation(record.state)}",
            tuple(buttons),
        )

    async def _attach_reply(self, session_value: str) -> RenderedMessage:
        record = await self._record(session_value)
        if record is None or not await self._can_copy_attach(record):
            return self._message("Copy Attach is unavailable until this managed pane is live.")
        copy = getattr(self.launcher, "copy_attach", None)
        command = await copy(record.session_id) if copy is not None else None
        if command is None:
            return self._message("Copy Attach is unavailable until this managed pane is live.")
        return self._message(f"<b>Copy attach command</b>\n<code>{escape(command)}</code>")

    async def _remote_control_confirm_reply(self, entity_id: str) -> RenderedMessage:
        session_value, separator, state_value = entity_id.partition("|")
        if not separator or state_value not in {"active", "inactive"}:
            return self._message("That Remote Control request has expired.")
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

    async def _remote_control_reply(self, entity_id: str, token: str) -> dict[str, object]:
        if self.launcher is None or not self.callbacks.claim_mutation(
            token,
            owner_id=self.owner_user_id,
            chat_id=self.owner_chat_id,
            view_revision=self._view_revisions[(self.owner_user_id, self.owner_chat_id)],
        ):
            return _reply_arguments(self._message("That request has expired."))
        session_value, separator, state_value = entity_id.partition("|")
        if not separator:
            return _reply_arguments(self._message("That request has expired."))
        state = RemoteControlState(state_value)
        result = await self.launcher.set_remote_control(
            RemoteControlCommand(SessionId.parse(session_value), state, token)
        )
        self._next_revision(self.owner_user_id, self.owner_chat_id)
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
        result = await self._inspection_result(session_value)
        if result is None:
            return self._message("Inspection is unavailable.")
        return self._message(f"<pre>{escape(result.text)}</pre>")

    async def _send_inspection(self, query, session_value: str) -> None:
        result = await self._inspection_result(session_value)
        if result is None:
            await query.edit_message_text(
                **_reply_arguments(self._message("Inspection is unavailable."))
            )
            return
        await query.edit_message_text(
            **_reply_arguments(self._message(f"<pre>{escape(result.text)}</pre>"))
        )
        if result.attachment is not None and result.filename is not None:
            await query.message.reply_document(
                document=io.BytesIO(result.attachment), filename=result.filename
            )

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
        self._next_revision(self.owner_user_id, self.owner_chat_id)
        entry = "project.name" if action == "project.area" else action
        self._awaiting_text[(self.owner_user_id, self.owner_chat_id)] = (entry, entity_id)
        await query.edit_message_text(**_reply_arguments(self._message(_entry_instruction(entry))))
        await query.message.reply_text(**self._guided_text_reply(entry))

    async def _stop_reply(self, action: str, token: str) -> dict[str, object]:
        revision = self._view_revisions[(self.owner_user_id, self.owner_chat_id)]
        if action == "force" and token not in self._force_confirmed:
            if not self.stops.confirm_force(
                token, self.owner_user_id, self.owner_chat_id, revision
            ):
                return _reply_arguments(self._message("That request has expired."))
            self._force_confirmed.add(token)
            return _reply_arguments(
                self._message("Confirm force stop.", ((Button("Force stop", token),),))
            )
        request = self.stops.claim(token, self.owner_user_id, self.owner_chat_id, revision)
        if request is None or self.launcher is None:
            return _reply_arguments(self._message("That request has expired."))
        record = await self._record(str(request.session_id))
        if record is None or not await self.stops.execute(request, self.launcher, record):
            return _reply_arguments(self._message("That session changed; refresh it first."))
        self._next_revision(self.owner_user_id, self.owner_chat_id)
        return _reply_arguments(self._message(f"{action.title()} completed for this session."))

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
        self, projects: tuple[CatalogProject, ...], *, view_id: str, page: int = 1
    ) -> RenderedMessage:
        self._project_views[view_id] = projects
        try:
            rendered = paginate_catalogue(projects, page, self.project_page_size)
        except ValueError:
            return self._message("That project view has expired.")
        buttons = [
            (
                Button(
                    project.name,
                    self._callback("launch.project", project.opaque_id),
                ),
            )
            for project in rendered.projects
        ]
        navigation = []
        if rendered.page > 1:
            navigation.append(
                Button("Previous", self._callback("launch.page", f"{view_id}|{rendered.page - 1}"))
            )
        if rendered.page < rendered.page_count:
            navigation.append(
                Button("Next", self._callback("launch.page", f"{view_id}|{rendered.page + 1}"))
            )
        if navigation:
            buttons.append(tuple(navigation))
        buttons.append((Button("Search", self._callback("launch.search", "search")),))
        buttons.append((Button("Back", self._callback("nav.home", "home")),))
        return self._message(
            f"<b>Projects {rendered.page}/{rendered.page_count}</b>\nSelect a project to launch.",
            tuple(buttons),
        )

    def _project_page_reply(self, entity_id: str) -> RenderedMessage:
        view_id, separator, page_value = entity_id.partition("|")
        if not separator or view_id not in {"all", "search"}:
            return self._message("That project view has expired.")
        try:
            page = int(page_value)
        except ValueError:
            return self._message("That project view has expired.")
        projects = self._project_views.get(view_id)
        if projects is None:
            return self._message("That project view has expired.")
        return self._projects_reply(projects, view_id=view_id, page=page)

    def _resume_projects_reply(self) -> RenderedMessage:
        buttons = tuple(
            (Button(project.name, self._callback("resume.project", project.opaque_id)),)
            for project in self.catalogue
        )
        return self._message(
            "<b>Resume</b>\nSelect the project for the prior conversation.",
            buttons or ((Button("No projects available", self._callback("nav.home", "home")),),),
        )

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
            return self._message("That continuation view has expired.")
        project_id, profile_id, page = parsed
        if not any(project.opaque_id == project_id for project in self.catalogue):
            return self._message("The project is no longer available.")
        try:
            result = await self.conversations.catalogue(
                ConversationCatalogueQuery(page, 10, ProfileId(profile_id), ProjectId(project_id))
            )
        except ValueError:
            return self._message("That continuation view has expired.")
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
        rows = list(buttons)
        if navigation:
            rows.append(tuple(navigation))
        rows.append((Button("Back", self._callback("resume.project", project_id)),))
        return self._message(
            f"<b>Prior conversations {result.page}/{result.page_count}</b>\n"
            "Select a resumable conversation.",
            tuple(rows)
            if buttons
            else (
                (
                    Button(
                        "No resumable conversations",
                        self._callback("resume.project", project_id),
                    ),
                ),
            ),
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

    def _message(self, text: str, keyboard: tuple[tuple[Button, ...], ...] = ()) -> RenderedMessage:
        home = Button("Home", self._callback("nav.home", "home"))
        return render_message(text, keyboard + ((home,),))

    def _callback(self, action: str, entity_id: str, *, mutation: bool = False) -> str:
        revision = self._view_revisions.get((self.owner_user_id, self.owner_chat_id), 0)
        return self.callbacks.create(
            action,
            entity_id,
            self.owner_user_id,
            self.owner_chat_id,
            revision,
            mutation=mutation,
        )

    def _navigation_callbacks(self) -> NavigationCallbacks:
        return NavigationCallbacks(
            home=self._callback("nav.home", "home"),
            back=self._callback("nav.home", "home"),
            refresh=self._callback("nav.refresh", "home"),
            previous=self._callback("nav.home", "home"),
            next=self._callback("nav.home", "home"),
        )

    def _guided_text_reply(self, action: str, text: str | None = None) -> dict[str, object]:
        instruction, placeholder = _GUIDED_TEXT_ENTRY.get(
            action, ("Reply with a label, or send Skip, Cancel, or Back.", "Optional session label")
        )
        return {
            "text": text or instruction,
            "reply_markup": ForceReply(input_field_placeholder=placeholder),
        }

    def _next_revision(self, owner_id: int, chat_id: int) -> int:
        key = (owner_id, chat_id)
        revision = self._view_revisions.get(key, 0) + 1
        self._view_revisions[key] = revision
        return revision


async def run_private_bot(
    secrets: TelegramSecrets, boundary: PrivateBotBoundary | None = None
) -> None:
    """Long-poll the approved bot until SIGTERM/SIGINT, refusing a competing webhook."""
    boundary = boundary or PrivateBotBoundary(secrets.owner_user_id, secrets.owner_chat_id)
    application = ApplicationBuilder().token(secrets.bot_token).build()
    application.add_handler(CommandHandler("start", boundary.start))
    application.add_handler(CommandHandler("launch", boundary.launch_command))
    application.add_handler(CommandHandler("sessions", boundary.sessions_command))
    application.add_handler(CommandHandler("help", boundary.help_command))
    application.add_handler(CallbackQueryHandler(boundary.callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, boundary.text))
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


def _age(created_at: datetime) -> str:
    minutes = max(0, int((datetime.now(UTC) - created_at).total_seconds() // 60))
    return f"{minutes}m ago"


def _session_row_label(record: SessionRecord) -> str:
    return f"{record.display.rendered} · {record.state.value} · {_age(record.created_at)}"


def _state_explanation(state: SessionState) -> str:
    """Defer to the shared mapping so both surfaces describe a state identically."""
    return explain_state(state)
