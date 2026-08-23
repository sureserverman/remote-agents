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

That holds for `sessions` and `projects` too, and holding it there is the point: a boundary
carrying a session use case and no project creation is not a broken composition, it is a
host that cannot register a project and must not advertise Add Project. Forty-nine sites in
this suite are that host. A factory that filled those two with working stubs would make
every one of them advertise an affordance it cannot perform.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from remote_agents.application.backend import Backend
from remote_agents.application.project_catalog import CatalogProject
from remote_agents.domain.models import SessionId
from remote_agents.domain.profiles import ProfileCompatibility
from remote_agents.domain.trust import TrustState
from remote_agents.ports.agent_activity import AgentActivity

_UNSET: object = object()
"""Distinguishes "the caller said nothing" from "the caller said None".

`None` is a meaningful value for four of these fields — it is how a host says it offers no
resume, no inspect, no feed — so it cannot double as the not-stated marker.
"""


def backend_for(
    *,
    sessions: object | None = _UNSET,
    projects: object | None = _UNSET,
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
        "sessions": sessions,
        "projects": projects,
        "conversations": conversations,
        "catalogue": catalogue,
        "refresh_catalogue": refresh_catalogue,
        "profiles": profiles,
        "capture": capture,
        "activity_feed": activity_feed,
        "max_label_length": max_label_length,
    }
    return Backend(**{name: value for name, value in stated.items() if value is not _UNSET})


class SessionUseCaseDouble:
    """Answers the three reads a screen makes while drawing, as a host with none of them.

    Every session detail this bot renders asks the same three questions of its session use
    case, whatever the test is actually about: does this pane still exist (`inspect`), is it
    waiting on the folder-trust question (`trust_state`), and how recently has each project
    been launched into (`project_usage`). A double written for a navigation test models none
    of them, and until Stage 3 it did not have to — `service.py` asked by `getattr` and read
    a missing method as a missing capability.

    That probe is gone, because in production the field is a `SessionService` and every one
    of those methods exists; a composition that failed to wire the use case now says so
    instead of quietly withholding a row. What the probe was also doing, invisibly, was
    supplying these defaults to thirteen test doubles. So they are supplied here instead —
    once, in test support, where it is a statement about the double rather than a decision
    the product makes about itself.

    The three are exactly the render-time reads. `rename` and `copy_attach` are deliberately
    absent: those are asked only when the owner takes an action, so a test that drives one
    models it, and a test that does not should fail loudly rather than pass against a stub.
    """

    async def inspect(self, query: object) -> None:
        """No pane on this host, which is what an unmodelled terminal honestly has."""
        del query
        return None

    async def trust_state(self, session_id: object) -> TrustState:
        """Not waiting on the dialog — the answer `trust_available` reads as "no row"."""
        del session_id
        return TrustState.UNKNOWN

    async def project_usage(self) -> tuple[()]:
        """No launch history, so the catalogue keeps the order it was built in."""
        return ()


#: The `TuiContext` fields that are the surface's own rather than the backend's — the attach
#: route (DEC-039), the profile narrowing this surface applies, the capture parameter, and
#: the four console capabilities (DEC-040).
def _surface_fields() -> frozenset[str]:
    """Every `TuiContext` field that is not the backend, read off the dataclass.

    Hand-listed until Stage 4's Task 4.4 added `console_show_projects` and this set did not
    know about it -- so the new field went to `backend_for`, which refused it with a
    `TypeError` naming a helper the test had never mentioned. A list that has to be updated
    whenever a field is added is a list that will be out of date, and the failure it produces
    points away from the change that caused it.

    Derived instead. `TuiContext` is a frozen dataclass, so its own fields are the authority,
    and a field added tomorrow is sorted correctly the day it appears.
    """
    from dataclasses import fields

    from remote_agents.adapters.tui.context import TuiContext

    return frozenset(field.name for field in fields(TuiContext)) - {"backend"}


def tui_context_for(**arguments: object):
    """Build a `TuiContext`, sorting each argument into the backend or the surface.

    The twin of `backend_for`, for the several test helpers that take one flat `**overrides`
    dict and hand it straight to the constructor. Those cannot be rewritten mechanically the
    way a literal call can, and rewriting each by hand would put the same split in six
    places — where the seventh would get it subtly wrong.

    It names the split rather than hiding it: anything `TuiContext` declares is the surface's,
    everything else is a `Backend` field and goes through `backend_for`, so an unknown key
    fails loudly at `Backend(**...)` rather than being silently dropped. Deliberately no
    `launcher`/`creator` aliases — the point of Stage 3 is that those names are gone, and a
    compatibility shim here would keep them alive in the one place nobody looks.
    """
    from remote_agents.adapters.tui.context import TuiContext

    surface_fields = _surface_fields()
    surface = {name: value for name, value in arguments.items() if name in surface_fields}
    backend = {name: value for name, value in arguments.items() if name not in surface_fields}
    return TuiContext(backend=backend_for(**backend), **surface)  # type: ignore[arg-type]
