"""Opt-in owner-bound verification against the configured private Telegram bot."""

from __future__ import annotations

import pytest
from telegram import Bot
from telegram.constants import ChatType
from telegram.error import InvalidToken

from remote_agents.config import load_secrets


@pytest.mark.live_telegram
async def test_configured_bot_is_reachable_private_and_not_webhook_backed() -> None:
    secrets = load_secrets(production=False)
    if secrets is None:
        pytest.skip("BLOCKED: Telegram environment is not loaded")

    async with Bot(secrets.bot_token) as bot:
        identity = await bot.get_me()
        webhook = await bot.get_webhook_info()
        chat = await bot.get_chat(secrets.owner_chat_id)

    assert identity.id > 0
    assert webhook.url == ""
    assert chat.id == secrets.owner_chat_id
    assert chat.type == ChatType.PRIVATE


@pytest.mark.live_telegram
async def test_rejected_credential_cannot_access_the_bot() -> None:
    """Exercise credential denial without replacing the configured production credential."""
    bot = Bot("000000000:invalid-token")
    try:
        with pytest.raises(InvalidToken):
            await bot.get_me()
    finally:
        await bot.shutdown()
