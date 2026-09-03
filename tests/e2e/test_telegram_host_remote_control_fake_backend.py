"""The owner turns this machine's Codex Remote Control on from their phone, end to end.

Driven through the chat rather than through the handlers: `/remote` is typed, the button is
pressed on the message actually carrying it, and the confirmation is pressed on the screen the
first press drew. What that adds over the contract tests beside it is the wiring -- the command
handler, the callback dispatch, and the live view's one-message rule -- none of which a direct
call to a reply method can fail on.
"""

from __future__ import annotations

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
from remote_agents.domain.models import ProfileId
from remote_agents.domain.remote_control import HostConnection, RemoteControlState
from remote_agents.ports.agent_usage import AgentLimits, UsageWindow

ON = HOST_REMOTE_CONTROL_LABELS[RemoteControlState.ACTIVE]
OFF = HOST_REMOTE_CONTROL_LABELS[RemoteControlState.INACTIVE]


class _NoSessions(SessionUseCaseDouble):
    """A host with nothing running, which is exactly when the account blocks matter most."""

    async def list_sessions(self) -> list[object]:
        return []

    async def refresh_readiness(self) -> None:
        return None


async def _one_limit() -> tuple[AgentLimits, ...]:
    return (AgentLimits(ProfileId("codex"), (UsageWindow("week", 61.0),)),)


def _boundary(control: object | None) -> PrivateBotBoundary:
    return build_private_bot(
        7,
        11,
        backend=backend_for(sessions=_NoSessions(), limits=_one_limit, host_remote_control=control),
    )


def _labels(message) -> list[str]:
    return [
        unmarked(unpadded(button.text))
        for row in message.reply_markup.inline_keyboard
        for button in row
    ]


def _button(message, label: str) -> str:
    for row in message.reply_markup.inline_keyboard:
        for button in row:
            if unmarked(unpadded(button.text)) == label:
                return button.callback_data
    raise AssertionError(f"no {label!r} button among {_labels(message)}")


async def test_the_owner_turns_this_machine_on_and_the_chat_keeps_one_screen() -> None:
    control = FakeHostRemoteControl(HostConnection.DISABLED)
    boundary = _boundary(control)
    chat = FakeChat()

    await boundary.remote_command(chat.message_update("/remote"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(chat.press(_button(chat.messages[anchor], ON)), None)
    assert control.calls == ["status"], "the first press asks; nothing has acted yet"
    await boundary.callback(chat.press(_button(chat.messages[anchor], ON)), None)

    assert control.connection is HostConnection.CONNECTED
    assert "on" in chat.messages[anchor].text, chat.messages[anchor].text
    assert OFF in _labels(chat.messages[anchor]), "the reading came back with the open direction"
    assert len(chat.bot_messages) == 1, chat.transcript()


async def test_cancelling_the_confirmation_leaves_the_machine_alone() -> None:
    control = FakeHostRemoteControl(HostConnection.DISABLED)
    boundary = _boundary(control)
    chat = FakeChat()

    await boundary.remote_command(chat.message_update("/remote"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(chat.press(_button(chat.messages[anchor], ON)), None)
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Cancel")), None)

    assert control.connection is HostConnection.DISABLED
    assert "set_state:active" not in control.calls
    assert ON in _labels(chat.messages[anchor]), "cancel lands back on the reading"


async def test_a_host_that_wired_no_toggle_is_told_rather_than_left_pressing() -> None:
    boundary = _boundary(None)
    chat = FakeChat()

    await boundary.remote_command(chat.message_update("/remote"), None)

    text = chat.bot_messages[0].text
    assert "unavailable" in text, text
    assert ON not in _labels(chat.bot_messages[0])


async def test_the_sessions_screen_reports_this_machine_beneath_the_plan_limits() -> None:
    boundary = _boundary(FakeHostRemoteControl(HostConnection.CONNECTED))
    chat = FakeChat()

    await boundary.sessions_command(chat.message_update("/sessions"), None)

    text = chat.bot_messages[0].text
    assert f"{HOST_REMOTE_CONTROL_TITLE} · on (Paisleys-Blender)" in text, text
    assert text.index("Plan limits") < text.index(HOST_REMOTE_CONTROL_TITLE)


async def test_help_names_the_host_toggle_only_where_this_composition_wired_one() -> None:
    wired, bare = _boundary(FakeHostRemoteControl()), _boundary(None)
    wired_chat, bare_chat = FakeChat(), FakeChat()

    await wired.help_command(wired_chat.message_update("/help"), None)
    await bare.help_command(bare_chat.message_update("/help"), None)

    assert HOST_REMOTE_CONTROL_TITLE in wired_chat.bot_messages[0].text
    assert HOST_REMOTE_CONTROL_TITLE not in bare_chat.bot_messages[0].text


async def test_a_pairing_code_is_never_put_back_at_the_bottom_of_the_chat() -> None:
    """Driven through the real handler, so the SERVICE's decision is what is under test.

    `LiveView.move_to_bottom` re-sends the last remembered screen once per
    activity-notification pass, with no press involved -- that is how the menu stays
    reachable below arriving notifications. The pairing reply was remembered like any other
    screen, so a pass re-sent the live code as a brand new message with its own push
    notification, under a line that said "shown once", and again on every pass after.

    Two earlier versions of this test could not see it. One handed `LiveView` the flag
    itself; the next computed the flag from the same constant the service reads. Both proved
    the mechanism could keep a secret while leaving the code that must ask it to unguarded.
    This one presses the real button through `boundary.callback` and then asks the real view
    to move, which is the sequence a person and a notification actually produce.
    """
    control = FakeHostRemoteControl(HostConnection.CONNECTED)
    boundary = _boundary(control)
    chat = FakeChat()

    await boundary.remote_command(chat.message_update("/remote"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Pair a phone")), None)

    shown = [message for message in chat.bot_messages if "ZZZZ-9999" in (message.text or "")]
    assert shown, "the code was never rendered, so this test proves nothing"
    identities = {message.message_id for message in shown}

    await boundary.view.move_to_bottom(chat.bot)

    # The *identity*, not the count. `move_to_bottom` sends and then deletes the old anchor,
    # so a re-send leaves the chat holding the same number of messages -- but a different,
    # newly delivered one, with its own push notification. Counting could never see that;
    # the message id is what changes.
    after = [message for message in chat.bot_messages if "ZZZZ-9999" in (message.text or "")]
    assert {message.message_id for message in after} == identities, (
        "the live view re-sent the pairing code as a new message, with no press: "
        f"was {sorted(identities)}, now {sorted(m.message_id for m in after)}"
    )
