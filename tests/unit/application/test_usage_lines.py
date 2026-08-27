"""How a session's spend is worded, including the two ways it can be absent."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from remote_agents.application.session_views import usage_lines
from remote_agents.ports.agent_usage import AgentUsage, ContextWindow, UsageWindow


def _soon(**offset: int) -> datetime:
    return datetime.now(UTC) + timedelta(**offset)


def _limits(usage: AgentUsage) -> str:
    """The limits line alone, for the tests that are about its wording and not the screen."""
    return next(line for line in usage_lines(usage) if line.startswith("Limits:"))


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


def test_limit_windows_render_with_the_time_left_on_each() -> None:
    usage = AgentUsage(
        windows=(
            UsageWindow("5h", 2.0, _soon(hours=3)),
            UsageWindow("week", 88.0, _soon(days=4)),
        )
    )

    assert _limits(usage) == "Limits: 5h 2% (resets in 2h) · week 88% (resets in 3d)"


def test_a_borrowed_figure_says_where_it_came_from() -> None:
    """Its freshness depends on a script this project does not own, so it is never shown bare."""
    usage = AgentUsage(
        windows=(UsageWindow("5h", 4.0),),
        stale_source="status-line cache",
    )

    assert _limits(usage) == "Limits: 5h 4% — via status-line cache"


def test_a_measured_figure_claims_no_source() -> None:
    usage = AgentUsage(windows=(UsageWindow("5h", 4.0),))

    assert _limits(usage) == "Limits: 5h 4%"


def test_context_and_limits_are_separate_lines() -> None:
    both = AgentUsage(context=ContextWindow(1_000), windows=(UsageWindow("5h", 1.0),))

    assert len(usage_lines(both)) == 2
    assert len(usage_lines(AgentUsage(context=ContextWindow(1_000)))) == 1


def test_limits_without_a_context_say_which_half_is_missing() -> None:
    """Claude's limits are account-wide and answer while its transcript has not been matched.

    Without this the screen shows a `Limits` line with no `Context` above it, and the reader
    has to work out for themselves whether the context is zero, unavailable, or not yet known.
    """
    lines = usage_lines(AgentUsage(windows=(UsageWindow("5h", 1.0),)))

    assert lines[0] == "Context: no conversation matched yet."
    assert len(lines) == 2


def test_a_window_that_has_already_reset_reads_as_zero_rather_than_negative() -> None:
    """A clock the reader cannot see disagreeing must not look like a broken session."""
    usage = AgentUsage(windows=(UsageWindow("5h", 0.0, datetime.now(UTC) - timedelta(hours=1)),))

    assert _limits(usage) == "Limits: 5h 0% (resets in 0m)"


def test_nothing_here_escapes_or_measures_anything() -> None:
    """`session_views` renders; the presenter escapes. DEC-014 keeps that boundary per surface."""
    usage = AgentUsage(context=ContextWindow(1_000))

    assert all("<" not in line and "&" not in line for line in usage_lines(usage))
