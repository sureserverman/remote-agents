"""Telegram screen dependencies and conversion to transport arguments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from remote_agents.adapters.telegram.presenters import Button, RenderedMessage, uniform_keyboard
from remote_agents.application.backend import Backend
from remote_agents.ports.callback_state import CallbackStatePort


class RenderScreen(Protocol):
    def __call__(
        self,
        text: str,
        keyboard: tuple[tuple[Button, ...], ...] = (),
        *,
        back: str | None = None,
        back_label: str = "Back",
    ) -> RenderedMessage: ...


class CreateCallback(Protocol):
    def __call__(self, action: str, entity_id: str, *, mutation: bool = False) -> str: ...


@dataclass
class ScreenActions:
    """Ports shared by screens. Delivery and callback authorization belong to the boundary."""

    backend: Backend
    callbacks: CallbackStatePort
    owner_user_id: int
    owner_chat_id: int
    _message: RenderScreen
    _callback: CreateCallback


def reply_arguments(
    message: RenderedMessage, *, protect_content: bool = False
) -> dict[str, object]:
    """The Telegram call arguments for one rendered screen.

    `protect_content` marks a message unforwardable and unsavable in the client. Off by
    default because a menu is not a secret; set for the two sends whose content is one.
    """
    arguments: dict[str, object] = {
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
    if protect_content:
        arguments["protect_content"] = True
    return arguments
