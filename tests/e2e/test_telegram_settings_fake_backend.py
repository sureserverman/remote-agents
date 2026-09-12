"""The owner opens Settings on their phone and moves both providers, end to end.

Driven through the chat rather than through the reply builders: `/settings` is typed, each row
is pressed on the message actually carrying it, and the Codex row's confirmation is pressed on
the screen the row drew. What that adds over the contract tests beside it is the wiring -- the
command handler, the callback dispatch, the token binding, and the live view's one-message
rule -- none of which a direct call to `_settings_screen` can fail on.

The shape is borrowed from `test_telegram_host_remote_control_fake_backend.py` deliberately:
the Codex row's whole job is to hand the owner over to that screen, so the journey this file
drives is the one that file drives with one extra press in front of it.
"""

from __future__ import annotations

from dataclasses import replace

from backends import FakeHostRemoteControl, SessionUseCaseDouble, backend_for
from fake_telegram import FakeChat

from remote_agents.adapters.telegram.presenters import unpadded
from remote_agents.adapters.telegram.service import (
    PrivateBotBoundary,
    build_private_bot,
    unmarked,
)
from remote_agents.application.host_remote_control import (
    HOST_REMOTE_CONTROL_LABELS,
    HOST_REMOTE_CONTROL_TITLE,
)
from remote_agents.application.remote_control_default import (
    REMOTE_CONTROL_DEFAULT_LABELS,
    REMOTE_CONTROL_DEFAULT_TITLE,
)
from remote_agents.domain.remote_control import (
    HostConnection,
    RemoteControlDefault,
    RemoteControlState,
)

ON = HOST_REMOTE_CONTROL_LABELS[RemoteControlState.ACTIVE]
OFF = HOST_REMOTE_CONTROL_LABELS[RemoteControlState.INACTIVE]


class _NoSessions(SessionUseCaseDouble):
    """Nothing is running: this screen's subject is the machine, not a session."""

    async def list_sessions(self) -> list[object]:
        return []

    async def refresh_readiness(self) -> None:
        return None


class FakeClaudeDefault:
    """A scripted `ports.remote_control_default.RemoteControlDefaultPort` that remembers.

    A copy of the contract suite's fake rather than a shared import, because test support has
    no parameter for this capability yet and this task may not add one. It keeps the port's
    observable contract -- a write lands where the next read sees it, and neither verb raises
    -- which is all a screen may assume.
    """

    def __init__(self, value: RemoteControlDefault = RemoteControlDefault.PROVIDER_DEFAULT) -> None:
        self.value = value
        self.writes: list[RemoteControlDefault] = []

    async def read(self) -> RemoteControlDefault:
        return self.value

    async def write(self, value: RemoteControlDefault) -> None:
        self.writes.append(value)
        self.value = value


def _boundary(claude: object | None, codex: object | None) -> PrivateBotBoundary:
    boundary = build_private_bot(
        7, 11, backend=backend_for(sessions=_NoSessions(), host_remote_control=codex)
    )
    boundary.backend = replace(boundary.backend, claude_remote_control_default=claude)
    return boundary


def _rows(message) -> list[list[str]]:
    return [
        [unmarked(unpadded(button.text)) for button in row]
        for row in message.reply_markup.inline_keyboard
    ]


def _labels(message) -> list[str]:
    return [label for row in _rows(message) for label in row]


def _button(message, label: str) -> str:
    for row in message.reply_markup.inline_keyboard:
        for button in row:
            if unmarked(unpadded(button.text)) == label:
                return button.callback_data
    raise AssertionError(f"no {label!r} button among {_labels(message)}")


def _row(message, title: str) -> str:
    """The token on the row whose label starts with this provider's title."""
    for label in _labels(message):
        if label.startswith(title):
            return _button(message, label)
    raise AssertionError(f"no {title!r} row among {_labels(message)}")


def _claude_label(message) -> str:
    for label in _labels(message):
        if label.startswith(REMOTE_CONTROL_DEFAULT_TITLE):
            return label
    raise AssertionError(f"no Claude row among {_labels(message)}")


async def test_settings_opens_with_both_providers_reading_their_own_value() -> None:
    claude = FakeClaudeDefault(RemoteControlDefault.OFF)
    boundary = _boundary(claude, FakeHostRemoteControl(HostConnection.CONNECTED))
    chat = FakeChat()

    await boundary.settings_command(chat.message_update("/settings"), None)

    screen = chat.bot_messages[0]
    assert _claude_label(screen) == (
        f"{REMOTE_CONTROL_DEFAULT_TITLE}: {REMOTE_CONTROL_DEFAULT_LABELS[RemoteControlDefault.OFF]}"
    )
    assert f"{HOST_REMOTE_CONTROL_TITLE}: on" in _labels(screen), _labels(screen)
    assert claude.writes == [], "opening the screen must not change anything"
    assert len(chat.bot_messages) == 1, chat.transcript()


async def test_three_presses_of_the_claude_row_walk_the_cycle_and_come_home() -> None:
    """One screen, three presses, and every state seen once.

    Pressed on the anchor each time, which is the property the contract test cannot reach:
    each press re-renders the same message with a *fresh* token (DEC-011), so a row that
    cached its token across renders would answer the second press with "That screen has moved
    on" and the cycle would stop after one step.
    """
    claude = FakeClaudeDefault(RemoteControlDefault.ON)
    boundary = _boundary(claude, None)
    chat = FakeChat()

    await boundary.settings_command(chat.message_update("/settings"), None)
    anchor = chat.bot_messages[0].message_id
    seen = [_claude_label(chat.messages[anchor])]
    for _ in range(3):
        await boundary.callback(
            chat.press(_row(chat.messages[anchor], REMOTE_CONTROL_DEFAULT_TITLE)), None
        )
        seen.append(_claude_label(chat.messages[anchor]))

    assert claude.value is RemoteControlDefault.ON, claude.writes
    assert seen[0] == seen[-1], seen
    assert len(set(seen[:-1])) == 3, f"a press that did not move the row: {seen}"
    assert len(chat.bot_messages) == 1, chat.transcript()


async def test_the_codex_row_hands_the_owner_to_the_host_screen_it_already_had() -> None:
    """Two presses after the row, because the host screen confirms before it acts.

    Nothing here is new behaviour: the row is a door, and what is behind it is the shipped
    `/remote` screen with its own directions and its own caution (DEC-071 keeps the two
    vocabularies apart). What this pins is that the door opens onto that screen rather than
    onto a second toggle minted for Settings.
    """
    claude = FakeClaudeDefault(RemoteControlDefault.ON)
    codex = FakeHostRemoteControl(HostConnection.DISABLED)
    boundary = _boundary(claude, codex)
    chat = FakeChat()

    await boundary.settings_command(chat.message_update("/settings"), None)
    anchor = chat.bot_messages[0].message_id
    codex_row = _row(chat.messages[anchor], HOST_REMOTE_CONTROL_TITLE)
    await boundary.callback(chat.press(codex_row), None)
    assert ON in _labels(chat.messages[anchor]), _labels(chat.messages[anchor])
    await boundary.callback(chat.press(_button(chat.messages[anchor], ON)), None)
    assert codex.calls.count("set_state:active") == 0, "the direction press asks first"
    await boundary.callback(chat.press(_button(chat.messages[anchor], ON)), None)

    assert codex.connection is HostConnection.CONNECTED
    assert OFF in _labels(chat.messages[anchor]), "the host screen came back with its reading"
    assert claude.writes == [], "the Codex journey never wrote Claude's setting"
    assert len(chat.bot_messages) == 1, chat.transcript()


async def test_a_host_that_wired_neither_row_is_told_rather_than_left_pressing() -> None:
    boundary = _boundary(None, None)
    chat = FakeChat()

    await boundary.settings_command(chat.message_update("/settings"), None)

    screen = chat.bot_messages[0]
    assert f"{REMOTE_CONTROL_DEFAULT_TITLE} is unavailable." in screen.text, screen.text
    assert f"{HOST_REMOTE_CONTROL_TITLE} is unavailable." in screen.text, screen.text
    assert not any(
        label.startswith((REMOTE_CONTROL_DEFAULT_TITLE, HOST_REMOTE_CONTROL_TITLE))
        for label in _labels(screen)
    ), _labels(screen)


async def test_the_settings_screen_closes_with_the_navigation_bar() -> None:
    """DEC-032: the bar is appended at one choke point, so every screen ends with it."""
    boundary = _boundary(FakeClaudeDefault(), FakeHostRemoteControl(HostConnection.DISABLED))
    chat = FakeChat()

    await boundary.settings_command(chat.message_update("/settings"), None)

    rows = _rows(chat.bot_messages[0])
    assert rows[-1] == ["Sessions", "Launch"], rows
    assert len(rows[-2]) == 1 and "Back to sessions" in rows[-2][0], rows


async def test_back_from_settings_lands_on_the_sessions_list() -> None:
    boundary = _boundary(FakeClaudeDefault(), None)
    chat = FakeChat()

    await boundary.settings_command(chat.message_update("/settings"), None)
    anchor = chat.bot_messages[0].message_id
    back = next(
        _button(chat.messages[anchor], label)
        for label in _labels(chat.messages[anchor])
        if "Back to sessions" in label
    )
    await boundary.callback(chat.press(back), None)

    assert "Sessions" in chat.messages[anchor].text, chat.messages[anchor].text
    assert len(chat.bot_messages) == 1, chat.transcript()


async def test_the_settings_screen_marks_no_tab_on_the_navigation_bar() -> None:
    """It belongs to no flow, so marking one would say the owner is somewhere they are not."""
    boundary = _boundary(FakeClaudeDefault(), None)
    chat = FakeChat()

    await boundary.settings_command(chat.message_update("/settings"), None)

    assert _rows(chat.bot_messages[0])[-1] == ["Sessions", "Launch"]
    assert all("•" not in label for label in _labels(chat.bot_messages[0]))
