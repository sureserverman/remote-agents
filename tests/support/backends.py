"""Build a `Backend` for a test that cares about one corner of it.

Both frontends are typed against `application.backend.Backend` (ARCH-B1), and the suite
constructs them at 127 sites. Almost none of those sites are about the backend: they are
about a navigation bar, a wizard step, a stop dispatch, and they carry one or two partial
doubles to get there. Making each of them state the whole composition would not just be
tedious — it would make every test assert a wiring it does not mean, and a test that
states a wiring it does not mean is a test that starts failing for reasons it is not
about.

So this fills the gaps, under two rules.

**It returns a real `Backend`.** Not a namespace, not a mock. The reason Stage 3 types the
frontends at all is that what they receive is what `compose_backend` builds; a factory
handing them a lookalike would put the untyped seam back one layer down, where nothing
checks it.

**It invents no capability.** An unstated field comes back as whatever `Backend` declares
it to be, read off the type rather than copied here — so `capture` and `activity_feed`
default to `None`, and a test that never mentions inspect sees a host that does not offer
it. That is a host the composition root really can build: those two are wired per process.
A factory that defaulted them to working stubs would hide every not-offered-here branch in
the suite behind a helper nobody opens.

`sessions` and `projects` have no default on `Backend`, because no real process runs
without them. Omitted here they become inert placeholders that fail by name on first use —
`'_UnstatedSessionUseCase' object has no attribute 'list_sessions'` says the test did not
ask for one, which is the thing a reader needs to know.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from remote_agents.application.backend import Backend
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import SessionId
from remote_agents.domain.profiles import ProfileCompatibility
from remote_agents.ports.agent_activity import AgentActivity

_UNSET: object = object()
"""Distinguishes "the caller said nothing" from "the caller said None".

`None` is a meaningful value for four of these fields — it is how a host says it offers no
resume, no inspect, no feed — so it cannot double as the not-stated marker.
"""


class _UnstatedSessionUseCase:
    """Stands in for `SessionService` when a test did not ask for one."""


class _UnstatedProjectUseCase:
    """Stands in for `ProjectCreationService` when a test did not ask for one."""


def backend_for(
    *,
    sessions: object = _UNSET,
    projects: object = _UNSET,
    conversations: object | None = _UNSET,
    catalogue: tuple[CatalogProject, ...] = _UNSET,  # type: ignore[assignment]
    refresh_catalogue: Callable[[], tuple[CatalogProject, ...]] | None = _UNSET,  # type: ignore[assignment]
    profiles: tuple[ProfileCompatibility, ...] = _UNSET,  # type: ignore[assignment]
    capture: Callable[[SessionId], Awaitable[str]] | None = _UNSET,  # type: ignore[assignment]
    activity_feed: Callable[[], Awaitable[tuple[AgentActivity, ...]]] | None = _UNSET,  # type: ignore[assignment]
    max_label_length: int = _UNSET,  # type: ignore[assignment]
) -> Backend:
    """A `Backend` carrying what the caller stated and `Backend`'s own defaults for the rest.

    Every parameter mirrors a field, so a call reads as the composition it means. The
    defaults are deliberately *not* restated: an unstated field is dropped before
    construction and `Backend` supplies its own, which is what keeps this helper from
    drifting the first time one of those defaults changes.
    """
    stated = {
        "sessions": _UnstatedSessionUseCase() if sessions is _UNSET else sessions,
        "projects": _UnstatedProjectUseCase() if projects is _UNSET else projects,
        "conversations": conversations,
        "catalogue": catalogue,
        "refresh_catalogue": refresh_catalogue,
        "profiles": profiles,
        "capture": capture,
        "activity_feed": activity_feed,
        "max_label_length": max_label_length,
    }
    return Backend(**{name: value for name, value in stated.items() if value is not _UNSET})
