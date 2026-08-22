"""Everything the local terminal surface may use, resolved once by the composition root."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from remote_agents.application.backend import Backend
from remote_agents.application.console import RecoveryReport
from remote_agents.application.profiles import ProfileAvailability

#: How many observations the feed shows and its reader fetches — one number, imported by
#: both the composition root (the reader's LIMIT) and the dashboard (the render slice), so
#: the two can never drift.
FEED_LIMIT = 20


@dataclass(frozen=True, slots=True)
class TuiContext:
    """The sealed surface the terminal app drives; it never reaches past these."""

    backend: Backend
    """Every use case this surface may drive, as the composition root assembled it (ARCH-B1).

    Eight fields stood here instead — `launcher`, `creator`, `refresh_catalogue`,
    `catalogue`, `capture`, `conversations`, `activity_feed`, `max_label_length`. They were
    correctly typed, unlike the bot's, and that was the odd part: the same objects, named
    twice, composed twice, and only one of the two namings kept honest. A capability added
    to one surface could miss the other with nothing to say so.

    `attach_argv` and the four console fields stay outside it because they are this
    surface's alone: DEC-039 keeps the attach route per-surface, and DEC-040 keeps console
    hosting out of anything the bot shares.

    `profiles` used to stand outside it too, for a reason that no longer holds:
    `Backend.profiles` was the domain `ProfileCompatibility` and this surface rendered its
    own `ProfileChoice`, which refused any reason alongside `available=True` — the rule that
    once took this surface down on a version probe that merely timed out. Both narrowings
    are now one, in `application/profiles.py`, and it is read off the backend.
    """
    profiles: tuple[ProfileAvailability, ...]
    attach_argv: Callable[[str], tuple[str, ...]]
    # `capture_redactions` is not a capability but a parameter of one: it tunes
    # `backend.capture`, can only remove text from what is rendered, and is inert when
    # capture is None. Nothing sources it today; the bot passes no redactions either.
    capture_redactions: tuple[str, ...] = field(default_factory=tuple)
    # The console capabilities, same widening pattern as the two above: when the
    # composition root determines the surface is hosted by a client on our own tmux server,
    # opening a session **exchanges** that agent's pane into the console's left slot and the
    # surface stays alive, while `console_sync` notices what the other writer did to whatever
    # is displayed, wherever the surface reloads its list. Hosts wiring neither keep the
    # exec-attach contract exactly as it was.
    open_in_console: Callable[[str], Awaitable[str | None]] | None = None
    console_sync: Callable[[tuple], Awaitable[None]] | None = None
    # One line on the tmux status bar when the feed gains news — wired only under console
    # hosting, where a status line exists to flash on; a glance-level nudge, never a modal.
    console_flash: Callable[[str], Awaitable[None]] | None = None
    # What the console's start-only repair did and could not do, carried to the surface
    # rather than printed. The composition root runs `settle()` before Textual starts, so a
    # `print` there is erased by the alternate screen microseconds later — invisible for the
    # whole session it describes. Only the process resident in the console's left slot gets a
    # report with anything in it; every other pane is refused by `settle`'s own guard and
    # receives an empty one.
    console_recovery: RecoveryReport | None = None

    def __post_init__(self) -> None:
        """Refuse a backend this surface cannot actually drive.

        `launcher: SessionService` and `creator: ProjectCreationService` were **required**
        fields here before Stage 3. Folding them into `Backend`, where both are optional so
        the bot can represent a host that wired neither, silently made them optional here
        too — and this surface has no `is None` guard on either. `refresh_readiness` on the
        ordinary sessions reload, `available_areas` in the project screen, and seven other
        call sites dereference them straight. A composition that forgot one would therefore
        start and die inside the dashboard's mount worker: the same silent-absence failure
        the refactor set out to end, moved from a `getattr` to a dataclass default.

        So the guarantee is restored rather than described. It is the contract this class
        already had; the bot keeps the optional one because it genuinely degrades, answering
        "that is unavailable" at thirteen guarded entry points.
        """
        # Spelled out rather than looped over a tuple of field names. The loop wanted an
        # attribute lookup by name, which is not a capability probe here — the names are
        # literals two lines up — but it is indistinguishable from one to the sweep that
        # keeps probes out of the adapters, and it tripped it. A rule worth enforcing is
        # worth not arguing with over a saved line.
        if self.backend.sessions is None:
            raise ValueError("the local surface requires a backend with `sessions`")
        if self.backend.projects is None:
            raise ValueError("the local surface requires a backend with `projects`")

    @property
    def max_label_length(self) -> int:
        """The host's configured bound, which now has exactly one home.

        Kept as a property rather than pushed to every reader: it is read where a name is
        being validated, and `self.services.backend.max_label_length` at those sites reads
        as plumbing rather than as the rule it is. `Backend.__post_init__` does the
        validation this class used to do, so the check is not lost by moving.
        """
        return self.backend.max_label_length
