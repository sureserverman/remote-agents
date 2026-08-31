"""Telegram transport adapter."""

from __future__ import annotations

from remote_agents.ports.frontend_descriptor import FrontendDescriptor


def _wire(*args: object, **kwargs: object) -> object:
    """Defer the transport import so reading the descriptor never loads the Telegram library."""
    from remote_agents.adapters.telegram.service import build_private_bot

    return build_private_bot(*args, **kwargs)


#: What this surface is and what it cannot start without (ARCH-03). The claim names only
#: capabilities every host wires; the optional ones (conversations, capture, usage) degrade
#: on-screen by design and must not stop a bare host from serving.
FRONTEND = FrontendDescriptor(
    name="telegram",
    wire=_wire,
    required_capabilities=("sessions", "projects", "refresh_catalogue"),
)
