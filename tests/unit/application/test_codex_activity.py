from __future__ import annotations

from datetime import UTC, datetime

from remote_agents.application.activity import _activity
from remote_agents.ports.agent_activity import ActivityKind


def test_codex_permission_request_needs_an_answer_without_payload_detail() -> None:
    activity = _activity(
        {
            "session_id": "codex-session",
            "event": "PermissionRequest",
            "reason": None,
            "detail": None,
            "observed_at": datetime.now(UTC).isoformat(),
            "kind": "needs_answer",
            "confidence": "reported",
        }
    )
    assert activity is not None
    assert activity.kind is ActivityKind.NEEDS_ANSWER
    assert activity.detail is None


# --- the title pattern is Codex's own declaration, handed in by composition (BL-131) -------


def test_the_watcher_reads_the_pattern_it_is_given_and_no_other() -> None:
    import re

    from remote_agents.application.activity import observe_codex_action_required

    invented = re.compile(r"^WAITING ")
    moment = datetime.now(UTC)

    asked, activity = observe_codex_action_required(
        False, session_id="s", title="WAITING on you", now=moment, pattern=invented
    )
    assert asked and activity is not None and activity.kind is ActivityKind.NEEDS_ANSWER
    unmoved, nothing = observe_codex_action_required(
        False,
        session_id="s",
        title="[ ! ] Action Required | remote-agents",
        now=moment,
        pattern=invented,
    )
    assert not unmoved and nothing is None


def test_composition_hands_the_watcher_codex_s_declared_pattern() -> None:
    from remote_agents.adapters.agents.codex.hooks import ACTION_REQUIRED_TITLE
    from remote_agents.adapters.agents.registry import profiles_watched_by_title

    assert dict(profiles_watched_by_title()) == {"codex": ACTION_REQUIRED_TITLE}
