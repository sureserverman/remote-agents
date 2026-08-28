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
        }
    )
    assert activity is not None
    assert activity.kind is ActivityKind.NEEDS_ANSWER
    assert activity.detail is None
