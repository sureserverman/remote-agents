"""Live private-bot polling boundary with exact owner/chat authorization."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.inspection import inspect_capture
from remote_agents.adapters.telegram.presenters import Button, RenderedMessage
from remote_agents.adapters.telegram.stops import StopController
from remote_agents.adapters.telegram.wizard import ProfileAvailability
from remote_agents.application.commands import LaunchCommand
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.config import TelegramSecrets
from remote_agents.domain.models import ProfileId, ProjectId, SessionId, SessionRecord, SessionState


@dataclass(slots=True)
class PrivateBotBoundary:
    """Authorize the one configured private chat before handling any supported action."""

    owner_user_id: int
    owner_chat_id: int
    catalogue: tuple[CatalogProject, ...] = ()
    profiles: tuple[ProfileAvailability, ...] = ()
    launcher: object | None = None
    capture: Callable[[SessionId], Awaitable[str]] | None = None
    callbacks: CallbackStateStore = field(default_factory=CallbackStateStore)
    stops: StopController = field(init=False)
    _view_revisions: dict[tuple[int, int], int] = field(default_factory=dict)
    _force_confirmed: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.stops = StopController(self.callbacks)

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
        await update.effective_message.reply_text(**self._home_reply())

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
            return
        await query.answer()
        try:
            await query.edit_message_text(
                **(await self._reply_for(state.action, state.entity_id, token=query.data or ""))
            )
        except BadRequest as error:
            if "Message is not modified" not in str(error):
                raise

    def _home_reply(self) -> dict[str, object]:
        self._next_revision(self.owner_user_id, self.owner_chat_id)
        return _reply_arguments(
            self._message(
                "<b>Remote agents</b>\nChoose an action.",
                (
                    (Button("Launch", self._callback("launch.open", "projects")),),
                    (Button("Sessions", self._callback("sessions.open", "sessions")),),
                    (Button("Refresh", self._callback("nav.refresh", "home")),),
                ),
            )
        )

    async def _reply_for(
        self, action: str, entity_id: str, *, token: str = ""
    ) -> dict[str, object]:
        if action in {"nav.home", "nav.refresh"}:
            return self._home_reply()
        if action == "launch.confirm":
            return await self._launch_reply(entity_id, token)
        if action in {"graceful", "cleanup", "force"}:
            return await self._stop_reply(action, token)
        self._next_revision(self.owner_user_id, self.owner_chat_id)
        if action == "launch.open":
            return _reply_arguments(self._projects_reply())
        if action == "launch.project":
            return _reply_arguments(self._profiles_reply(entity_id))
        if action == "launch.profile":
            return _reply_arguments(self._confirm_reply(entity_id))
        if action == "sessions.open":
            return _reply_arguments(await self._sessions_reply())
        if action == "session.detail":
            return _reply_arguments(await self._detail_reply(entity_id))
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
        await self.launcher.launch(
            LaunchCommand(ProjectId(project_id), ProfileId(profile_id), token)
        )
        self._next_revision(self.owner_user_id, self.owner_chat_id)
        return _reply_arguments(self._message("Session launch requested."))

    async def _sessions_reply(self) -> RenderedMessage:
        records = await self._records()
        return self._message(
            "<b>Sessions</b>",
            tuple(
                (
                    Button(
                        record.display.rendered,
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
        for action in _available_stops(record.state):
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
            f"<b>{escape(record.display.rendered)}</b>\n{record.state}", tuple(buttons)
        )

    async def _inspect_reply(self, session_value: str) -> RenderedMessage:
        if self.capture is None:
            return self._message("Inspection is unavailable.")
        captured = await self.capture(SessionId.parse(session_value))
        result = inspect_capture(captured.encode())
        return self._message(result.text)

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
        return _reply_arguments(self._message("Session action completed."))

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

    def _projects_reply(self) -> RenderedMessage:
        return self._message(
            "<b>Projects</b>\nSelect a project to launch.",
            tuple(
                (
                    Button(
                        project.name,
                        self._callback("launch.project", project.opaque_id),
                    ),
                )
                for project in self.catalogue[:20]
            ),
        )

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
            _button_rows(buttons),
        )

    def _confirm_reply(self, entity_id: str) -> RenderedMessage:
        project_id, profile_id = _split_launch(entity_id)
        if not any(project.opaque_id == project_id for project in self.catalogue):
            return self._message("The project is no longer available.")
        if not any(
            profile.profile_id == profile_id and profile.available for profile in self.profiles
        ):
            return self._message("That agent is unavailable.")
        return self._message(
            f"Launch {_profile_name(profile_id)}?",
            ((Button("Launch", self._callback("launch.confirm", entity_id, mutation=True)),),),
        )

    def _message(self, text: str, keyboard: tuple[tuple[Button, ...], ...] = ()) -> RenderedMessage:
        home = Button("Home", self._callback("nav.home", "home"))
        return RenderedMessage(text, keyboard + ((home,),))

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
    application.add_handler(CallbackQueryHandler(boundary.callback))
    stopping = asyncio.Event()
    _install_stop_signals(stopping)
    await application.initialize()
    try:
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


def _profile_name(profile_id: str) -> str:
    return {
        "claude": "Claude",
        "claude-remote": "Claude Remote",
        "codex": "Codex",
        "opencode": "OpenCode",
        "cursor-agent": "Cursor Agent",
    }.get(profile_id, "Unavailable")


def _available_stops(state: SessionState) -> tuple[str, ...]:
    actions = ["force"]
    if state is SessionState.RUNNING:
        actions.insert(0, "graceful")
    if state is SessionState.PRESERVED:
        actions.insert(0, "cleanup")
    return tuple(actions)


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
