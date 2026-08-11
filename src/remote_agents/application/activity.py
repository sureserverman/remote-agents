"""Read what the agent hooks spooled, and say what it means, once.

The spool is the boundary between a hook running inside somebody else's process and this
service. Everything on the far side is untrusted in the ordinary sense -- it may be absent,
truncated, or shaped differently by a newer agent release -- so every step here drops what it
cannot read rather than raising, and a record that reaches no mapping is dropped rather than
guessed at. Reporting the wrong reason an agent stopped is worse than reporting nothing: the
owner acts on these.

Draining deletes. A record turned into activity is gone from disk before it is returned, so a
service that restarts halfway through a delivery cannot tell the owner the same thing twice.
The cost is the opposite failure -- a crash between the delete and the send loses one
notification -- and that is the right way round for a message that says an agent is waiting.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from remote_agents.ports.agent_activity import ActivityConfidence, ActivityKind, AgentActivity

_LOG = logging.getLogger(__name__)

MAXIMUM_DETAIL_CHARACTERS = 240

# Only the reasons that answer "why did it stop". Every other value these fields can take --
# authentication_failed, auth_success, elicitation_*, and whatever upstream adds next -- is
# absent on purpose, and an event carrying one is dropped by the lookup below.
_STOP_FAILURES = {
    "rate_limit": ActivityKind.LIMIT_REACHED,
    "max_output_tokens": ActivityKind.LIMIT_REACHED,
}
_NOTIFICATIONS = {
    "permission_prompt": (ActivityKind.NEEDS_ANSWER, ActivityConfidence.REPORTED),
    "agent_needs_input": (ActivityKind.NEEDS_ANSWER, ActivityConfidence.REPORTED),
    "idle_prompt": (ActivityKind.NEEDS_ANSWER, ActivityConfidence.INFERRED),
}


def drain_activity(activity_directory: Path) -> tuple[AgentActivity, ...]:
    """Take every spooled record, return what it means, and leave the spool empty."""
    try:
        paths = sorted(activity_directory.glob("*.json"))
    except OSError:
        # A spool that does not exist yet is the ordinary case before the first managed
        # launch, and one that cannot be listed is a problem for the operator, not for the
        # poll that found it. Either way there is nothing to deliver this pass.
        return ()
    return tuple(sorted(_drained(paths), key=lambda activity: activity.observed_at))


def _drained(paths: list[Path]) -> Iterator[AgentActivity]:
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _LOG.warning("discarding an unreadable activity record")
            record = None
        finally:
            # Before the record is used, not after: a record that is read and then fails to
            # delete would be delivered again on the next pass, and repeating "your agent is
            # waiting" every sixty seconds is the storm this whole path is arranged to avoid.
            path.unlink(missing_ok=True)
        activity = _activity(record) if isinstance(record, dict) else None
        if activity is not None:
            yield activity


def _activity(record: dict) -> AgentActivity | None:
    """Map one spooled record onto the vocabulary, or onto nothing."""
    session_id = record.get("session_id")
    observed_at = _moment(record.get("observed_at"))
    if not isinstance(session_id, str) or observed_at is None:
        return None
    resolved = _kind(record.get("event"), record.get("reason"))
    if resolved is None:
        return None
    kind, confidence = resolved
    return AgentActivity(
        session_id=session_id,
        kind=kind,
        detail=_detail(record.get("detail")),
        observed_at=observed_at,
        confidence=confidence,
    )


def _kind(event: object, reason: object) -> tuple[ActivityKind, ActivityConfidence] | None:
    if event == "Stop":
        return ActivityKind.COMPLETED, ActivityConfidence.REPORTED
    if event == "SessionEnd":
        return ActivityKind.ENDED, ActivityConfidence.REPORTED
    if event == "StopFailure":
        kind = _STOP_FAILURES.get(reason) if isinstance(reason, str) else None
        return None if kind is None else (kind, ActivityConfidence.REPORTED)
    if event == "Notification" and isinstance(reason, str):
        return _NOTIFICATIONS.get(reason)
    return None


def _moment(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _detail(value: object) -> str | None:
    """Bound it again here rather than trusting the spool to have done it.

    The spool bounds what it writes, but this reads files a different process produced, and
    the one thing a notification must not do is carry an agent's entire last message into a
    Telegram message.
    """
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:MAXIMUM_DETAIL_CHARACTERS] if normalized else None
