"""The provider-level activity contract the notification pipeline may rely on."""

from remote_agents.ports.agent_activity import ActivitySource, activity_source_for


def test_claude_profiles_are_hook_exclusive() -> None:
    assert activity_source_for("claude") is ActivitySource.HOOK_EXCLUSIVE
    assert activity_source_for("claude-remote") is ActivitySource.HOOK_EXCLUSIVE


def test_codex_is_hybrid_until_its_hook_reports() -> None:
    assert activity_source_for("codex") is ActivitySource.HYBRID


def test_other_curated_profiles_remain_quiet_only() -> None:
    assert activity_source_for("opencode") is ActivitySource.QUIET_ONLY
    assert activity_source_for("cursor-agent") is ActivitySource.QUIET_ONLY
