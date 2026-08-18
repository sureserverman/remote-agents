"""Everything the local terminal surface may use, resolved once by the composition root."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from remote_agents.application.conversations import ConversationService
from remote_agents.application.project_admin import ProjectCreationService
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.application.services import SessionService
from remote_agents.domain.models import SessionId


@dataclass(frozen=True, slots=True)
class ProfileChoice:
    """One curated agent, with the reason it cannot be launched when it cannot."""

    profile_id: str
    available: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.profile_id:
            raise ValueError("profile identifier must be present")
        if self.available and self.reason is not None:
            raise ValueError("an available profile has no blocking reason")


@dataclass(frozen=True, slots=True)
class TuiContext:
    """The sealed surface the terminal app drives; it never reaches past these."""

    launcher: SessionService
    creator: ProjectCreationService
    profiles: tuple[ProfileChoice, ...]
    refresh_catalogue: Callable[[], tuple[CatalogProject, ...]]
    attach_argv: Callable[[str], tuple[str, ...]]
    max_label_length: int = 40
    catalogue: tuple[CatalogProject, ...] = field(default_factory=tuple)
    # Widened deliberately for the two capabilities that need a dependency the launch wizard
    # never did: `capture` (inspect) and `conversations` (resume). Both are optional, so a
    # host that wires neither simply offers neither affordance rather than failing to start.
    # `capture_redactions` is a third field but not a third capability: it parameterizes
    # `capture`, can only remove text from what is rendered, and is inert when capture is
    # None. Nothing sources it today; the bot passes no redactions either.
    capture: Callable[[SessionId], Awaitable[str]] | None = None
    capture_redactions: tuple[str, ...] = field(default_factory=tuple)
    conversations: ConversationService | None = None
    # The console capability, same widening pattern as the two above: when the composition
    # root determines the surface is hosted by a client on our own tmux server, opening a
    # session means switching that client — awaited here — and the surface stays alive.
    # A host wiring nothing keeps the exec-attach contract exactly as it was.
    open_in_console: Callable[[str], Awaitable[None]] | None = None

    def __post_init__(self) -> None:
        if self.max_label_length < 1:
            raise ValueError("label length bound must be positive")
