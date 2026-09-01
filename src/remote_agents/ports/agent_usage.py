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

from dataclasses import KW_ONLY, dataclass
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
    limit_declared: bool = False
    """Whether the ceiling was *stated* by the owner rather than published by the provider.

    The two are not the same claim and must not read the same. Codex writes
    `model_context_window` into its own rollout, so its denominator is measured; Claude publishes
    none, so its denominator is whatever the owner declared in config -- or, on a host that has
    stated nothing, this project's default. DEC-061's rule is that a figure this service did not
    measure says so out loud, which is why `AgentUsage.stale_source` exists for the borrowed
    limits; this is the same rule reaching the other borrowed number.

    Without it `556k of 1.0M · 56%` and `184k of 258k · 71%` render identically while one
    denominator is an assertion and the other is a measurement.
    """

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
    `adapters.agents.claude.usage`. Presentation says so out loud, because a number whose freshness
    depends on a script this project does not own must not be shown as though the service had
    measured it.
    """

    @property
    def is_empty(self) -> bool:
        """Whether this carries nothing worth rendering a line for."""
        return self.context is None and not self.windows


@dataclass(frozen=True, slots=True)
class AgentLimits:
    """What one agent has spent against its plan, for the whole account rather than a session.

    Separate from `AgentUsage` because the two answer questions of different *scope*, and
    collapsing them is what the owner reported on 2026-08-29: a rate-limit window rendered
    inside a session's detail reads as that session's spend, when both providers that publish
    one publish it for the account. The window is the same fact whichever session is open, and
    a type that can only be obtained by naming a session cannot say so.

    That difference is also why this carries a `profile_id` and `AgentUsage` does not. A usage
    read is handed back to a caller that just named a session, so the identity is already in
    the caller's hand; a limits read takes no query at all, and a set of them would otherwise
    be a tuple of percentages with nothing to attach them to.

    **Nothing here is required and nothing is validated beyond `UsageWindow`'s own rules.**
    `opencode` and `cursor-agent` publish no limits at all, so an empty `windows` is the
    honest answer rather than a degenerate one — and a type that refused it would push those
    readers into either returning `None`, which this project words as "no conversation
    matched" and means something else entirely, or inventing a window. DEC-061 forbids the
    second and the first would be a lie, so the empty tuple has to be legal.
    """

    profile_id: ProfileId
    windows: tuple[UsageWindow, ...] = ()

    _: KW_ONLY
    """Everything past here is *about* the answer rather than the answer, and is named.

    Measured during the stage that added `observed_at`: it was inserted between `windows` and
    `stale_source`, and one of the two callers still passing three positional arguments put the
    borrowed-cache string into the timestamp field and silently left the provenance stamp
    empty — so a figure this project cannot vouch for rendered as though it had been measured
    here, which is the one thing DEC-061 forbids outright. The payload stays positional because
    it is what the caller asked for; a field added between these two can no longer shift one
    into another.
    """

    observed_at: datetime | None = None
    """When the *provider* recorded these figures — not when this process read them.

    A rate-limit percentage only moves when the agent takes a turn, so the number in a file is
    a statement about the moment it was written and stays frozen while the agent is idle. The
    window it counts against keeps moving regardless. So a reading hours old is not a slightly
    old truth, and presentation is entitled to say how old it is; without this field the only
    honest alternatives would be to withhold the figure or to show it as current.

    `AgentUsage.observed_at` means the other thing — when the read happened — because a context
    window is re-derived from the transcript on every read and has no separate observation
    instant. The names match; the questions do not.
    """

    stale_source: str | None = None
    """Where these came from when they did not come from the provider's own accounting.

    Set by Claude's reader alone, and for the reason `AgentUsage.stale_source` records: its
    limits are borrowed from the status-line cache described in `adapters.agents.claude.usage`, and
    a figure whose freshness depends on a script this project does not own is never rendered
    as though the service had measured it.
    """


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
