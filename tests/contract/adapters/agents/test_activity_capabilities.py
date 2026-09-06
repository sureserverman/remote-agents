"""The provider-level activity contract the notification pipeline may rely on."""

from remote_agents.ports.agent_activity import (
    ActivityKind,
    ActivitySource,
    activity_source_for,
    reported_activity_kinds_for,
)


def test_claude_profiles_are_hook_exclusive() -> None:
    assert activity_source_for("claude") is ActivitySource.HOOK_EXCLUSIVE
    assert activity_source_for("claude-remote") is ActivitySource.HOOK_EXCLUSIVE


def test_codex_is_hybrid_until_its_hook_reports() -> None:
    assert activity_source_for("codex") is ActivitySource.HYBRID
    assert reported_activity_kinds_for("codex") == {
        ActivityKind.COMPLETED,
        ActivityKind.NEEDS_ANSWER,
    }
    assert ActivityKind.LIMIT_REACHED not in reported_activity_kinds_for("codex")
    assert ActivityKind.OUTPUT_LIMIT not in reported_activity_kinds_for("codex")


def test_opencode_reports_through_its_own_plugin() -> None:
    """Hook-exclusive since 2026-09-06, and the two kinds its measured events can carry.

    This assertion used to read `UNOBSERVED`, with a docstring accepting that as the honest
    state after the pane-digest watch was retired. The acceptance was honest and the reasoning
    was incomplete: nothing observed OpenCode because nobody had measured what OpenCode
    publishes, not because it publishes nothing. `docs/acceptance-2026-09-06-opencode-activity.md`
    measured it, and a generated plugin now reports `session.idle` and `permission.asked`.

    Neither limit kind is claimed. That part of the old reasoning survives unchanged: OpenCode
    has no `StopFailure` equivalent, and a rate limit is not something to guess from a pane.
    """
    assert activity_source_for("opencode") is ActivitySource.HOOK_EXCLUSIVE
    assert reported_activity_kinds_for("opencode") == {
        ActivityKind.COMPLETED,
        ActivityKind.NEEDS_ANSWER,
    }
    assert ActivityKind.LIMIT_REACHED not in reported_activity_kinds_for("opencode")
    assert ActivityKind.OUTPUT_LIMIT not in reported_activity_kinds_for("opencode")


def test_cursor_is_observed_by_nothing() -> None:
    """Retiring the pane-digest watch left this profile with no activity source at all.

    Accepted on 2026-08-30 rather than worked around: `cursor-agent` publishes no hooks and
    carries no title marker, so the only signal it ever had was a guess about a pane that had
    stopped changing. Reporting nothing about it is the honest state, and this contract is where
    it is stated rather than discovered. Unlike OpenCode's, this absence is not one a
    measurement could close -- there is nothing published to measure.
    """
    assert activity_source_for("cursor-agent") is ActivitySource.UNOBSERVED
    assert reported_activity_kinds_for("cursor-agent") == frozenset()
