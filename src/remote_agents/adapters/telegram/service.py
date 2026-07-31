"""Live private-bot polling boundary with exact owner/chat authorization."""

from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass, field

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from remote_agents.adapters.telegram.callbacks import CallbackStateStore
from remote_agents.adapters.telegram.presenters import (
    NavigationCallbacks,
    RenderedMessage,
    render_home,
)
from remote_agents.config import TelegramSecrets


@dataclass(slots=True)
class PrivateBotBoundary:
    """Authorize the one configured private chat before handling any supported action."""

    owner_user_id: int
    owner_chat_id: int
    callbacks: CallbackStateStore = field(default_factory=CallbackStateStore)
    _view_revisions: dict[tuple[int, int], int] = field(default_factory=dict)

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
        if state is None or state.action not in {"nav.home", "nav.refresh"}:
            await query.answer("This view has expired.")
            return
        await query.answer()
        await query.edit_message_text(**self._home_reply())

    def _home_reply(self) -> dict[str, object]:
        view_revision = self._next_revision(self.owner_user_id, self.owner_chat_id)
        callbacks = NavigationCallbacks(
            home=self.callbacks.create(
                "nav.home", "home", self.owner_user_id, self.owner_chat_id, view_revision
            ),
            back=self.callbacks.create(
                "nav.back", "home", self.owner_user_id, self.owner_chat_id, view_revision
            ),
            refresh=self.callbacks.create(
                "nav.refresh", "home", self.owner_user_id, self.owner_chat_id, view_revision
            ),
            previous=self.callbacks.create(
                "nav.previous", "home", self.owner_user_id, self.owner_chat_id, view_revision
            ),
            next=self.callbacks.create(
                "nav.next", "home", self.owner_user_id, self.owner_chat_id, view_revision
            ),
        )
        return _reply_arguments(render_home(callbacks))

    def _next_revision(self, owner_id: int, chat_id: int) -> int:
        key = (owner_id, chat_id)
        revision = self._view_revisions.get(key, 0) + 1
        self._view_revisions[key] = revision
        return revision


async def run_private_bot(secrets: TelegramSecrets) -> None:
    """Long-poll the approved bot until SIGTERM/SIGINT, refusing a competing webhook."""
    boundary = PrivateBotBoundary(secrets.owner_user_id, secrets.owner_chat_id)
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
