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


def test_other_curated_profiles_are_observed_by_nothing() -> None:
    """Retiring the pane-digest watch left these profiles with no activity source at all.

    Accepted on 2026-08-30 rather than worked around: `opencode` and `cursor-agent` publish no
    hooks and carry no title marker, so the only signal they ever had was a guess about a pane
    that had stopped changing. Reporting nothing about them is the honest state, and this
    contract is where it is stated rather than discovered.
    """
    assert activity_source_for("opencode") is ActivitySource.UNOBSERVED
    assert activity_source_for("cursor-agent") is ActivitySource.UNOBSERVED
    assert reported_activity_kinds_for("opencode") == frozenset()
