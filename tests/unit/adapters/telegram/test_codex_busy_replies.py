"""The bot names a Codex Remote Control refusal instead of failing silently (BL-037).

A Codex launch and a Codex Remote Control change take one lock, because a change restarts the
daemon under a TUI still starting. When the other side holds it past the bound, the service
raises `CodexRemoteControlBusy` before anything is started. The token behind the pressed button
is already spent, so a reply that invites another press must carry a fresh button.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from backends import SessionUseCaseDouble, backend_for

from remote_agents.adapters.agents.registry import profile_names, profile_trust_dialogs
from remote_agents.adapters.telegram.service import PrivateBotBoundary, build_private_bot
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.host_remote_control import CodexRemoteControlBusy

OWNER = 7
CHAT = 11
PROJECT = CatalogProject("a" * 24, "Demo", "tests", "Registered")
UNTRUSTED = SessionRecord(
    SessionId(UUID(int=9)),
    ProjectId(PROJECT.opaque_id),
    ProfileId("codex"),
    SessionDisplayIdentity("Demo", "Codex", "regular", 1),
    SessionState.UNTRUSTED,
    datetime(2026, 10, 10, tzinfo=UTC),
)


class _Busy(SessionUseCaseDouble):
    """Every guarded call refused, as it is while a change holds the lock."""

    def __init__(self) -> None:
        self.launches = 0

    async def list_sessions(self):
        return [UNTRUSTED]

    async def get(self, session_id):
        return UNTRUSTED

    async def launch(self, _command):
        self.launches += 1
        raise CodexRemoteControlBusy("held")

    async def answer_trust(self, _command):
        raise CodexRemoteControlBusy("held")


class _BusyToggle:
    async def status(self):
        return None

    async def set_state(self, _command):
        raise CodexRemoteControlBusy("held")


def _boundary() -> PrivateBotBoundary:
    return build_private_bot(
        OWNER,
        CHAT,
        backend=backend_for(
            catalogue=(PROJECT,),
            sessions=_Busy(),
            host_remote_control=_BusyToggle(),
        ),
        profiles=(ProfileAvailability("codex", True, None),),
        names=profile_names(),
        trust_dialogs=profile_trust_dialogs(),
    )


def _claimed(boundary: PrivateBotBoundary, action: str, entity: str) -> str:
    token = boundary._callback(action, entity, mutation=True)
    boundary.callbacks.bind_pending(CHAT, 1)
    return token


def _labels(reply: dict[str, object]) -> list[str]:
    markup = reply["reply_markup"]
    keyboard = getattr(markup, "inline_keyboard", None) or getattr(markup, "keyboard", ())
    return [button.text for row in keyboard for button in row]


@pytest.mark.asyncio
async def test_a_refused_launch_says_why_and_offers_a_fresh_press() -> None:
    boundary = _boundary()
    entity = f"{PROJECT.opaque_id}|codex"
    reply = await boundary._launch_reply(entity, _claimed(boundary, "launch.profile", entity), 1)

    assert "Codex Remote Control is changing" in str(reply["text"])
    assert "Nothing was started" in str(reply["text"])
    assert "Launch again" in _labels(reply)


@pytest.mark.asyncio
async def test_a_refused_trust_answer_keeps_the_question_answerable() -> None:
    boundary = _boundary()
    entity = str(UNTRUSTED.session_id)
    reply = await boundary._trust_reply(entity, _claimed(boundary, "session.trust", entity), 1)

    assert "nothing was sent" in str(reply["text"])
    assert "Trust" in _labels(reply)


@pytest.mark.asyncio
async def test_a_refused_toggle_says_a_codex_session_is_still_starting() -> None:
    boundary = _boundary()
    reply = await boundary._host_remote_control_act_reply(
        "active", _claimed(boundary, "host.remote.act", "active"), 1
    )

    assert "A Codex session is still starting" in str(reply["text"])
    assert "not changed" in str(reply["text"])
