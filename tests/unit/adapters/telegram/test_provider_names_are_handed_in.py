"""The bot captions a provider with the name it was handed, and spells none of its own (BL-101).

The names used to be a literal dict in this adapter. They are now each descriptor's `name`,
folded by `registry.profile_names` at the composition root and handed over the way `glyphs`
is, because this adapter imports nothing under `adapters.agents` (DEC-070).
"""

from __future__ import annotations

from backends import backend_for
from fake_telegram import FakeChat

from remote_agents.adapters.telegram.presenters import unpadded
from remote_agents.adapters.telegram.service import build_private_bot
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject

OWNER = 7
CHAT = 11
PROJECT = CatalogProject("a" * 24, "Demo", "tests", "Registered")


async def _agent_buttons(names: dict[str, str]) -> list[str]:
    boundary = build_private_bot(
        OWNER,
        CHAT,
        backend=backend_for(catalogue=(PROJECT,)),
        profiles=(ProfileAvailability("claude", True, None),),
        names=names,
    )
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    await boundary.launch_command(chat.message_update("/launch"), None)
    anchor = chat.bot_messages[0].message_id
    project = next(
        button.callback_data
        for row in chat.messages[anchor].reply_markup.inline_keyboard
        for button in row
        if unpadded(button.text) == "Demo"
    )
    await boundary.callback(chat.press(project), None)
    return [
        unpadded(button.text)
        for row in chat.messages[anchor].reply_markup.inline_keyboard
        for button in row
    ]


async def test_the_launch_button_carries_the_name_it_was_handed() -> None:
    assert "Zed" in await _agent_buttons({"claude": "Zed"})


async def test_a_profile_with_no_name_handed_in_says_so() -> None:
    labels = await _agent_buttons({})
    assert "Unavailable" in labels
    assert "Claude" not in labels
