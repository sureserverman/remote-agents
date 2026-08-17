"""Choosing an agent launches it: the contract for a launch with no confirmation in front."""

from __future__ import annotations

import pytest
from fake_telegram import FakeChat

from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.adapters.telegram.wizard import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject

_PROJECT = "a" * 24


class _RecordingLauncher:
    """Records every LaunchCommand it is handed, so a repeat is observable rather than implied."""

    def __init__(self) -> None:
        self.commands: list[object] = []

    async def list_sessions(self):
        return []

    async def refresh_readiness(self) -> None:
        return None

    async def launch(self, command):
        self.commands.append(command)
        return None


def _boundary() -> tuple[PrivateBotBoundary, _RecordingLauncher]:
    launcher = _RecordingLauncher()
    return (
        PrivateBotBoundary(
            7,
            11,
            catalogue=(CatalogProject(_PROJECT, "Demo", "tests", "Registered"),),
            profiles=(ProfileAvailability("claude", True),),
            launcher=launcher,
        ),
        launcher,
    )


def _button(message, label: str) -> str:
    for row in message.reply_markup.inline_keyboard:
        for button in row:
            # Marker-stripped: a navigation-bar button carries it when the owner is
            # already inside that flow.
            if button.text.removeprefix("• ") == label:
                return button.callback_data
    raise AssertionError(f"no {label!r} button in {message.text!r}")


@pytest.mark.asyncio
async def test_pressing_an_agent_issues_exactly_one_instant_launch() -> None:
    """The owner request: choosing the agent starts the session, with no review step.

    Driven through `callback()` rather than `_reply_for`, so the token is minted by the
    screen that draws the button and claimed by the press — which is the part that has to
    hold, not the routing.
    """
    boundary, launcher = _boundary()
    chat = FakeChat()
    await boundary.launch_command(chat.message_update("/launch"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Demo")), None)

    await boundary.callback(chat.press(_button(chat.messages[anchor], "Claude")), None)

    assert len(launcher.commands) == 1
    assert str(launcher.commands[0].project_id) == _PROJECT
    assert str(launcher.commands[0].profile_id) == "claude"


@pytest.mark.asyncio
async def test_an_instant_launch_carries_no_label() -> None:
    """Naming a session is a later act from its own menu, so the launch itself is unnamed."""
    boundary, launcher = _boundary()
    chat = FakeChat()
    await boundary.launch_command(chat.message_update("/launch"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Demo")), None)

    await boundary.callback(chat.press(_button(chat.messages[anchor], "Claude")), None)

    assert launcher.commands[0].label is None


@pytest.mark.asyncio
async def test_a_second_instant_launch_press_is_dropped_not_serviced() -> None:
    """DEC-008, and the reason removing the confirmation is safe.

    The confirmation was never the thing preventing a double launch — the one-shot mutation
    claim is, and Sub-plan 1 made it durable rather than process-local. A double-tap on a
    twenty-second startup is the failure this pins: the repeat is dropped, and the launch
    already in flight is untouched.
    """
    boundary, launcher = _boundary()
    chat = FakeChat()
    await boundary.launch_command(chat.message_update("/launch"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Demo")), None)
    agent = _button(chat.messages[anchor], "Claude")

    await boundary.callback(chat.press(agent, on=anchor), None)
    await boundary.callback(chat.press(agent, on=anchor), None)

    assert len(launcher.commands) == 1, "a second press must not start a second session"


@pytest.mark.asyncio
async def test_an_instant_launch_tells_the_owner_it_is_running() -> None:
    """Up to twenty seconds pass before a profile reports ready.

    Telegram clears the button spinner the moment the query is answered, so without a notice
    on the action that now launches, the owner presses an agent and watches an unchanged
    screen. The notice moved here from the review step that used to carry it.
    """
    boundary, _ = _boundary()

    assert boundary._pending_notice("launch.profile") == (
        "Launching — waiting for the agent to become ready…"
    )
    assert boundary._pending_notice("launch.confirm") is None


@pytest.mark.asyncio
async def test_the_instant_launch_button_carries_a_one_shot_mutation_token() -> None:
    """The guarantee itself, asserted where a regression can be seen.

    The double-tap test above passes even with `mutation=True` removed from the agent button,
    because production answers callbacks sequentially (`concurrent_updates(False)`): the first
    press retires the message's keyboard and prunes its tokens, so the second press resolves
    to nothing and the launch is dropped by *pruning* rather than by the claim. That is a real
    protection and it is worth having — but it is not the one the stage rests on, and it does
    not survive the cases pruning cannot reach: a redelivered update, two presses racing
    across the window before the render lands, or a second process.

    So this pins the property directly — the token behind the agent button is a mutation
    token — and the claim below pins that a mutation is one-shot at the store, which is what
    holds when pruning does not.
    """
    boundary, _ = _boundary()
    chat = FakeChat()
    await boundary.launch_command(chat.message_update("/launch"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Demo")), None)
    token = _button(chat.messages[anchor], "Claude")

    state = boundary.callbacks.resolve(token, owner_id=7, chat_id=11, message_id=anchor)

    assert state is not None
    assert state.mutation is True, "the agent button must carry the one-shot, not a plain token"
    assert boundary.callbacks.claim_mutation(token, owner_id=7, chat_id=11, message_id=anchor)
    assert not boundary.callbacks.claim_mutation(
        token, owner_id=7, chat_id=11, message_id=anchor
    ), "a claimed mutation is never claimable twice, which is what survives a redelivery"
