"""The bot's Permissions action: a running Codex session's mode, changed from its screen.

One action and one pick (DEC-033): the session's block carries Permissions, which opens the
three live choices (OD-3) with the current one marked. A pick asks `Backend.change_session_
permissions` once -- a redelivered press runs nothing -- and the message becomes the shared
outcome sentence (DEC-091). The use case itself is pinned in
`tests/unit/application/test_permission_presets.py`; this file pins the surface.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from html import unescape
from uuid import UUID

from backends import SessionUseCaseDouble, backend_for
from fake_telegram import FakeChat

from remote_agents.adapters.telegram.presenters import unpadded
from remote_agents.adapters.telegram.service import build_private_bot
from remote_agents.application.permission_presets import (
    LIVE_CHOICES,
    PermissionChange,
    PermissionSettings,
    permission_change_sentence,
)
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.domain.profiles import PermissionPreset
from remote_agents.ports.permission_bindings import PermissionState

OWNER = 7
CHAT = 11
_SESSION = SessionId(UUID(int=1))


class _Setting:
    async def read(self) -> PermissionPreset:
        return PermissionPreset.APPROVE_FOR_ME

    async def write(self, preset: PermissionPreset) -> None:
        raise AssertionError("a live change never writes the Settings default")


class _Sessions(SessionUseCaseDouble):
    def __init__(self, record: SessionRecord, outcome: PermissionChange | None = None) -> None:
        self.record = record
        self.requested = PermissionPreset.ASK
        self.outcome = outcome
        self.changes: list[tuple[SessionId, PermissionPreset]] = []

    async def list_sessions(self):
        return [self.record]

    async def refresh_readiness(self) -> None:
        return None

    async def permission_preset(self, session_id):
        return PermissionPreset.ASK

    async def permission_state(self, session_id):
        return PermissionState(self.requested, self.requested, "d", None)

    async def change_session_permissions(self, session_id, preset):
        self.changes.append((session_id, preset))
        outcome = self.outcome or PermissionChange(True, preset)
        if outcome.changed:
            self.requested = preset
        return outcome


def _record(profile: str = "codex", state: SessionState = SessionState.RUNNING):
    return SessionRecord(
        _SESSION,
        ProjectId("p" * 24),
        ProfileId(profile),
        SessionDisplayIdentity("Demo", profile.title(), "regular", 1),
        state,
        datetime(2026, 11, 12, tzinfo=UTC),
    )


def _boundary(sessions: _Sessions):
    backend = replace(
        backend_for(sessions=sessions),
        permission_presets=PermissionSettings(
            _Setting(), profiles={ProfileId("codex"): tuple(PermissionPreset)}
        ),
    )
    return build_private_bot(
        OWNER,
        CHAT,
        backend=backend,
        profiles=(
            ProfileAvailability("codex", True, None),
            ProfileAvailability("claude", True, None),
        ),
    )


def _labels(message) -> list[str]:
    markup = getattr(message, "reply_markup", None)
    rows = getattr(markup, "inline_keyboard", ())
    return [unpadded(button.text) for row in rows for button in row]


def _token(message, label: str) -> str:
    for row in message.reply_markup.inline_keyboard:
        for button in row:
            if unpadded(button.text).endswith(label):
                return button.callback_data
    raise AssertionError(f"no {label!r} button in {_labels(message)}")


async def _detail(chat: FakeChat, boundary) -> int:
    await boundary.sessions_command(chat.message_update("/sessions"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(chat.press(_token(chat.messages[anchor], "Demo")), None)
    return anchor


async def _picker(chat: FakeChat, boundary) -> int:
    anchor = await _detail(chat, boundary)
    await boundary.callback(chat.press(_token(chat.messages[anchor], "Permissions")), None)
    return anchor


async def test_a_running_codex_session_offers_permissions() -> None:
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary(_Sessions(_record()))
    anchor = await _detail(chat, boundary)
    assert "Permissions" in [label.split(" ", 1)[-1] for label in _labels(chat.messages[anchor])]


async def test_a_claude_session_has_no_permissions_button() -> None:
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary(_Sessions(_record("claude")))
    anchor = await _detail(chat, boundary)
    assert not any(label.endswith("Permissions") for label in _labels(chat.messages[anchor]))


async def test_a_codex_session_that_is_not_running_has_no_permissions_button() -> None:
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary(_Sessions(_record(state=SessionState.STARTING)))
    anchor = await _detail(chat, boundary)
    assert not any(label.endswith("Permissions") for label in _labels(chat.messages[anchor]))


async def test_the_picker_offers_the_live_choices_and_marks_the_current_one() -> None:
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary(_Sessions(_record()))
    anchor = await _picker(chat, boundary)
    labels = _labels(chat.messages[anchor])
    assert "Ask me ✓" in labels
    assert "Approve for me" in labels and "Full access" in labels


async def test_the_keyboards_choices_are_exactly_the_live_choices() -> None:
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary(_Sessions(_record()))
    anchor = await _picker(chat, boundary)
    offered = [label.removesuffix(" ✓") for label in _labels(chat.messages[anchor])]
    live = [choice.label for choice in LIVE_CHOICES]
    assert [label for label in offered if label in {c.label for c in LIVE_CHOICES}] == live
    assert "Use Codex configuration" not in offered
    assert not any("default" in label.lower() for label in offered)


async def test_a_pick_changes_once_even_when_the_press_is_redelivered() -> None:
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    sessions = _Sessions(_record())
    boundary = _boundary(sessions)
    anchor = await _picker(chat, boundary)
    token = _token(chat.messages[anchor], "Full access")
    await boundary.callback(chat.press(token), None)
    text = unescape(chat.messages[anchor].text)
    assert permission_change_sentence(PermissionChange(True, PermissionPreset.FULL_ACCESS)) in text
    await boundary.callback(chat.press(token), None)
    assert sessions.changes == [(_SESSION, PermissionPreset.FULL_ACCESS)]


async def test_a_failed_pick_says_not_changed_with_the_reason_escaped() -> None:
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    outcome = PermissionChange(False, PermissionPreset.FULL_ACCESS, "daemon said <nope>")
    sessions = _Sessions(_record(), outcome)
    boundary = _boundary(sessions)
    anchor = await _picker(chat, boundary)
    await boundary.callback(chat.press(_token(chat.messages[anchor], "Full access")), None)
    raw = chat.messages[anchor].text
    assert "&lt;nope&gt;" in raw and "<nope>" not in raw
    assert "not changed: daemon said <nope>" in unescape(raw)


async def test_a_session_on_the_providers_configuration_is_refused_by_name() -> None:
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    outcome = PermissionChange(
        False,
        PermissionPreset.APPROVE_FOR_ME,
        "it uses Use Codex configuration; relaunch with a mode to change it live",
    )
    sessions = _Sessions(_record(), outcome)
    boundary = _boundary(sessions)
    anchor = await _picker(chat, boundary)
    await boundary.callback(chat.press(_token(chat.messages[anchor], "Approve for me")), None)
    assert "relaunch with a mode" in unescape(chat.messages[anchor].text)
