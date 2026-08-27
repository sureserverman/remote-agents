"""What an agent has spent — its context window, and its plan's rate-limit windows.

Separate from `agent_activity` on purpose, and the difference is where the number comes from.
An activity is **pushed**: a hook fires, the spool records it, the drain turns it into a
message the owner is interrupted with. Usage is **pulled**: nobody reports it, it is read off
the provider's own working files at the moment a screen asks, and it is never news — a context
window that has grown is not an event worth a phone buzzing, it is a fact worth showing to
someone who has already opened the session.

That is also why nothing here is persisted. DEC-013 clause (2) says what a payload or a pane
carries is rendered and never stored; this is the same rule reached from the other side, and
for a stronger reason — a stored token count is stale the moment the agent takes another turn,
so storing it would be building a cache whose only guarantee is that it disagrees with the
thing it caches.

**Every field is optional, and that is the design rather than defensiveness.** The providers
disagree about what they write down, and the disagreement is not small: Codex records its
context *and* both rate-limit windows in its own rollout; Claude records its context but not
its limits; Cursor records neither. A type that made any of this required would force an
adapter to invent a number for a provider that does not publish one, which is the single thing
a usage reader must never do. Absent is a renderable answer; a guess is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from remote_agents.domain.models import ProfileId


@dataclass(frozen=True, slots=True)
class ContextWindow:
    """How much of a conversation's context the agent is currently carrying.

    `limit_tokens` is the model's window when the provider states it and `None` when it does
    not. Claude's transcript records what each turn *used* and never what the ceiling was, so
    the percentage that would make the number legible is exactly the part that is missing —
    presentation handles that by showing the count alone rather than by assuming a ceiling
    from a model name, which would be wrong the first time the owner switched models.
    """

    used_tokens: int
    limit_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.used_tokens < 0:
            raise ValueError("a context window cannot have used negative tokens")
        if self.limit_tokens is not None and self.limit_tokens < 1:
            raise ValueError("a context limit must be positive when it is known")

    @property
    def used_fraction(self) -> float | None:
        """The share of the window in use, or None when the ceiling is not known."""
        return None if self.limit_tokens is None else self.used_tokens / self.limit_tokens


@dataclass(frozen=True, slots=True)
class UsageWindow:
    """One of a plan's rate-limit windows, as the provider itself reports it.

    `label` is the provider's own window in the owner's words — "5h", "week" — rather than a
    duration this project derived, because the two providers that publish windows do not use
    the same ones and a normalised vocabulary would have to invent a mapping between plans it
    cannot see.

    `used_percent` is a percentage and not a fraction, matching what both sources emit, so no
    adapter has to remember which of the two conventions this type wanted.
    """

    label: str
    used_percent: float
    resets_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.label or self.label != self.label.strip():
            raise ValueError("a usage window needs a trimmed, non-empty label")
        if not 0 <= self.used_percent <= 100:
            raise ValueError("a usage window percentage must fall between 0 and 100")


@dataclass(frozen=True, slots=True)
class AgentUsage:
    """Everything one session's provider will say about what it has spent.

    An instance carrying neither a context window nor any usage window is legal and means the
    honest thing: this provider publishes nothing a reader can find. It is distinct from a
    reader returning `None`, which means the *session* could not be matched to a conversation
    at all — a difference the owner is entitled to, because the first is permanent and the
    second usually resolves itself on the agent's next turn.
    """

    context: ContextWindow | None = None
    windows: tuple[UsageWindow, ...] = ()
    observed_at: datetime | None = None
    stale_source: str | None = None
    """Where a figure came from when it did not come from the session's own files.

    Set only by a reader that fell back to something another program maintains — today just
    Claude's limits, which are read out of the status-line cache described in
    `adapters.agents.usage`. Presentation says so out loud, because a number whose freshness
    depends on a script this project does not own must not be shown as though the service had
    measured it.
    """

    @property
    def is_empty(self) -> bool:
        """Whether this carries nothing worth rendering a line for."""
        return self.context is None and not self.windows


@dataclass(frozen=True, slots=True)
class UsageQuery:
    """The identity of one managed session, in the terms a provider's files are searched by.

    None of the four fields is optional-by-accident. `workspace` is what every provider keys
    its conversations on, `started_at` is what separates this session's conversation from the
    ones the owner ran in the same directory yesterday, and `resume_source_id` short-circuits
    both when it is present — a resumed session names its provider conversation exactly, so a
    reader that has one never has to search at all.
    """

    profile_id: ProfileId
    workspace: Path
    started_at: datetime
    resume_source_id: str | None = None
