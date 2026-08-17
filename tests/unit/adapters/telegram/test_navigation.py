"""The fixed navigation bar every bot screen closes with.

`_message` is the one place a screen's closing row is built, so these drive real screens
through the boundary rather than asserting on that helper directly: a screen that stopped
routing through it would still pass a direct test of it, and that screen is exactly the
regression worth catching.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from fake_telegram import FakeChat

from remote_agents.adapters.telegram.service import PrivateBotBoundary
from remote_agents.adapters.telegram.wizard import ProfileAvailability
from remote_agents.application.conversations import ConversationService
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.conversations import (
    ConversationCataloguePage,
    ConversationReference,
    ConversationState,
    ConversationSummary,
    ProfileResumeCapability,
    ProviderConversationId,
    ResolvedConversation,
)
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)

OWNER = 7
CHAT = 11
PROJECT = CatalogProject("a" * 24, "Demo", "tests", "Registered")


class _Catalogue:
    def __init__(self, resolved: ResolvedConversation) -> None:
        self.resolved = resolved

    async def list_conversations(self, **_kwargs) -> ConversationCataloguePage:
        return ConversationCataloguePage((self.resolved.summary,), 1, 1)

    async def resolve_conversation(self, reference: ConversationReference):
        return self.resolved if reference == self.resolved.summary.reference else None

    async def resume_capabilities(self):
        return (ProfileResumeCapability(ProfileId("claude"), True, True),)


class _Launcher:
    """One RUNNING session, so the list and the detail both have something to draw."""

    def __init__(self, record: SessionRecord) -> None:
        self.record = record

    async def list_sessions(self):
        return [self.record]

    async def refresh_readiness(self) -> None:
        return None


def _record() -> SessionRecord:
    return SessionRecord(
        SessionId(UUID(int=1)),
        ProjectId(PROJECT.opaque_id),
        ProfileId("claude"),
        SessionDisplayIdentity("Demo", "Claude", "regular", 1),
        SessionState.RUNNING,
        datetime(2026, 8, 17, tzinfo=UTC),
    )


def _resolved() -> ResolvedConversation:
    return ResolvedConversation(
        ConversationSummary(
            ConversationReference("c-0123456789abcdef"),
            ProfileId("claude"),
            ProjectId(PROJECT.opaque_id),
            ConversationState.RESUMABLE,
            datetime(2026, 8, 2, tzinfo=UTC),
        ),
        ProviderConversationId("provider-id"),
    )


def _boundary(*, with_resume: bool = True) -> PrivateBotBoundary:
    return PrivateBotBoundary(
        OWNER,
        CHAT,
        catalogue=(PROJECT,),
        profiles=(ProfileAvailability("claude", True, None),),
        launcher=_Launcher(_record()),
        conversations=ConversationService(_Catalogue(_resolved())) if with_resume else None,
    )


def _rows(message) -> list[list[str]]:
    return [[button.text for button in row] for row in message.reply_markup.inline_keyboard]


def _button(message, label: str) -> str:
    for row in message.reply_markup.inline_keyboard:
        for button in row:
            if button.text == label or button.text.startswith(label):
                return button.callback_data
    raise AssertionError(f"no {label!r} button in {message.text!r}")


async def _every_screen(chat: FakeChat, boundary: PrivateBotBoundary) -> list[object]:
    """Drive a screen from each family the bot draws, returning what the owner would see."""
    seen = []
    await boundary.sessions_command(chat.message_update("/sessions"), None)
    anchor = chat.bot_messages[0].message_id
    seen.append(chat.messages[anchor])

    await boundary.callback(chat.press(_button(chat.messages[anchor], "Demo")), None)
    seen.append(chat.messages[anchor])

    await boundary.launch_command(chat.message_update("/launch"), None)
    seen.append(chat.messages[anchor])

    await boundary.help_command(chat.message_update("/help"), None)
    seen.append(chat.messages[anchor])
    return seen


@pytest.mark.asyncio
async def test_the_navigation_bar_closes_every_screen_that_carries_a_keyboard() -> None:
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary()

    for screen in await _every_screen(chat, boundary):
        assert _rows(screen)[-1] == ["Sessions", "Launch", "Resume"], (
            f"screen {screen.text!r} does not close with the navigation bar"
        )


@pytest.mark.asyncio
async def test_the_navigation_bar_omits_resume_when_no_conversation_service_is_wired() -> None:
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary(with_resume=False)

    for screen in await _every_screen(chat, boundary):
        assert _rows(screen)[-1] == ["Sessions", "Launch"]


@pytest.mark.asyncio
async def test_the_navigation_bar_keeps_back_on_its_own_row_above_it() -> None:
    """Back is context-dependent and the bar is not; merging them would make the bar mean
    something different on every screen, which is the whole property it exists to have."""
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary()

    await boundary.sessions_command(chat.message_update("/sessions"), None)
    anchor = chat.bot_messages[0].message_id
    await boundary.callback(chat.press(_button(chat.messages[anchor], "Demo")), None)

    rows = _rows(chat.messages[anchor])
    assert rows[-1] == ["Sessions", "Launch", "Resume"]
    assert rows[-2] == ["Back"]


@pytest.mark.asyncio
async def test_the_navigation_bar_replaced_the_home_button_on_every_screen() -> None:
    chat = FakeChat(chat_id=CHAT, owner_id=OWNER)
    boundary = _boundary()

    for screen in await _every_screen(chat, boundary):
        labels = {label for row in _rows(screen) for label in row}
        assert "Home" not in labels, f"screen {screen.text!r} still offers Home"
