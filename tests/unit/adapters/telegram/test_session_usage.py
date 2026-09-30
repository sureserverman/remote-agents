"""The session detail screen's usage lines: present, absent, and never load-bearing."""

from __future__ import annotations

from datetime import UTC, datetime
from html import unescape

import pytest
from backends import SessionUseCaseDouble, backend_for

from remote_agents.adapters.telegram.limits_block import WIDTH, limits_block
from remote_agents.adapters.telegram.presenters import MAX_TELEGRAM_TEXT_UNITS
from remote_agents.adapters.telegram.service import PrivateBotBoundary, build_private_bot
from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.session_views import LimitPart, LimitRow, LimitWindow
from remote_agents.domain.models import (
    ProfileId,
    ProjectId,
    SessionDisplayIdentity,
    SessionId,
    SessionRecord,
    SessionState,
)
from remote_agents.ports.agent_usage import (
    AgentLimits,
    AgentUsage,
    ContextWindow,
    LimitsAbsence,
    UsageWindow,
)

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
async def test_the_sessions_screen_carries_a_group_of_lines_per_agent() -> None:
    """Where a whole-agent fact belongs: on the screen about every session, not inside one."""
    rendered = await _account_boundary(
        _limits_reader(
            AgentLimits(
                ProfileId("claude"), (UsageWindow("5h", 2.0),), stale_source="status-line cache"
            ),
            AgentLimits(ProfileId("codex"), (UsageWindow("week", 61.0),)),
        )
    )._sessions_reply()

    # The `Plan limits` block: each agent's name over one `<code>` line per window, the fixed
    # 5h and week kinds always drawn, then the borrowed source and the age (DEC-061).
    assert rendered.text.endswith(
        "\n\n<b>Plan limits</b>\n"
        "claude\n"
        "<code>5h █░░░░░░░  2%</code>\n"
        "<code>wk ░░░░░░░░</code>\n"
        "<code>via status-line cache · live</code>\n"
        "codex\n"
        "<code>5h ░░░░░░░░</code>\n"
        "<code>wk █████░░░ 61%</code>\n"
        "<code>live</code>"
    )


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
    assert "codex\n<code>5h ░░░░░░░░</code>\n<code>wk █████░░░ 61%</code>" in text


@pytest.mark.asyncio
async def test_a_host_that_wired_no_limits_reader_renders_no_block() -> None:
    text = (await _account_boundary(None)._sessions_reply()).text

    assert "Sessions" in text
    assert "5h" not in text and "week" not in text


@pytest.mark.asyncio
async def test_a_limits_reader_that_raises_marks_every_agent_unreadable() -> None:
    """The screen's real content is the list of sessions and the way into each one.

    And the block stays: one that vanished on a failed read said "no limits to report", which
    is a different answer from "the read failed".
    """

    async def exploding() -> tuple[AgentLimits, ...]:
        raise RuntimeError("the provider changed its layout under an upgrade")

    rendered = await build_private_bot(
        OWNER,
        CHAT,
        backend=backend_for(sessions=_Launcher(), catalogue=(PROJECT,), limits=exploding),
        profiles=(
            ProfileAvailability("claude", True, None),
            ProfileAvailability("codex", False, "not_installed"),
        ),
    )._sessions_reply()

    assert "Sessions" in rendered.text
    assert rendered.keyboard
    assert rendered.text.endswith(
        "\n\n<b>Plan limits</b>\nclaude\n<code>unreadable</code>\ncodex\n<code>unreadable</code>"
    )
    # Without it, a guard that swallowed the exception and then rendered the exception's own
    # words in its place passed here -- proven by mutation.
    assert "provider changed" not in rendered.text and "RuntimeError" not in rendered.text


@pytest.mark.asyncio
async def test_an_agent_that_answered_nothing_keeps_its_row_and_names_its_silence() -> None:
    """The row set is the profile set's, as it is on the terminal (DEC-061, DEC-100).

    Reached whenever an agent answers with no windows -- Claude's borrowed cache past its
    thirty-minute fence -- and when its reader was never heard from at all.
    """
    text = (
        await _account_boundary(
            _limits_reader(AgentLimits(ProfileId("claude"), ()))
        )._sessions_reply()
    ).text

    assert text.endswith("\n\n<b>Plan limits</b>\nclaude\n<code>no reading yet</code>")


@pytest.mark.asyncio
async def test_no_heading_is_promised_when_there_is_nothing_under_it() -> None:
    """The heading is part of the block, so it goes when the block does.

    Reached on a host whose every agent publishes no limits at all: none has a row, and a bare
    "Plan limits" over nothing promises a block and delivers none.
    """
    rendered = await build_private_bot(
        OWNER,
        CHAT,
        backend=backend_for(
            sessions=_Launcher(),
            catalogue=(PROJECT,),
            limits=_limits_reader(
                AgentLimits(ProfileId("opencode"), absence=LimitsAbsence.NOT_REPORTED)
            ),
        ),
        profiles=(ProfileAvailability("opencode", True, None),),
    )._sessions_reply()

    assert "Plan limits" not in rendered.text


# --- the block on a phone ------------------------------------------------------------------

#: The widest a row's figures get: a spent window, a three-character countdown, the longest
#: pace words, the longest source this project stamps and a two-digit age.
_FULL = LimitRow(
    "claude",
    (
        LimitWindow("5h", 100, "59m"),
        LimitWindow("week", 100, "23h", expected_percent=0, pace_delta=100),
    ),
    borrowed="status-line cache",
    stale_for=None,
)
_UNDER = LimitRow(
    "codex",
    (LimitWindow("week", 0, "23h", expected_percent=100, pace_delta=-100),),
    borrowed="rollout file",
    stale_for=None,
)
_DATED = LimitRow(
    "claude",
    (LimitWindow("5h", 100, "59m"), LimitWindow("week", 100, "23h")),
    borrowed="status-line cache",
    stale_for="23h",
)
_SPLIT = LimitRow(
    "cursor-agent",
    (LimitWindow("month", 100, "29d", parts=(LimitPart("cursor", 100), LimitPart("other", 100))),),
    borrowed="Cursor API",
    stale_for=None,
)
_ABSENT = tuple(
    LimitRow(name, (), None, None, absence=phrase)
    for name, phrase in (
        ("claude", "no reading yet"),
        ("codex", "unreadable"),
        ("cursor-agent", "sign in to cursor-agent"),
        ("opencode", "unreadable"),
    )
)

_DAILY = LimitRow(
    "codex",
    (
        LimitWindow("5h", 100, "59m"),
        LimitWindow("week", 100, "23h", expected_percent=0, pace_delta=100),
        LimitWindow("day", 0, "23h", expected_percent=100, pace_delta=-100),
    ),
    borrowed="rollout file",
    stale_for=None,
)

_LAYOUTS = {
    "two agents": (_FULL, _UNDER),
    "a daily window": (_FULL, _DAILY),
    "four agents": (_FULL, _UNDER, _SPLIT, _ABSENT[3]),
    "a dated reading": (_DATED, _UNDER),
    "all absent": _ABSENT,
    "Cursor split": (_SPLIT,),
}


@pytest.mark.parametrize("layout", _LAYOUTS)
def test_the_limits_block_fits_a_phone_line(layout: str) -> None:
    """No monospace line outruns `WIDTH`, at the widest figures each layout can carry."""
    lines = limits_block(_LAYOUTS[layout]).split("\n")
    monospace = [
        unescape(line.removeprefix("<code>").removesuffix("</code>"))
        for line in lines
        if line.startswith("<code>")
    ]

    assert monospace, lines
    too_wide = [line for line in monospace if len(line) > WIDTH]
    assert not too_wide, too_wide


def test_the_limits_block_at_its_widest_is_a_line_of_exactly_the_phone_width() -> None:
    """The bound is met and not merely approached, so `WIDTH` cannot drift above the layout."""
    assert "<code>wk ░░░░░░░░   0% ↻ 23h ▼ 100 under</code>" in limits_block((_FULL, _UNDER))
    assert len("wk ░░░░░░░░   0% ↻ 23h ▼ 100 under") == WIDTH


def test_the_limits_block_of_four_agents_leaves_the_message_its_room() -> None:
    """Four agents at their widest are a small share of what one Telegram message may hold."""
    block = limits_block((_FULL, _UNDER, _SPLIT, _ABSENT[3]))

    assert len(block.encode("utf-16-le")) // 2 < MAX_TELEGRAM_TEXT_UNITS // 8


def test_the_limits_block_escapes_every_string_it_did_not_write() -> None:
    """A stray `<` in a name, a label or a source would cost the whole sessions message.

    Telegram refuses a message whose HTML does not parse, and this block is on the one screen
    that is the only way to reach a session (DEC-014).
    """
    rows = (
        LimitRow(
            "a<b>&gent",
            (LimitWindow("5<h", 10, "2h"), LimitWindow("week", 20, "3d")),
            borrowed="cache & <file>",
            stale_for=None,
        ),
        LimitRow(
            "split<er",
            (LimitWindow("month", 50, None, parts=(LimitPart("o<ther>", 5), LimitPart("&", 6))),),
            borrowed=None,
            stale_for=None,
        ),
        LimitRow("absent&", (), None, None, absence="sign in to <absent&>"),
    )

    block = limits_block(rows)

    ours = block.replace("<b>Plan limits</b>", "").replace("<code>", "").replace("</code>", "")
    assert "<" not in ours and ">" not in ours, ours
    assert not [part for part in ours.split("&")[1:] if not part.startswith(("lt;", "gt;", "amp;"))]
    for written in (
        "a&lt;b&gt;&amp;gent",
        "5&lt;h",
        "via cache &amp; &lt;file&gt; · live",
        "split&lt;er",
        "o&lt;ther&gt; 5% · &amp; 6%",
        "absent&amp;",
        "sign in to &lt;absent&amp;&gt;",
    ):
        assert written in block, written
