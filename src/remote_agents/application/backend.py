"""The one backend both frontends drive.

Before this, `bootstrap` composed the Telegram service and the local surface separately:
two `SessionService` instances over the same SQLite file, two catalogue providers, two
profile probes, sharing only the helper functions that built their parts. Nothing was wrong
with either composition — what was wrong was that there were two, and that only one of them
was typed. `PrivateBotBoundary` declared its half `launcher: object | None` and reached into
it by name (`getattr(self.launcher, "rename", None)` and four siblings), so a capability the
composition root failed to wire produced no error anywhere: just a row that quietly stopped
being offered, on one surface, until somebody noticed.

`Backend` is what both receive instead, and being a real type is most of the point.

**Composed once per process, not once.** The two processes hold their database handles
differently and must keep doing so: the service keeps one long-lived connection, while a
surface's handle exists only for the duration of a single store operation
(`LeasedConnection`), which is the guarantee DEC-035 replaced the old exec-away contract
with and the README states in those words. So `compose_backend` takes the connection it is
given rather than opening one, and the strategy stays the caller's (ARCH-B2). DEC-005 —
corrected 2026-08-20 to as many as five concurrent writers — is sound only because of that
lease, so collapsing the two would not be a tidy-up; it would remove the thing that makes
the writer count safe.

**Application, domain and ports only** (ARCH-B1). `application/` may not import an adapter
(ARCH-02, DEC-015), and `check_imports.py` enforces it, but the rule is easy to break here
in particular: the natural instinct when adding a field is to type it against whichever
frontend consumes it. That is not hypothetical — `bootstrap.LocalRuntime` used to be typed
against `adapters.telegram.wizard.ProfileAvailability` and handed it to the local surface,
which converted it back. `profiles` below is what that instinct cost, and what replaced it:
one `application/` type both surfaces read.

**What is deliberately absent.** Anything only one surface has: the reconciler and
`SessionLocks` (the service's, composed at the root per DEC-030), console hosting and
`attach_argv` (the surface's, per DEC-040 and DEC-039). Both processes do wire
`hide_in_console`, from their own composers — the surface's builds and arranges the console,
the bot's is hide-only and never calls `ensure` — and that is wired into `SessionService`
at composition time rather than being a field here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from remote_agents.application.profiles import ProfileAvailability
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import MAX_LABEL_LENGTH, SessionId
from remote_agents.ports.agent_activity import AgentActivity


@dataclass(frozen=True, slots=True)
class Backend:
    """The sealed set of use cases a frontend may drive, and nothing besides."""

    sessions: object | None = None
    """The session lifecycle use case (`application.services.SessionService`).

    Typed `object` for one release only. `SessionService` imports `ports.terminal`, and
    naming it here would be correct but would pull the whole port graph into every module
    that reads a `Backend` — including the two frontends' test doubles, which is the churn
    Task 3.1's `backend_for` factory exists to avoid. Sub-plan 4 tightens it once the
    frontends no longer construct partial backends.

    **Optional because the bot's existing contract needs it to be, and only the bot's.**
    `PrivateBotBoundary` answers "that is unavailable" rather than failing to start, at
    thirteen guarded entry points, and it has always done so. This type records what a
    process wired, so it has to be able to record one that wired nothing — otherwise typing
    the seam would change behaviour on the hosts relying on that absence, which is the one
    thing this refactor may not do.

    **The local surface is the other case, and it is enforced rather than assumed.**
    `TuiContext` declared `launcher: SessionService` as a required field and has no `is
    None` guard anywhere; making it optional here would have made a forgotten wiring die in
    the dashboard's mount worker instead of at construction. `TuiContext.__post_init__`
    therefore refuses a backend missing `sessions` or `projects`. Two frontends, two
    contracts, both now stated somewhere a reader can find them.

    That optionality is *not* how the composition root is held to wiring it. Nothing
    reaches for a capability by name any more, so a forgotten field is a named `None` on a
    declared type rather than a `getattr` returning quietly, and
    `test_compose_backend_builds_one_backend_from_the_real_helpers` asserts every real
    composition carries one. The absence is representable; it is not permitted in
    production and a test says so.
    """

    projects: object | None = None
    """Project creation (`application.project_admin.ProjectCreationService`).

    Optional for the reason `sessions` is, and independently of it: forty-nine boundaries
    in the suite carry a session use case and no project creation, because a host that
    cannot register a project must not advertise Add Project. `help_command` is where that
    composition describes itself.
    """

    conversations: object | None = None
    """Resume catalogue and resolution, or None on a host that offers no resume."""

    catalogue: tuple[CatalogProject, ...] = ()
    """The catalogue snapshot this process started with."""

    refresh_catalogue: Callable[[], tuple[CatalogProject, ...]] | None = None
    """Re-scan the registry and dev root. A read of the filesystem, so both frontends run
    it off the event loop; neither may call it from a render."""

    profiles: tuple[ProfileAvailability, ...] = ()
    """Installed-agent availability, narrowed once for both surfaces.

    `compose_backend` is the only place the domain's `ProfileCompatibility` becomes this, so
    a profile is probed once per process and narrowed once per process. That was not true
    until sub-plan 4: the bot was handed `LocalRuntime.profiles` and the local surface
    re-narrowed `LocalRuntime.profiles` again, so profiles were the one capability composed
    twice and free to diverge — the single exception to the promise the rest of this type
    makes.

    Merging the two narrowings needed a type that could hold what both surfaces meant, which
    is why it waited for its own stage. `ProfileCompatibility.reason` answers two questions
    at once — why a profile is blocked, and why no version is being shown — and the local
    surface's old type read any reason as blocking, so a version probe that merely timed out
    took it down. `ProfileAvailability` splits that field, and each surface reads the part it
    means: `blocked_reason` alone for the local surface, `any_reason` for the bot's one-string
    row. DEC-043 — the decision is shared, the sentence stays the surface's.
    """

    capture: Callable[[SessionId], Awaitable[str]] | None = None
    """Read a session's pane. None on a host that offers no inspect affordance."""

    activity_feed: Callable[[], Awaitable[tuple[AgentActivity, ...]]] | None = None
    """A bounded newest-first read of the durable activity table — a reader, never a
    drainer: consuming the spool would starve the phone's notifications (DEC-031/DEC-034)."""

    max_label_length: int = MAX_LABEL_LENGTH
    """The host's configured bound, clamped by `config` to 1..40 and never looser than the
    domain's.

    The default is the domain constant rather than a copy of its value. Production never
    reaches it -- `compose_backend` always passes `config.max_label_length` -- so the two
    agreeing was coincidence rather than a rule, and a change to the domain ceiling would
    have moved one and not the other.
    """

    def __post_init__(self) -> None:
        if self.max_label_length < 1:
            raise ValueError("label length bound must be positive")
