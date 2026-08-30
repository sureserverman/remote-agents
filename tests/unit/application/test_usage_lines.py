"""How a session's spend is worded, including the two ways it can be absent."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from remote_agents.application.session_views import usage_lines
from remote_agents.ports.agent_usage import AgentUsage, ContextWindow, UsageWindow


def _soon(**offset: int) -> datetime:
    return datetime.now(UTC) + timedelta(**offset)


def test_an_unmatched_session_is_told_it_may_yet_match() -> None:
    """An agent that has not written its first turn has no file to find, and that resolves."""
    assert usage_lines(None) == ("Usage: no conversation matched yet.",)


def test_a_provider_that_publishes_nothing_is_not_told_to_wait() -> None:
    """cursor-agent will never write this down, so `yet` would be a promise nothing keeps."""
    assert usage_lines(AgentUsage()) == ("Usage: not reported by this agent.",)


def test_the_two_absences_are_worded_differently() -> None:
    """The distinction is the reason `AgentUsage()` and `None` are both representable."""
    assert usage_lines(None) != usage_lines(AgentUsage())


def test_a_context_with_a_stated_ceiling_carries_a_percentage() -> None:
    usage = AgentUsage(context=ContextWindow(24_349, 258_400))

    assert usage_lines(usage) == ("Context: 24.3k of 258k · 9%",)


def test_a_context_with_no_stated_ceiling_is_a_bare_count() -> None:
    """Deriving the ceiling from a model name would guess, and guess wrong on a model switch."""
    assert usage_lines(AgentUsage(context=ContextWindow(185_296))) == ("Context: 185k",)


def test_a_session_line_never_carries_the_plans_limits() -> None:
    """The move itself. A window is the account's, so a line under a session misreports it.

    The owner's words on 2026-08-29: the weekly limits "belong to the whole agent, not a
    particular session". `limit_lines` renders them once per agent instead, and the gate check
    `! grep -rn 'Limits: ' src/` is what proves none survived anywhere.
    """
    usage = AgentUsage(
        context=ContextWindow(24_349, 258_400),
        windows=(UsageWindow("5h", 2.0), UsageWindow("week", 88.0)),
        stale_source="status-line cache",
    )

    assert usage_lines(usage) == ("Context: 24.3k of 258k · 9%",)


def test_windows_on_a_session_reading_are_ignored_rather_than_rendered() -> None:
    """A reader may still carry them; this function stopped being the place they are shown."""
    assert usage_lines(
        AgentUsage(context=ContextWindow(1_000), windows=(UsageWindow("5h", 2.0),))
    ) == ("Context: 1.0k",)


def test_nothing_here_escapes_or_measures_anything() -> None:
    """`session_views` renders; the presenter escapes. DEC-014 keeps that boundary per surface."""
    usage = AgentUsage(context=ContextWindow(1_000))

    assert all("<" not in line and "&" not in line for line in usage_lines(usage))


def test_a_reading_with_windows_but_no_context_is_not_called_permanent() -> None:
    """ "Not reported by this agent" is the permanent sentence; this state resolves next turn.

    Guarded here as well as at the reader that produces it, because the collapse happened when
    the windows-shaped branch was deleted and nothing downstream noticed the two absences had
    become one sentence.
    """
    transient = AgentUsage(context=None, windows=(UsageWindow("5h", 2.0),))

    assert usage_lines(transient) != usage_lines(AgentUsage())
