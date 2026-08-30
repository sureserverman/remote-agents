"""What each provider contributes to activity, now that nothing is watched by pane digest."""

from __future__ import annotations

from remote_agents.ports.agent_activity import (
    ActivityKind,
    ActivitySource,
    activity_source_for,
)


def test_the_vocabulary_no_longer_carries_a_quiet_kind() -> None:
    """`quiet` was the one kind nothing ever said. It is retired, not reworded.

    Asserted on the enum itself rather than on a renderer, because a kind that cannot be
    constructed cannot then be stored, grouped, rate-limited or delivered by mistake further
    down -- which is the whole reason the retirement happens here.
    """
    assert not hasattr(ActivityKind, "QUIET")
    assert "quiet" not in {kind.value for kind in ActivityKind}


def test_hooked_providers_keep_the_sources_that_describe_them() -> None:
    assert activity_source_for("claude") is ActivitySource.HOOK_EXCLUSIVE
    assert activity_source_for("claude-remote") is ActivitySource.HOOK_EXCLUSIVE
    assert activity_source_for("codex") is ActivitySource.HYBRID


def test_a_provider_with_neither_hooks_nor_a_title_watch_contributes_nothing() -> None:
    """The member that replaces `QUIET_ONLY`, and the reason it is not simply `HYBRID`.

    `opencode` and `cursor-agent` publish no hooks and have no title marker, so after the pane
    digest goes there is nothing left that could observe them. Classifying them as anything the
    watcher polls would cost a tmux capture per pass for an observation that can never be made.
    """
    assert activity_source_for("opencode") is ActivitySource.UNOBSERVED
    assert activity_source_for("cursor-agent") is ActivitySource.UNOBSERVED
    assert activity_source_for("something-nobody-curated") is ActivitySource.UNOBSERVED


def test_no_source_still_describes_a_pane_digest_watch() -> None:
    assert not hasattr(ActivitySource, "QUIET_ONLY")
    assert {source.value for source in ActivitySource} == {
        "hook_exclusive",
        "hybrid",
        "unobserved",
    }
