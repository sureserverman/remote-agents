"""Value objects and row keys the app and its screens both need.

Extracted so `screens/` can import them without importing the app, which imports `screens/`
to install its default screen. Nothing here knows about Textual: these are the surface's own
data, and `app.py` re-exports them so existing importers keep working.
"""

from __future__ import annotations

from dataclasses import dataclass

from remote_agents.adapters.tui.context import ProfileChoice
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.relative_time import age
from remote_agents.application.session_actions import state_word
from remote_agents.domain.conversations import ConversationSummary
from remote_agents.domain.models import SessionRecord, normalize_label
from remote_agents.domain.projects import ProjectIdentity

# Row keys for choices that are navigation rather than data. The NUL prefix is what keeps
# them from colliding with a project id, a profile id, or a conversation reference.
_NEXT = "\x00next"
_PREVIOUS = "\x00previous"
_BACK = "\x00back"
_CANCEL = "\x00cancel"
#: The one row a position shows in place of no rows at all. Disabled when rendered, so it
#: occupies the space the absent rows would have without becoming a choice.
_EMPTY = "\x00empty"


@dataclass(frozen=True, slots=True)
class LaunchSelection:
    """What the wizard has gathered so far, and nothing the surface has not been given.

    Two fields, and it had a third: `label`, set by a launch-time step that no longer exists.
    A `review()` method went with it — three lines of gathered summary that no caller in `src/`
    or `tests/` ever read, because the breadcrumb took over naming the project and the agent
    and the one part it could not carry was the label.
    """

    project: CatalogProject | None = None
    profile: ProfileChoice | None = None


@dataclass(frozen=True, slots=True)
class LaunchFailure:
    """A launch that handed back no session: what stays on screen, and what is said once.

    Two fields rather than one string because the two halves have different lifetimes, which
    is the distinction the status split is built on. `status` is what the owner may still need
    in a minute — an attach command they have to copy, or where to go next; `explanation` is
    why, which they read once and are done with.

    Returned rather than rendered for the reason `launch` has always returned its message: a
    failure has to leave the cursor somewhere deliberate, and only the review screen knows
    where that is.
    """

    status: str
    explanation: str


@dataclass(frozen=True, slots=True)
class AttachRequest:
    """The one command the app hands back to its caller after a ready launch."""

    session_id: str
    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.session_id or not self.argv:
            raise ValueError("an attach request needs a session and a command")

    @property
    def command(self) -> str:
        return " ".join(self.argv)


def label_or_error(value: str, limit: int) -> str | None:
    """Normalize an optional session label under the configured bound.

    The rule itself is `domain.models.normalize_label`; what this adds is the *optional* part.
    A blank field means "no label" on a form the owner may simply leave alone, so blank returns
    `None` here rather than raising — the one place the two differ, and the reason this wrapper
    exists at all instead of the screens calling the domain directly.
    """
    if not value.strip():
        return None
    try:
        return normalize_label(value, max_length=limit)
    except ValueError as error:
        # The domain states the rule; this surface states it in the words its form uses, and
        # the bound quoted is the host's configured one rather than the domain ceiling.
        raise ValueError(f"use a visible label of up to {limit} characters") from error


def selectable_area(value: str) -> bool:
    """Offer an existing directory only when the project identity rule also accepts it."""
    try:
        ProjectIdentity(area=value, name=value)
    except ValueError:
        return False
    return True


def conversation_row(summary: ConversationSummary) -> str:
    """Render a conversation for selection: never a provider ID, and no filtering beyond that.

    This line used to promise "never a provider ID, path, or path fragment". The first third
    is true and enforced -- `ProviderConversationId` is not a field of `ConversationSummary`
    and cannot reach here. The rest was never enforced anywhere: `description` is the owner's
    own last prompt, checked for length and printability and never for content, so a path they
    typed is rendered like any other text (BL-007, and see `ConversationSummary` itself).
    """
    described = summary.description or "(no description)"
    return f"{described} · {summary.state.value} · {age(summary.updated_at)}"


def session_row(record: SessionRecord) -> str:
    """One list row. The state word comes from the shared policy, never from `state.value`.

    Both surfaces had this exact line, independently, which is how they rendered both kinds
    of ORPHANED identically (BL-031). `state_word` is the single authority now.
    """
    word = state_word(record.state, record.orphan_provenance)
    return f"{record.display.rendered} · {word} · {age(record.created_at)}"
