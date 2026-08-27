"""The session detail screen's usage lines: present, absent, and never load-bearing."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from backends import SessionUseCaseDouble, backend_for

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
from remote_agents.ports.agent_usage import AgentUsage, ContextWindow, UsageWindow

OWNER = 4242
CHAT = 99
SESSION = SessionId.parse("11111111-1111-4111-8111-111111111111")
PROJECT = CatalogProject("opaque-editor", "Demo", "/dev/demo", 0)


def _record() -> SessionRecord:
    return SessionRecord(
        session_id=SESSION,
        project_id=ProjectId("opaque-editor"),
        profile_id=ProfileId("claude"),
        display=SessionDisplayIdentity("Demo", "Claude", "regular", 1),
        state=SessionState.RUNNING,
        created_at=datetime(2026, 8, 27, 6, 0, tzinfo=UTC),
    )


class _Launcher(SessionUseCaseDouble):
    """One RUNNING session, so the detail screen has something to draw usage beneath."""

    async def list_sessions(self) -> list[SessionRecord]:
        return [_record()]

    async def refresh_readiness(self) -> None:
        return None


def _boundary(usage: object) -> PrivateBotBoundary:
    return build_private_bot(
        OWNER,
        CHAT,
        backend=backend_for(
            sessions=_Launcher(),
            catalogue=(PROJECT,),
            usage=usage,
        ),
        profiles=(ProfileAvailability("claude", True, None),),
    )


async def _detail_text(usage: object) -> str:
    rendered = await _boundary(usage)._detail_reply(str(SESSION))
    return rendered.text


def _reader(answer: AgentUsage | None):
    async def read(session_id: SessionId) -> AgentUsage | None:
        assert session_id == SESSION
        return answer

    return read


@pytest.mark.asyncio
async def test_the_detail_screen_shows_what_the_session_has_spent() -> None:
    text = await _detail_text(
        _reader(
            AgentUsage(
                context=ContextWindow(24_349, 258_400),
                windows=(UsageWindow("5h", 2.0),),
            )
        )
    )

    assert "Context: 24.3k of 258k · 9%" in text
    assert "Limits: 5h 2%" in text


@pytest.mark.asyncio
async def test_the_usage_lines_sit_below_the_state_they_are_context_for() -> None:
    """What a session *is* comes first; what it has spent is for a reader who has read that."""
    text = await _detail_text(_reader(AgentUsage(context=ContextWindow(1_000))))

    assert text.index("State:") < text.index("Context:")


@pytest.mark.asyncio
async def test_a_host_that_wired_no_reader_renders_no_usage_line_at_all() -> None:
    """Absence is the answer, not a row telling the owner the host is missing something."""
    text = await _detail_text(None)

    assert "Context" not in text and "Usage" not in text


@pytest.mark.asyncio
async def test_a_provider_publishing_nothing_says_so_on_the_screen() -> None:
    text = await _detail_text(_reader(AgentUsage()))

    assert "Usage: not reported by this agent." in text


@pytest.mark.asyncio
async def test_a_reader_that_raises_costs_the_line_and_not_the_screen() -> None:
    """The screen's real content is the session's state and its stop actions."""

    async def exploding(session_id: SessionId) -> AgentUsage | None:
        raise RuntimeError("the provider changed its layout under an upgrade")

    text = await _detail_text(exploding)

    assert "State: running" in text
    assert "Context" not in text
