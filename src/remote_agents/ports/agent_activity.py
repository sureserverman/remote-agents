"""What an agent was last observed doing, in the vocabulary the owner is told it in."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ActivityKind(Enum):
    """The only things this service claims about an agent that has stopped working.

    Deliberately fewer than the events upstream emits. A hook carries a dozen error types and
    notification types; the owner is being told one of five things, and an event that does not
    answer "why did it stop" is dropped rather than mapped to the nearest neighbour.
    """

    COMPLETED = "completed"
    LIMIT_REACHED = "limit_reached"
    NEEDS_ANSWER = "needs_answer"
    ENDED = "ended"
    QUIET = "quiet"


class ActivityConfidence(Enum):
    """Whether the agent said this, or something guessed it from the outside.

    It exists so presentation can weaken its wording rather than flatten the difference.
    `Notification(idle_prompt)` is a sixty-second timer with recorded false positives and
    false negatives, and pane quiet is a heuristic by construction -- both are worth telling
    the owner and neither is worth telling them as a fact.
    """

    REPORTED = "reported"
    INFERRED = "inferred"


@dataclass(frozen=True, slots=True)
class AgentActivity:
    """One observation about one session, bounded and ready to render."""

    session_id: str
    kind: ActivityKind
    detail: str | None
    observed_at: datetime
    confidence: ActivityConfidence = ActivityConfidence.REPORTED
