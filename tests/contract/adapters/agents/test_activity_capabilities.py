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


def test_other_curated_profiles_remain_quiet_only() -> None:
    assert activity_source_for("opencode") is ActivitySource.QUIET_ONLY
    assert activity_source_for("cursor-agent") is ActivitySource.QUIET_ONLY
    assert reported_activity_kinds_for("opencode") == frozenset()
