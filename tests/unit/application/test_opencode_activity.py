"""The drain's OpenCode vocabulary: two event names, two kinds, and a detail that never comes.

The spool admits `session.idle` and `permission.asked`; this is the other half — the mapping
that turns a spooled record into the kind a surface renders. Without it the records land, are
read, and are dropped as events nothing can interpret, which is the exact silent failure
`_DISCRIMINATING_FIELDS`' comment records this project losing months to.
"""

from __future__ import annotations

from datetime import UTC, datetime

from remote_agents.application.activity import _activity
from remote_agents.ports.agent_activity import ActivityConfidence, ActivityKind


def _record(event: str, **overrides: object) -> dict:
    return {
        "session_id": "opencode-session",
        "event": event,
        "reason": None,
        "detail": None,
        "observed_at": datetime.now(UTC).isoformat(),
        **overrides,
    }


def test_session_idle_is_a_completion_the_agent_reported() -> None:
    """Not inferred: OpenCode's own plugin said the session went idle."""
    activity = _activity(_record("session.idle"))

    assert activity is not None
    assert activity.kind is ActivityKind.COMPLETED
    assert activity.confidence is ActivityConfidence.REPORTED
    assert activity.detail is None
    assert activity.ask is None


def test_permission_asked_needs_an_answer_and_names_the_class_of_ask() -> None:
    """The token survives the spool round trip and reaches the surfaces as `ask` (DEC-074)."""
    activity = _activity(_record("permission.asked", ask="bash"))

    assert activity is not None
    assert activity.kind is ActivityKind.NEEDS_ANSWER
    assert activity.confidence is ActivityConfidence.REPORTED
    assert activity.detail is None
    assert activity.ask == "bash"


def test_a_completion_carrying_a_detail_from_somewhere_else_still_renders_it() -> None:
    """The drain is provider-blind on purpose; the *spool* is where OpenCode's detail is refused.

    Asserted rather than assumed, because it is the reason the "OpenCode never has a detail"
    claim is made about the spool and never about this function. A rule enforced in two places
    is a rule that can disagree with itself, so it is enforced in one.
    """
    activity = _activity(_record("session.idle", detail="words from a foreign writer"))

    assert activity is not None
    assert activity.detail == "words from a foreign writer"


def test_permission_replied_is_not_a_kind_this_project_has() -> None:
    """It fired and was captured; nothing here needs to know how the owner answered."""
    assert _activity(_record("permission.replied")) is None
