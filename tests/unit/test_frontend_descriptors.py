"""Each frontend declares what it needs from the one Backend, and composition checks it.

The descriptor carries wiring and a claim, never use-case logic (DEC-043): its
`required_capabilities` name `Backend` fields the surface cannot start without, and the
composition root refuses loudly when a claimed capability was wired `None` — the
alternative is a surface that composes cleanly and fails on first use, which is the drift
this seam exists to make impossible.
"""

from __future__ import annotations

import pytest

from remote_agents.adapters import telegram, tui
from remote_agents.application.backend import Backend
from remote_agents.composition.backend import require_frontend_capabilities
from remote_agents.ports.frontend_descriptor import FrontendDescriptor


def test_both_frontends_export_a_descriptor() -> None:
    assert isinstance(telegram.FRONTEND, FrontendDescriptor)
    assert isinstance(tui.FRONTEND, FrontendDescriptor)
    assert telegram.FRONTEND.name == "telegram"
    assert tui.FRONTEND.name == "tui"


def test_every_required_capability_is_a_real_backend_field() -> None:
    fields = set(Backend.__dataclass_fields__)
    for descriptor in (telegram.FRONTEND, tui.FRONTEND):
        assert descriptor.required_capabilities, descriptor.name
        for name in descriptor.required_capabilities:
            assert name in fields, f"{descriptor.name} requires unknown capability {name!r}"


def test_a_claim_the_host_wired_none_fails_at_composition() -> None:
    """Fail at compose time, not on first use — the whole point of the claim."""
    from backends import backend_for

    backend = backend_for(
        sessions=object(),  # type: ignore[arg-type]
        projects=object(),  # type: ignore[arg-type]
        refresh_catalogue=tuple,
    )

    fabricated = FrontendDescriptor(
        name="fabricated", wire=None, required_capabilities=("conversations",)
    )
    with pytest.raises(ValueError, match="conversations"):
        require_frontend_capabilities(fabricated, backend)

    unknown = FrontendDescriptor(name="fabricated", wire=None, required_capabilities=("no_such",))
    with pytest.raises(ValueError, match="no_such"):
        require_frontend_capabilities(unknown, backend)

    honest = FrontendDescriptor(name="honest", wire=None, required_capabilities=("sessions",))
    assert require_frontend_capabilities(honest, backend) is backend
