from __future__ import annotations

from remote_agents.application.activity import QuietWatch


def test_quiet_watch_keeps_only_digest_and_transition_state() -> None:
    watch = QuietWatch("digest", 2, seen_a_change=True, already_reported=False)
    assert watch.digest == "digest"
    assert watch.unchanged_polls == 2
