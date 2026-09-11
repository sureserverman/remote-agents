from datetime import UTC, datetime

import pytest
from backends import SessionUseCaseDouble, backend_for

from remote_agents.adapters.telegram.service import build_private_bot
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.remote_control import RemoteControlState


class Launcher(SessionUseCaseDouble):
    """A use case whose pane reads whatever the test armed, and records what it was told.

    `remote_control_state` is modelled here rather than in `SessionUseCaseDouble` on that
    class's own rule: it is an *action-time* read, not one of the three every detail makes
    while drawing, so a test that drives the toggle states its pane and a test that does not
    fails loudly instead of passing against a stub.
    """

    def __init__(
        self, record: SessionRecord, reading: RemoteControlState = RemoteControlState.UNKNOWN
    ) -> None:
        self.record = record
        self.reading = reading
        self.commands = []
        self.reads = 0

    async def list_sessions(self):
        return (self.record,)

    async def inspect(self, _query):
        return None

    async def remote_control_state(self, session_id):
        del session_id
        self.reads += 1
        return self.reading

    async def set_remote_control(self, command):
        self.commands.append(command)
        return command.desired_state


def _record(profile: str = "claude", state: SessionState = SessionState.RUNNING) -> SessionRecord:
    return SessionRecord(
        SessionId.new(),
        ProjectId("opaque-editor"),
        ProfileId(profile),
        SessionDisplayIdentity("opaque-editor", profile, "regular", 1),
        state,
        datetime.now(UTC),
    )


def _toggle_token(detail) -> str | None:
    """The one Remote Control button's callback, or None if the screen offers none."""
    tokens = [
        button.callback_data
        for row in detail.keyboard
        for button in row
        if button.text.endswith("Remote Control")
    ]
    assert len(tokens) <= 1, f"more than one Remote Control button: {tokens}"
    return tokens[0] if tokens else None


async def _confirm_for(boundary, launcher):
    """Press the toggle and return (the confirm screen, the mutation button's entity id)."""
    detail = await boundary._detail_reply(str(launcher.record.session_id))
    token = _toggle_token(detail)
    assert token is not None
    boundary.callbacks.bind_pending(11, 1)
    state = boundary.callbacks.resolve(token, owner_id=7, chat_id=11, message_id=1)
    assert state is not None and state.action == "remote.control"
    confirmation = await boundary._remote_control_confirm_reply(state.entity_id)
    boundary.callbacks.bind_pending(11, 1)
    mutation = boundary.callbacks.resolve(
        confirmation.keyboard[0][0].callback_data, owner_id=7, chat_id=11, message_id=1
    )
    assert mutation is not None
    return confirmation, mutation.entity_id


async def test_a_running_claude_session_carries_exactly_one_remote_control_button() -> None:
    launcher = Launcher(_record())
    boundary = build_private_bot(7, 11, backend=backend_for(sessions=launcher))

    detail = await boundary._detail_reply(str(launcher.record.session_id))

    labels = [button.text for row in detail.keyboard for button in row]
    assert [label for label in labels if "Remote Control" in label] == ["📡 Remote Control"]
    assert launcher.reads == 0, "drawing the detail must not capture the pane"


async def test_a_non_claude_session_carries_no_remote_control_button() -> None:
    launcher = Launcher(_record(profile="codex"))
    boundary = build_private_bot(7, 11, backend=backend_for(sessions=launcher))

    assert _toggle_token(await boundary._detail_reply(str(launcher.record.session_id))) is None


@pytest.mark.parametrize(
    ("reading", "carried", "sentence"),
    [
        (RemoteControlState.ACTIVE, "inactive", "Remote Control is on. Turn it off?"),
        (RemoteControlState.INACTIVE, "active", "Remote Control is off. Turn it on?"),
        (
            RemoteControlState.UNKNOWN,
            "active",
            "Remote Control could not be read. Turn it on?",
        ),
    ],
)
async def test_the_confirmation_names_the_direction_the_pane_read_implies(
    reading: RemoteControlState, carried: str, sentence: str
) -> None:
    """One button, and the reading decides what pressing it will mean.

    UNKNOWN offers *on*, which is the direction that was always safe from an unreadable
    pane (DEC-003): enabling sends one curated sequence, while disabling has to open
    Claude's status menu and arrow through it, and a menu opened against a pane that was
    not where we thought it was stays open.
    """
    launcher = Launcher(_record(), reading)
    boundary = build_private_bot(7, 11, backend=backend_for(sessions=launcher))

    confirmation, entity_id = await _confirm_for(boundary, launcher)

    assert launcher.reads == 1, "the confirmation must read the pane, once"
    assert sentence in confirmation.text
    assert entity_id.endswith(f"|{carried}")


async def test_the_confirmation_cancels_back_to_the_detail_it_came_from() -> None:
    launcher = Launcher(_record(), RemoteControlState.ACTIVE)
    boundary = build_private_bot(7, 11, backend=backend_for(sessions=launcher))

    confirmation, _ = await _confirm_for(boundary, launcher)

    cancel = next(
        button for row in confirmation.keyboard for button in row if button.text == "Cancel"
    )
    boundary.callbacks.bind_pending(11, 1)
    state = boundary.callbacks.resolve(cancel.callback_data, owner_id=7, chat_id=11, message_id=1)
    assert state is not None and state.action == "session.detail"
    assert state.entity_id == str(launcher.record.session_id)


async def test_claude_remote_control_requires_confirmation_and_uses_opaque_callbacks() -> None:
    """The whole press-confirm-mutate path, with the direction resolved from the reading.

    The mutation still carries an explicit ACTIVE or INACTIVE: the confirmation is where the
    toggle stops being a toggle, so `set_remote_control` never has to re-read a pane to find
    out what it was asked to do.
    """
    launcher = Launcher(_record(), RemoteControlState.INACTIVE)
    boundary = build_private_bot(7, 11, backend=backend_for(sessions=launcher))

    confirmation, entity_id = await _confirm_for(boundary, launcher)
    boundary.callbacks.bind_pending(11, 1)
    result = await boundary._remote_control_reply(
        entity_id, confirmation.keyboard[0][0].callback_data, 1
    )

    assert "Remote Control: active" in result["text"]
    assert launcher.commands[0].desired_state is RemoteControlState.ACTIVE
