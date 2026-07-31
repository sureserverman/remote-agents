"""Live private-bot polling boundary with exact owner/chat authorization."""

from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass

from telegram import Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler

from remote_agents.config import TelegramSecrets


@dataclass(frozen=True, slots=True)
class PrivateBotBoundary:
    """Authorize the one configured private chat before handling any supported action."""

    owner_user_id: int
    owner_chat_id: int

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

    async def start(self, update: Update, context: object) -> None:
        """Expose an intentionally content-free readiness acknowledgement to the owner only."""
        del context
        if not self.permits(update) or update.effective_message is None:
            return
        await update.effective_message.reply_text("Remote Agents is ready.")

    async def callback(self, update: Update, context: object) -> None:
        """Acknowledge an authorized callback without accepting a control surface yet."""
        del context
        if not self.permits(update) or update.callback_query is None:
            return
        await update.callback_query.answer()


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
