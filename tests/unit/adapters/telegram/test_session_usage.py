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
from remote_agents.ports.agent_usage import AgentLimits, AgentUsage, ContextWindow, UsageWindow

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

    # The redesign's fact line: a padded label, then the gauge first and the counts after it.
    assert "<code>context  █░░░░░░░ 9% · 24.3k / 258k</code>" in text


@pytest.mark.asyncio
async def test_the_detail_screen_no_longer_claims_the_accounts_limits() -> None:
    """The owner's report: a window here reads as this session's spend, and it never was."""
    text = await _detail_text(
        _reader(AgentUsage(context=ContextWindow(1_000), windows=(UsageWindow("5h", 2.0),)))
    )

    assert "Limits" not in text
    assert "5h" not in text


@pytest.mark.asyncio
async def test_the_usage_lines_sit_below_the_state_they_are_context_for() -> None:
    """What a session *is* comes first; what it has spent is for a reader who has read that."""
    text = await _detail_text(_reader(AgentUsage(context=ContextWindow(1_000))))

    assert text.index("🟢 running") < text.index("<code>context")


@pytest.mark.asyncio
async def test_a_host_that_wired_no_reader_renders_no_usage_line_at_all() -> None:
    """Absence is the answer, not a row telling the owner the host is missing something."""
    text = await _detail_text(None)

    assert "context" not in text and "Usage" not in text


@pytest.mark.asyncio
async def test_a_provider_publishing_nothing_says_so_on_the_screen() -> None:
    text = await _detail_text(_reader(AgentUsage()))

    assert "<code>context  not reported by this agent</code>" in text


@pytest.mark.asyncio
async def test_a_reader_that_raises_costs_the_line_and_not_the_screen() -> None:
    """The screen's real content is the session's state and its stop actions."""

    async def exploding(session_id: SessionId) -> AgentUsage | None:
        raise RuntimeError("the provider changed its layout under an upgrade")

    text = await _detail_text(exploding)

    assert "<code>🟢 running" in text
    assert "context" not in text


# --- the account block, on the screen that is about every session -------------------------


def _account_boundary(limits: object) -> PrivateBotBoundary:
    return build_private_bot(
        OWNER,
        CHAT,
        backend=backend_for(sessions=_Launcher(), catalogue=(PROJECT,), limits=limits),
        profiles=(ProfileAvailability("claude", True, None),),
    )


class _NoSessions(_Launcher):
    """The empty branch, which is exactly when a stop has just ended the last session."""

    async def list_sessions(self) -> list[SessionRecord]:
        return []


def _limits_reader(*entries: AgentLimits):
    async def read() -> tuple[AgentLimits, ...]:
        return entries

    return read


@pytest.mark.asyncio
async def test_the_sessions_screen_carries_one_line_per_answering_agent() -> None:
    """Where a whole-agent fact belongs: on the screen about every session, not inside one."""
    rendered = await _account_boundary(
        _limits_reader(
            AgentLimits(
                ProfileId("claude"), (UsageWindow("5h", 2.0),), stale_source="status-line cache"
            ),
            AgentLimits(ProfileId("codex"), (UsageWindow("week", 61.0),)),
        )
    )._sessions_reply()

    # The `Plan limits` block: names padded to the longest plus two inside `<code>`, the
    # borrowed source disclosed (DEC-061), no reset countdowns on the phone.
    assert "<b>Plan limits</b>" in rendered.text
    assert "<code>claude  5h 2% · via status-line cache</code>" in rendered.text
    assert "<code>codex   week 61%</code>" in rendered.text


@pytest.mark.asyncio
async def test_an_empty_list_still_carries_the_agents_limits() -> None:
    empty = build_private_bot(
        OWNER,
        CHAT,
        backend=backend_for(
            sessions=_NoSessions(),
            catalogue=(PROJECT,),
            limits=_limits_reader(AgentLimits(ProfileId("codex"), (UsageWindow("week", 61.0),))),
        ),
        profiles=(ProfileAvailability("claude", True, None),),
    )

    text = (await empty._sessions_reply()).text

    assert "Nothing is running." in text
    assert "<code>codex  week 61%</code>" in text


@pytest.mark.asyncio
async def test_a_host_that_wired_no_limits_reader_renders_no_block() -> None:
    text = (await _account_boundary(None)._sessions_reply()).text

    assert "Sessions" in text
    assert "5h" not in text and "week" not in text


@pytest.mark.asyncio
async def test_a_limits_reader_that_raises_costs_the_block_and_not_the_screen() -> None:
    """The screen's real content is the list of sessions and the way into each one."""

    async def exploding() -> tuple[AgentLimits, ...]:
        raise RuntimeError("the provider changed its layout under an upgrade")

    rendered = await _account_boundary(exploding)._sessions_reply()

    assert "Sessions" in rendered.text
    assert rendered.keyboard
    # The assertion the test is named for. Without it, a guard that swallowed the exception and
    # then rendered a diagnostic line in its place passed here -- proven by mutation.
    assert "Plan limits" not in rendered.text
    assert "limits" not in rendered.text.replace("Plan limits", "")


@pytest.mark.asyncio
async def test_no_heading_is_promised_when_there_is_nothing_under_it() -> None:
    """The heading is part of the block, so it goes when the block does.

    Reached whenever every agent answers with no windows -- Claude's borrowed cache past its
    thirty-minute fence, codex quiet -- which is the same routine state the TUI pane's empty
    sentence exists for. A bare "Plan limits" over nothing promises a block and delivers none,
    and puts the two surfaces back into disagreement at the instant one of them says so.
    """
    text = (
        await _account_boundary(
            _limits_reader(AgentLimits(ProfileId("claude"), ()))
        )._sessions_reply()
    ).text

    assert "Plan limits" not in text
