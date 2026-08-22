"""The test factory that builds a `Backend` the way the composition root does.

Stage 3 types both frontends against `Backend`, and there are 79 `PrivateBotBoundary(`
construction sites plus 48 `TuiContext(` ones across the suite. Almost none of them care
about the backend as a whole: a navigation test wants one launcher that lists one session,
a wizard test wants a project creator and nothing else. Without a factory, typing the
boundary would mean writing every absent field at every one of those sites, and the cost
of that is not the typing — it is that each site then states a wiring it does not mean,
which is how a test comes to assert against a composition production never builds.

`backend_for` takes the partial stubs the suite already has and fills the rest with the
same absences `Backend`'s own defaults declare. The two rules it exists to keep are that
the thing it returns is a **real `Backend`** — not a lookalike a test could pass against
and production could never build — and that it **invents no capability**: a field nobody
asked for comes back absent, because a factory that defaulted `capture` to a stub would
give every test an inspect affordance the host may not have wired.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from backends import backend_for

from remote_agents.application.backend import Backend
from remote_agents.application.project_catalog import CatalogProject

PROJECT = CatalogProject("a" * 24, "Demo", "tests", "Registered")


class _PartialLauncher:
    """Two methods of `SessionService`'s twenty — the shape the suite's doubles are in."""

    async def list_sessions(self):
        return []

    async def refresh_readiness(self) -> None:
        return None


def test_the_factory_returns_a_real_backend() -> None:
    """A `SimpleNamespace` would satisfy every caller and pin nothing.

    The point of typing the frontends is that the object they receive is the object the
    composition root builds. A factory returning a lookalike would let the whole suite go
    green against a shape `compose_backend` never produces, which is the failure the type
    exists to prevent, reintroduced one layer down.
    """
    backend = backend_for()

    assert type(backend) is Backend
    with pytest.raises(FrozenInstanceError):
        backend.max_label_length = 5  # type: ignore[misc]


def test_a_partial_double_is_accepted_as_the_session_use_case() -> None:
    """The suite's doubles implement the two methods their test drives, and no more.

    `Backend.sessions` is typed `object` for exactly this release, so a partial stub is
    not a compromise the factory makes — it is what the field already permits. If this
    ever fails it will be because the factory started validating the stub, and the 79
    sites are what would pay for it.
    """
    launcher = _PartialLauncher()

    backend = backend_for(sessions=launcher)

    assert backend.sessions is launcher


def test_an_unasked_capability_comes_back_absent() -> None:
    """The factory fills gaps with the absences `Backend` declares, never with stubs.

    A test that says nothing about inspect must see a host that offers no inspect, because
    that is a host the composition root really can build — `capture` and `activity_feed`
    are wired per process. Defaulting them to working stubs would hide every "this
    affordance is not offered here" branch behind a factory nobody reads.
    """
    backend = backend_for(sessions=_PartialLauncher())

    assert backend.capture is None
    assert backend.activity_feed is None
    assert backend.conversations is None
    assert backend.refresh_catalogue is None
    assert backend.catalogue == ()
    assert backend.profiles == ()


def test_the_defaults_are_the_type_s_own_defaults() -> None:
    """Read off `Backend`, never restated here.

    A factory carrying its own copy of the defaults drifts from the type the first time a
    field's default changes, and the drift is silent: every test keeps passing against the
    old value. So this asserts equality with a bare production-shaped `Backend` rather
    than with literals.
    """
    bare = Backend(sessions=object(), projects=object())
    made = backend_for()

    assert made.max_label_length == bare.max_label_length
    assert made.catalogue == bare.catalogue
    assert made.profiles == bare.profiles


def test_what_the_caller_states_survives() -> None:
    """The gap-filling must not overwrite an argument — the one bug a factory like this has."""
    launcher = _PartialLauncher()
    creator = object()

    backend = backend_for(
        sessions=launcher,
        projects=creator,
        catalogue=(PROJECT,),
        max_label_length=12,
    )

    assert backend.sessions is launcher
    assert backend.projects is creator
    assert backend.catalogue == (PROJECT,)
    assert backend.max_label_length == 12
