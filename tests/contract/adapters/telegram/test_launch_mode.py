"""The bot offers "Shared folder / Own worktree" only on contention (BL-116, DEC-011).

With no other session live on the project, choosing an agent launches it exactly as before.
With one, the press draws the two modes instead, each a one-shot token minted for that
message, and the chosen one reaches the launch.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from backends import SessionUseCaseDouble, backend_for
from fake_telegram import FakeChat

from remote_agents.adapters.agents.registry import profile_names
from remote_agents.adapters.telegram.service import PrivateBotBoundary, build_private_bot
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.services import LaunchOutcome
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

# The longest project id the domain accepts: the callback data must not grow with it.
_PROJECT = "p" * 64


class _Launcher(SessionUseCaseDouble):
    def __init__(self, *, offered: bool, outcome: LaunchOutcome | None = None) -> None:
        self.offered = offered
        self.asked: list[object] = []
        self.commands: list[object] = []
        self.outcome = outcome

    async def list_sessions(self):
        return []

    async def refresh_readiness(self) -> None:
        return None

    async def worktree_offered(self, project_id):
        self.asked.append(project_id)
        return self.offered

    async def launch(self, command):
        self.commands.append(command)
        return self.outcome


def _boundary(launcher: _Launcher) -> PrivateBotBoundary:
    return build_private_bot(
        7,
        11,
        backend=backend_for(
            catalogue=(CatalogProject(_PROJECT, "Demo", "tests", "Registered"),),
            sessions=launcher,
        ),
        profiles=(ProfileAvailability("claude", True),),
        names=profile_names(),
    )


def _buttons(message) -> dict[str, str]:
    return {
        button.text.removeprefix("• "): button.callback_data
        for row in message.reply_markup.inline_keyboard
        for button in row
    }


async def _press_agent(launcher: _Launcher) -> tuple[PrivateBotBoundary, FakeChat, int]:
    boundary = _boundary(launcher)
    chat = FakeChat()
    await boundary.launch_command(chat.message_update("/launch"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(chat.press(_buttons(chat.messages[anchor])["Demo"]), None)
    await boundary.callback(chat.press(_buttons(chat.messages[anchor])["Claude"]), None)
    return boundary, chat, anchor


@pytest.mark.asyncio
async def test_with_no_contention_one_press_launches_and_no_mode_is_asked() -> None:
    launcher = _Launcher(offered=False)
    _, chat, anchor = await _press_agent(launcher)

    assert len(launcher.commands) == 1
    assert launcher.commands[0].mode == "regular"
    assert "Own worktree" not in _buttons(chat.messages[anchor])


@pytest.mark.asyncio
async def test_with_contention_the_modes_are_offered_and_nothing_launches_yet() -> None:
    launcher = _Launcher(offered=True)
    _, chat, anchor = await _press_agent(launcher)

    assert launcher.commands == []
    assert [str(project) for project in launcher.asked] == [_PROJECT]
    buttons = _buttons(chat.messages[anchor])
    assert {"Shared folder", "Own worktree"} <= set(buttons)
    for label in ("Shared folder", "Own worktree"):
        assert len(buttons[label].encode()) <= 64, "Telegram's callback_data budget"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "mode"), [("Own worktree", "worktree"), ("Shared folder", "regular")]
)
async def test_the_chosen_mode_reaches_the_launch(label: str, mode: str) -> None:
    launcher = _Launcher(offered=True)
    boundary, chat, anchor = await _press_agent(launcher)

    await boundary.callback(chat.press(_buttons(chat.messages[anchor])[label]), None)

    assert len(launcher.commands) == 1
    command = launcher.commands[0]
    assert command.mode == mode
    assert str(command.project_id) == _PROJECT
    assert str(command.profile_id) == "claude"


@pytest.mark.asyncio
async def test_a_mode_button_is_a_one_shot_token_for_its_own_message() -> None:
    launcher = _Launcher(offered=True)
    boundary, chat, anchor = await _press_agent(launcher)
    token = _buttons(chat.messages[anchor])["Own worktree"]

    state = boundary.callbacks.resolve(token, owner_id=7, chat_id=11, message_id=anchor)
    assert state is not None and state.mutation is True
    assert state.message_id == anchor

    await boundary.callback(chat.press(token, on=anchor), None)
    await boundary.callback(chat.press(token, on=anchor), None)
    assert len(launcher.commands) == 1, "a replayed mode press must not launch twice"


@pytest.mark.asyncio
async def test_a_worktree_that_could_not_be_made_says_why() -> None:
    record = SessionRecord(
        SessionId.new(),
        ProjectId(_PROJECT),
        ProfileId("claude"),
        SessionDisplayIdentity("demo", "claude", "worktree", 1),
        SessionState.FAILED,
        datetime(2026, 10, 10, tzinfo=UTC),
    )
    launcher = _Launcher(
        offered=True,
        outcome=LaunchOutcome(record, False, worktree_failed="fatal: <invalid> reference"),
    )
    boundary, chat, anchor = await _press_agent(launcher)

    await boundary.callback(chat.press(_buttons(chat.messages[anchor])["Own worktree"]), None)

    text = chat.messages[anchor].text
    assert "Session did not start" in text
    # HTML: git's words are escaped, never taken as markup.
    assert "Its worktree could not be made: fatal: &lt;invalid&gt; reference" in text
    assert "Its pane may still exist" not in text
